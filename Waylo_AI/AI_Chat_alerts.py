#!/usr/bin/env python3
"""
AI_Chat_alerts.py – Wailo Assistant with user-facing alerts and safeguards

Adds non-blocking alerts for:
- Offline/online transitions
- Slow STT / LLM calls
- TTS start delays and clean cancellation on exit
- Slow mic open and repeated no-speech events
- DND (quiet time) notification

Falls back to local beeps if network TTS isn't available.
"""

import os
import time
import threading
import numpy as np

import AI_Chat as core  # reuse orchestration, api client, TTS/STT/mic


# ───────────── config / thresholds ─────────────
STT_WARN_SEC          = float(os.getenv("WAILO_STT_WARN_SEC", "3"))
TTS_START_WARN_SEC    = float(os.getenv("WAILO_TTS_START_WARN_SEC", "2"))
LLM_WARN_SEC          = float(os.getenv("WAILO_LLM_WARN_SEC", "5"))
MIC_OPEN_WARN_SEC     = float(os.getenv("WAILO_MIC_OPEN_WARN_SEC", "3"))
OFFLINE_ANNOUNCE_SEC  = float(os.getenv("WAILO_OFFLINE_ANNOUNCE_SEC", "5"))
NO_SPEECH_REPEATS_MAX = int(os.getenv("WAILO_NO_SPEECH_REPEATS", "3"))
ALERT_BEEPS           = os.getenv("WAILO_ALERT_BEEPS", "1").lower() in {"1","true","yes"}


# ───────────── alerts (beep + optional voice) ─────────────
def beep(freq_hz: int = 880, ms: int = 120, volume: float = 0.2) -> None:
    if not ALERT_BEEPS:
        return
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paFloat32, channels=1, rate=16_000, output=True)
        t = np.linspace(0, ms/1000.0, int(16_000 * (ms/1000.0)), False)
        tone = (np.sin(2*np.pi*freq_hz*t) * volume).astype(np.float32)
        stream.write(tone.tobytes())
        stream.stop_stream(); stream.close(); p.terminate()
    except Exception:
        # As a last resort, try ASCII bell
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


def speak_or_beep(text: str) -> None:
    # Prefer a short beep to avoid network latency for micro-alerts
    if not text:
        beep()
        return
    try:
        if core.net_up():
            core.streaming_speak(text)
            return
    except Exception:
        pass
    beep()


# ───────────── network monitor ─────────────
def network_monitor() -> None:
    last_online = None
    offline_since = None
    while not core.STOP_EVENT.is_set():
        online = core.net_up()
        now = time.time()
        if last_online is None:
            last_online = online
            offline_since = None if online else now
        else:
            if online and last_online is False:
                # came back online
                speak_or_beep("I'm back online.")
                offline_since = None
            elif not online and last_online is True:
                offline_since = now

            if not online and offline_since and now - offline_since >= OFFLINE_ANNOUNCE_SEC:
                speak_or_beep("I'm offline. Retrying…")
                offline_since = None  # announce once per outage

            last_online = online

        time.sleep(1.0)


def warn_if_slow(start_ts: float, seconds: float, action: str, alert_text: str) -> None:
    def worker():
        while not core.STOP_EVENT.is_set():
            if time.time() - start_ts >= seconds:
                speak_or_beep(alert_text)
                break
            time.sleep(0.05)
    threading.Thread(target=worker, daemon=True).start()


def main() -> None:
    # Start background network monitor
    threading.Thread(target=network_monitor, daemon=True).start()

    # Intro (per-run, reusing core's guards/throttle)
    if not core.silenced() and core.should_intro():
        # Warn if LLM intro is slow
        t0 = time.time()
        warn_if_slow(t0, LLM_WARN_SEC, "LLM", "Thinking…")
        intro = core.chat_gpt("Introduce yourself to the child.")

        # Respect core's intro logging guard
        try:
            # Best effort: only log once per run + throttle (enforced in core)
            if hasattr(core, "api"):
                core.api.log_request("Introduce yourself to the child.")
                core.api.log_response(intro)
        except Exception:
            pass

        # TTS start slow warning
        t1 = time.time()
        warn_if_slow(t1, TTS_START_WARN_SEC, "TTS", "Having trouble speaking; please wait…")
        core.streaming_speak(intro)
        # Mic open readiness cue if we delay opening mic after TTS
        time.sleep(max(0.0, MIC_OPEN_WARN_SEC - (time.time() - t1)))
        beep()

    no_speech_streak = 0

    while not core.STOP_EVENT.is_set():
        if core.silenced():
            speak_or_beep("I'm on quiet time now.")
            time.sleep(15)
            continue

        # Record
        t_mic_start = time.time()
        # If we delayed too long after last TTS, provide a readiness cue
        def mic_ready_worker():
            time.sleep(MIC_OPEN_WARN_SEC)
            if not core.STOP_EVENT.is_set():
                speak_or_beep("I'm ready.")
        threading.Thread(target=mic_ready_worker, daemon=True).start()

        raw = core.record()
        if not raw:
            no_speech_streak += 1
            if no_speech_streak >= NO_SPEECH_REPEATS_MAX:
                speak_or_beep("Please speak a little louder.")
                no_speech_streak = 0
            continue
        no_speech_streak = 0

        # STT with slow warning
        t_stt = time.time()
        warn_if_slow(t_stt, STT_WARN_SEC, "STT", "Still working on it…")
        utter = core.transcribe(raw)
        if not utter:
            speak_or_beep("Could you repeat that?")
            continue

        if hasattr(core, "log"):
            core.log.info("🗣️ %s", utter)

        # Log request immediately
        req_id = getattr(core, "api", None).log_request(utter) if hasattr(core, "api") else None

        # LLM with slow warning
        t_llm = time.time()
        warn_if_slow(t_llm, LLM_WARN_SEC, "LLM", "Thinking…")
        reply = core.chat_gpt(utter)

        # Start speaking reply (warn if TTS start is slow)
        t_tts = time.time()
        warn_if_slow(t_tts, TTS_START_WARN_SEC, "TTS", "Having trouble speaking; please wait…")

        spoken = {"done": False}
        def speak_worker():
            try:
                core.streaming_speak(reply)
            finally:
                spoken["done"] = True
        threading.Thread(target=speak_worker, daemon=True).start()

        # Log response while audio plays
        if hasattr(core, "api"):
            try:
                core.api.log_response(reply, req_id=req_id)
            except Exception:
                pass

        # Background analytics
        try:
            core.analyze_and_log_async(utter, req_id, None)
        except Exception:
            pass

        # Wait (briefly) for TTS to finish to avoid half-duplex cut-offs
        waited = 0.0
        max_wait = getattr(core, "LISTEN_WAIT_SEC", 10.0)
        listen_after = getattr(core, "LISTEN_AFTER_TTS", True)
        if listen_after:
            while not spoken["done"] and waited < max_wait and not core.STOP_EVENT.is_set():
                time.sleep(0.05); waited += 0.05

        # Exit keywords
        if utter.lower().strip() in {"exit", "quit", "bye"}:
            core.streaming_speak("Goodbye!")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye-bye!")


