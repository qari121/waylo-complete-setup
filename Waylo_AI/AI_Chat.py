#!/usr/bin/env python3
# Wailo Assistant – on-device companion

import os, io, wave, socket, time, logging, threading, platform, tempfile, json, sys
import re
import signal
from pathlib import Path
from datetime import datetime, time as dtime
from dateutil import parser
import numpy as np
import sounddevice as sd, webrtcvad
from pydub import AudioSegment
from elevenlabs import generate, set_api_key
from dotenv import load_dotenv
from openai import OpenAI

from wailo_api import WailoAPI
# Removed per-turn analytics imports; we will do a single async combined call instead
# keep the existing
from google.cloud import texttospeech_v1 as tts          # ← normal, non-streaming

# add right after it
from google.cloud import texttospeech_v1beta1 as tts_stream  # ← streaming client
from google.cloud import speech_v1 as speech

# ───────────── CONFIG ─────────────
PAR_POLL_SEC   = 60
META_PUSH_SEC  = 20 * 60
AUDIO_DEVICE   = (None, None)
SR             = 16_000
VOICE_ID       = "Xb7hH8MSUJpSbSDYk0k2"

# ───────────── ENV / KEYS ─────────────
load_dotenv()

# Default Google credentials file if env not set
DEFAULT_GCP_CRED = Path(__file__).with_name("waylo-251e0-c77ecbaa37ad.json")
if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and DEFAULT_GCP_CRED.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(DEFAULT_GCP_CRED)

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ELEVEN_KEY = "sk_a0520da5d2948fe0b35934a71ba642aaaf21df9d08e70d0d"
AZURE_SPEECH_KEY = ("")
AZURE_REGION = ("southeastasia")
AZURE_SPEECH_ENDPOINT="https://southeastasia.api.cognitive.microsoft.com/"


if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")

client = OpenAI(api_key=OPENAI_KEY)
set_api_key(ELEVEN_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wailo")

# ───────────── graceful signal handling (Ctrl+C / Ctrl+Z / SIGTERM / SIGHUP) ─────────────
STOP_EVENT = threading.Event()
ACTIVE_TTS = threading.Event()
# Alerts/flow coordination
ACTIVE_TTS_STARTED = threading.Event()
MIC_OPENED_EVENT = threading.Event()

def _handle_signal(signum, frame):
    try:
        log.info("received signal %s – exiting", signum)
    except Exception:
        pass
    STOP_EVENT.set()
    # Give any active TTS a moment to stop cleanly
    if ACTIVE_TTS.is_set():
        try:
            log.info("stopping active TTS…")
            waited = 0.0
            while ACTIVE_TTS.is_set() and waited < 3.0:
                time.sleep(0.05)
                waited += 0.05
        except Exception:
            pass
    try:
        sys.exit(0)
    except SystemExit:
        raise

def _setup_signal_handlers():
    for sig in (getattr(signal, "SIGINT", None),
                getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGTSTP", None),
                getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

_setup_signal_handlers()

# ───────────── Reused API clients (lazy init) ─────────────
SPEECH_CLIENT = None   # type: ignore
TTS_CLIENT    = None   # type: ignore
INTRO_LOGGED  = False  # per-run guard: log intro only once per process
EXIT_ON_NO_MIC = os.getenv("WAILO_EXIT_ON_NO_MIC", "1").lower() in {"1", "true", "yes"}

# ───────────── timing decorator ─────────────
def log_duration(label):
    def decorator(func):
        def wrapper(*args, **kwargs):
            start = time.time()
            result = func(*args, **kwargs)
            duration = time.time() - start
            log.info(f"⏱️ {label} took {duration:.2f} seconds")
            return result
        return wrapper
    return decorator

# ───────────── helpers ─────────────
def iso639(lang: str | None) -> str | None:
    if not lang: return None
    lang = lang.strip().lower()
    return lang[:2] if lang[:2].isalpha() else None

# Remove emojis and non-speech glyphs for TTS playback
EMOJI_RE = re.compile("[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF\U00002600-\U000026FF\U00002B00-\U00002BFF]+", re.UNICODE)

def sanitize_tts_text(text: str) -> str:
    if not text:
        return text
    # strip emoji variation selector and ZWJ
    text = text.replace("\uFE0F", "").replace("\u200D", "")
    # strip bidi isolate marks if present
    text = re.sub(r"[\u2066-\u2069]", "", text)
    # remove emojis/pictographs
    text = EMOJI_RE.sub("", text)
    # collapse whitespace
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text

def estimated_speech_secs(text: str) -> float:
    """Conservative estimate for TTS/voice length based on characters.
    ~9 chars/sec plus 4s overhead, min 12s, max 45s.
    """
    try:
        est = (len(text) / 9.0) + 4.0
        return max(12.0, min(45.0, est))
    except Exception:
        return 20.0

"""
User-facing alerts (non-intrusive, synced with flow)
We use beeps and logs to avoid overlapping speech with main TTS.
"""
# Thresholds (seconds)
STT_WARN_SEC          = float(os.getenv("WAILO_STT_WARN_SEC", "3"))
TTS_START_WARN_SEC    = float(os.getenv("WAILO_TTS_START_WARN_SEC", "2"))
LLM_WARN_SEC          = float(os.getenv("WAILO_LLM_WARN_SEC", "5"))
MIC_OPEN_WARN_SEC     = float(os.getenv("WAILO_MIC_OPEN_WARN_SEC", "3"))
OFFLINE_ANNOUNCE_SEC  = float(os.getenv("WAILO_OFFLINE_ANNOUNCE_SEC", "5"))
NO_SPEECH_REPEATS_MAX = int(os.getenv("WAILO_NO_SPEECH_REPEATS", "3"))
ALERT_BEEPS           = os.getenv("WAILO_ALERT_BEEPS", "1").lower() in {"1","true","yes"}

def _beep():
    if not ALERT_BEEPS:
        return
    # Avoid overlapping with main TTS audio
    if ACTIVE_TTS.is_set():
        return
    try:
        rate = 16_000
        dur_ms = 120
        freq = 880
        t = np.linspace(0, dur_ms/1000.0, int(rate * (dur_ms/1000.0)), False)
        tone = (np.sin(2*np.pi*freq*t) * 0.2).astype(np.float32)
        sd.play(tone, samplerate=rate, blocking=True)
    except Exception:
        try:
            print("\a", end="", flush=True)  # fallback console bell
        except Exception:
            pass

def _alert(msg: str):
    try:
        log.info("[ALERT] %s", msg)
    except Exception:
        print(msg)
    _beep()

def _warn_after(delay_sec: float, cancel_event: threading.Event, msg: str):
    def worker():
        waited = 0.0
        step = 0.05
        while waited < delay_sec and not STOP_EVENT.is_set() and not cancel_event.is_set():
            time.sleep(step); waited += step
        if not STOP_EVENT.is_set() and not cancel_event.is_set():
            _alert(msg)
    threading.Thread(target=worker, daemon=True).start()

def _network_monitor():
    last_online = None
    offline_since = None
    while not STOP_EVENT.is_set():
        online = net_up()
        now = time.time()
        if last_online is None:
            last_online = online
            offline_since = None if online else now
        else:
            if online and last_online is False:
                _alert("I'm back online.")
                offline_since = None
            elif not online and last_online is True:
                offline_since = now
            if not online and offline_since and now - offline_since >= OFFLINE_ANNOUNCE_SEC:
                _alert("I'm offline. Retrying…")
                offline_since = None
            last_online = online
        time.sleep(1.0)

def net_up(host="8.8.8.8", port=53, timeout=2.0) -> bool:
    try:
        socket.create_connection((host, port), timeout)
        return True
    except OSError:
        return False

# ───────────── backend init ─────────────
api       = WailoAPI()
toy_info  = api.toy_info().get("data", {})
child     = api.child_profile()
toy_name  = (child.get("toyname") or "Wailo").strip()
lang_code = iso639(child.get("language")) or "en"

def get_persistent_mac():
    """Get the same persistent MAC address used by wailo_gatt_server.py"""
    MAC_FILE = '/home/orangepi/Waylo_AI/.device_mac_address'
    
    # Try to read from persistent storage first
    if os.path.exists(MAC_FILE):
        try:
            with open(MAC_FILE, 'r') as f:
                stored_mac = f.read().strip()
                if stored_mac and len(stored_mac) == 17:  # Valid MAC format XX:XX:XX:XX:XX:XX
                    log.info(f"📱 Using persistent MAC address: {stored_mac}")
                    return stored_mac
        except Exception as e:
            log.warning(f"Failed to read persistent MAC: {e}")
    
    # Fallback to API or default
    toy_mac_raw = (toy_info.get("mac_address") or "").strip().upper()
    if toy_mac_raw and len(toy_mac_raw) == 17:
        log.info(f"📱 Using API MAC address: {toy_mac_raw}")
        return toy_mac_raw
    
    # Last resort default
    default_mac = "00:00:00:00:00:00"
    log.warning(f"📱 Using default MAC address: {default_mac}")
    return default_mac

# Get consistent MAC address
toy_mac_raw = get_persistent_mac()
toy_mac_id  = toy_mac_raw.replace(":", "") or "UNKNOWN"

log.info("🎯  toy_info   → %s", toy_info)
log.info("👶  child_info → %s", child)

BASE_PERSONA = (
    f"You are {toy_name}, a friendly AI pet who loves helping children learn. "
    "Explain things simply, be enthusiastic and encouraging."
)
SYSTEM_MSG = (
    f"{BASE_PERSONA}\n\n"
    f"Your name is {toy_name}. "
    f"You are talking to {child.get('name','friend')}, a {child.get('age','6')}-year-old {child.get('gender','child')}. "
    f"Speak in {lang_code.upper()} and stay extremely kid-friendly."
    f"Keep the answer short(1-3 sentences at max)"
)

# ───────────── parental-controls polling ─────────────
PARENTAL, LOCK = {}, threading.Lock()

def pull_parental():
    try:
        data = api.parental_controls().get("data", {})
        with LOCK:
            PARENTAL.clear()
            PARENTAL.update(data)
    except Exception as e:
        log.warning("parental fetch error: %s", e)

def par_loop():
    while not STOP_EVENT.is_set():
        pull_parental()
        time.sleep(PAR_POLL_SEC)

threading.Thread(target=par_loop, daemon=True).start()

def silenced() -> bool:
    with LOCK:
        pc = PARENTAL.copy()
    now = datetime.now()
    if not pc or pc.get("DND") is False:
        return False
    if pc.get("DND"):
        return True
    for entry in pc.get("schedule", []):
        try:
            if parser.parse(entry["startTime"]) <= now <= parser.parse(entry["endTime"]):
                return False
        except Exception:
            pass
    tl = pc.get("timeLimit") or {}
    try:
        st = dtime(int(tl["startHour"]), int(tl["startMinute"]))
        et = dtime(int(tl["endHour"]), int(tl["endMinute"]))
        if st <= now.time() <= et:
            return False
    except Exception:
        pass
    return True

# ───────────── metadata heartbeat ─────────────
def meta_loop():
    while True:
        status = "connected" if net_up() else "disconnected"
        try:
            api.update_metadata(
                battery="100%",
                board_name=platform.node() or "Unknown",
                connection_status=status)
        except Exception as e:
            log.warning("metadata push error: %s", e)
        time.sleep(META_PUSH_SEC)

threading.Thread(target=meta_loop, daemon=True).start()

# ───────────── audio / whisper / tts ─────────────
# Device pinning via env to avoid backend renegotiation
def _parse_dev_env(v: str | None):
    if v is None:
        return None
    v = v.strip()
    try:
        return int(v)
    except Exception:
        return v

IN_DEV_ENV  = os.getenv("WAILO_INPUT_DEVICE")
OUT_DEV_ENV = os.getenv("WAILO_OUTPUT_DEVICE")
IN_DEV      = _parse_dev_env(IN_DEV_ENV)
OUT_DEV     = _parse_dev_env(OUT_DEV_ENV)

def _apply_default_devices():
    try:
        sd.default.device = (
            IN_DEV if IN_DEV is not None else AUDIO_DEVICE[0],
            OUT_DEV if OUT_DEV is not None else AUDIO_DEVICE[1],
        )
    except Exception as e:
        log.warning("device pinning failed: %s", e)
_apply_default_devices()

# Optional: auto-select ES8388 if present and no env provided
if IN_DEV is None or OUT_DEV is None:
    try:
        devices = sd.query_devices()
        es8388_in  = next((i for i,d in enumerate(devices) if 'ES8388' in d['name'] and d['max_input_channels']>0), None)
        es8388_out = next((i for i,d in enumerate(devices) if 'ES8388' in d['name'] and d['max_output_channels']>0), None)
        if (IN_DEV is None and es8388_in is not None) or (OUT_DEV is None and es8388_out is not None):
            if IN_DEV is None:
                IN_DEV = es8388_in
            if OUT_DEV is None:
                OUT_DEV = es8388_out
            sd.default.device = (IN_DEV, OUT_DEV)
            log.info("auto-selected devices → in=%s out=%s", IN_DEV, OUT_DEV)
    except Exception:
        pass

# Start background network monitor for online/offline alerts
threading.Thread(target=_network_monitor, daemon=True).start()

# ───────────── intro gating (per run; can skip via env) ─────────────
def should_intro() -> bool:
    # Skip intro if operator requests it
    if os.getenv("WAILO_SKIP_INTRO", "").lower() in {"1", "true", "yes"}:
        return False
    return True

# ───────────── intro log throttle across processes ─────────────
INTRO_THROTTLE_SEC  = int(os.getenv("WAILO_INTRO_THROTTLE_SEC", "120"))
INTRO_THROTTLE_FILE = Path(f"/tmp/wailo_intro_throttle_{toy_mac_id}.stamp")

# ───────────── audio capture with detailed logging ─────────────
@log_duration("Mic record")
def record(max_sec: int = 60, silence: float = 0.8) -> bytes | None:
    """
    Records up to `max_sec` s (16-kHz, mono).
    Stops when `silence` seconds of quiet follow speech.
    Emits INFO logs for mic-ready, first speech frame, and stop event.
    Returns raw PCM bytes or None.
    """
    vad      = webrtcvad.Vad(2)
    chunk    = int(0.03 * SR)           # 30 ms
    limit    = int(max_sec / 0.03)
    buf      = []
    spoken   = False
    silent   = 0
    start_t  = time.time()

    # Prefer low-latency input to reduce post-TTS wait on some ARM boards
    blocksize_frames = int(os.getenv("WAILO_INPUT_BLOCKSIZE", "240"))  # ~15ms at 16kHz
    latency_pref = os.getenv("WAILO_INPUT_LATENCY", "low")             # "low" or "high"

    with sd.InputStream(
        samplerate=SR,
        channels=1,
        dtype="int16",
        blocksize=blocksize_frames,
        latency=latency_pref,
    ) as s:
        for _ in range(3):               # pre-roll flush
            s.read(chunk)

        log.info("🎤  Mic open – waiting for speech…")

        for _ in range(limit):
            frame, _ = s.read(chunk)
            buf.append(frame)

            if vad.is_speech(frame.tobytes(), SR):
                if not spoken:
                    log.info("🗣️  Speech detected (%.2f s)", time.time() - start_t)
                spoken, silent = True, 0
            elif spoken:
                silent += 1

            if spoken and silent * 0.03 >= silence:
                log.info("🔇 Silence %.1fs – stopping", silence)
                break
        else:
            log.info("⏱️ Max record time reached (%.0fs)", max_sec)

    if not spoken:
        log.warning("🎙️ No speech captured")
        return None

    log.info("🎧 Recording finished – %.2f s captured", time.time() - start_t)
    return np.concatenate(buf).tobytes()


# ───────────── Google Cloud Speech-to-Text ─────────────
@log_duration("STT (Google)")
def transcribe(raw: bytes) -> str | None:
    """
    Offline call to Google Cloud Speech-to-Text.
    Requires:
      • pip install google-cloud-speech
      • $GOOGLE_APPLICATION_CREDENTIALS pointing to JSON key.
    """
    from google.api_core import exceptions as gexc

    # Reuse a single SpeechClient across turns
    global SPEECH_CLIENT
    if SPEECH_CLIENT is None:
        SPEECH_CLIENT = speech.SpeechClient()          # creds from env
    client = SPEECH_CLIENT

    # Convert raw PCM → 16-kHz mono WAV bytes
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(raw)

        tmp.flush()
        tmp.seek(0)
        wav_bytes = tmp.read()
    os.unlink(tmp.name)

    if len(wav_bytes) < 100:
        log.warning("WAV payload empty – probable silence")
        return None

    audio  = speech.RecognitionAudio(content=wav_bytes)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SR,
        language_code=lang_code or "en-US",
        enable_automatic_punctuation=True,
    )

    try:
        resp = client.recognize(config=config, audio=audio)
    except gexc.GoogleAPIError as e:
        log.warning("Google STT API error: %s", e)
        return None

    if not resp.results:
        log.warning("Google STT: no match")
        return None

    return resp.results[0].alternatives[0].transcript.strip()

# --- streaming_speak -------------------------------------------------
from google.cloud import texttospeech_v1beta1 as tts_b1

STREAM_RATE     = 24_000          # Chirp-3 voices are fixed to 24 kHz
CHUNK_CHARS     = 150             # ~100 ms of speech per request
FRAMES_PER_BUF  = 2048            # playback buffer size (frames)
PREBUFFER_MS    = int(os.getenv("WAILO_TTS_PREBUFFER_MS", "80"))  # prebuffer (ms)

# Control whether we wait for TTS to finish before listening again
LISTEN_AFTER_TTS   = os.getenv("WAILO_LISTEN_AFTER_TTS", "1").lower() in {"1", "true", "yes"}
LISTEN_WAIT_SEC    = float(os.getenv("WAILO_LISTEN_WAIT_SEC", "10"))

def streaming_speak(text: str, voice_name="en-US-Chirp3-HD-Charon"):
    """
    Low-latency TTS using Cloud Text-to-Speech bidirectional streaming.
    Works only with Chirp-3 (HD / LP) voices.
    """
    original_text = text
    text = sanitize_tts_text(text)
    if text != original_text:
        try:
            log.info("TTS sanitized %d→%d chars (emojis removed)", len(original_text), len(text))
        except Exception:
            pass
    # Reuse a single TextToSpeechClient across turns
    global TTS_CLIENT
    if TTS_CLIENT is None:
        TTS_CLIENT = tts_b1.TextToSpeechClient()
    client = TTS_CLIENT

    # 1) Streaming config – **no audio_config field!**
    stream_cfg = tts_b1.StreamingSynthesizeConfig(
        voice=tts_b1.VoiceSelectionParams(
            name=voice_name,
            language_code=voice_name.split("-")[0] + "-" + voice_name.split("-")[1],
        )
    )

    # 2) Generator that sends the config once, then text chunks
    def req_gen():
        yield tts_b1.StreamingSynthesizeRequest(streaming_config=stream_cfg)
        for i in range(0, len(text), CHUNK_CHARS):
            yield tts_b1.StreamingSynthesizeRequest(
                input=tts_b1.StreamingSynthesisInput(text=text[i:i+CHUNK_CHARS])
            )

    # 3) SoundDevice raw sink with small prebuffer to avoid underruns
    try:
        out = sd.RawOutputStream(
            samplerate=STREAM_RATE,
            channels=1,
            dtype='int16',
            blocksize=FRAMES_PER_BUF,
            latency='low',
            device=(IN_DEV, OUT_DEV) if (IN_DEV is not None or OUT_DEV is not None) else None,
        )
        out.start()
    except Exception as e:
        log.warning("Output stream open failed: %s; disabling speech", e)
        return

    # Collect a short prebuffer before first write
    prebuffer_bytes_needed = int(PREBUFFER_MS * STREAM_RATE * 2 / 1000)  # 2 bytes per sample
    buffer = bytearray()
    started = False

    from google.api_core import exceptions as gexc

    def _compute_timeout(t: str) -> float:
        # More conservative: ~9 chars/sec + 4s overhead, min 12s, max 45s
        return estimated_speech_secs(t)

    tts_timeout_env = os.getenv("WAILO_TTS_TIMEOUT_SEC")
    tts_timeout = float(tts_timeout_env) if tts_timeout_env else _compute_timeout(text)

    try:
        ACTIVE_TTS.set()
        log.info("🔊 TTS start (len=%d, prebuffer=%dms, buf=%d)", len(text), PREBUFFER_MS, FRAMES_PER_BUF)
        rsp_iter = client.streaming_synthesize(requests=req_gen(), timeout=tts_timeout)
        for rsp in rsp_iter:
            if STOP_EVENT.is_set():
                log.info("TTS cancelled by signal")
                break
            if not rsp.audio_content:
                continue
            buffer.extend(rsp.audio_content)
            # Start playback once enough data is buffered
            if not started:
                if len(buffer) < prebuffer_bytes_needed:
                    continue
                started = True
                log.info("🔊 TTS playback start (buffered=%d bytes)", len(buffer))

            # Write in chunks aligned to frames_per_buffer
            chunk_bytes = FRAMES_PER_BUF * 2
            wrote_any = False
            while len(buffer) >= chunk_bytes:
                if STOP_EVENT.is_set():
                    break
                to_write = bytes(buffer[:chunk_bytes])
                del buffer[:chunk_bytes]
                try:
                    out.write(to_write)
                except Exception as e:
                    # Ignore intermittent underruns; continue writing
                    log.warning("audio write underrun: %s", e)
                    continue
                wrote_any = True
            # Yield briefly to allow mic stream setup sooner
            if wrote_any:
                time.sleep(0.002)

        # Flush remaining buffer
        while len(buffer) > 0:
            if STOP_EVENT.is_set():
                break
            chunk_bytes = FRAMES_PER_BUF * 2
            n = min(len(buffer), chunk_bytes)
            to_write = bytes(buffer[:n])
            del buffer[:n]
            try:
                out.write(to_write)
            except Exception as e:
                log.warning("audio write underrun (flush): %s", e)
                break
            time.sleep(0.002)
    except gexc.DeadlineExceeded:
        log.warning("TTS streaming timed out after %.1fs; flushing buffer", tts_timeout)
    except Exception as e:
        log.warning("TTS streaming error: %s", e)
    finally:
        try:
            out.stop()
            out.close()
        except Exception:
            pass
        log.info("🔊 TTS done")
        ACTIVE_TTS.clear()
# --------------------------------------------------------------------

                
@log_duration("GPT Chat Completion")
def chat_gpt(prompt: str) -> str:
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=125,
            messages=[
                {"role": "system", "content": SYSTEM_MSG},
                {"role": "user",   "content": prompt},
            ])
        return r.choices[0].message.content.strip()
    except Exception as e:
        log.error("GPT error: %s", e)
        return "Sorry, something went wrong."

# ───────────── async combined analytics (sentiment + interest) ─────────────
def analyze_and_log_async(utter: str, req_id: str | None, res_id: str | None) -> None:
    """Fire-and-forget analytics call that extracts sentiment and interest
    in a single OpenAI request, then logs to backend. Non-blocking.
    """
    def worker():
        try:
            rsp = client.chat.completions.create(
                model="gpt-3.5-turbo",
                temperature=0.3,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return ONLY JSON with keys: "
                            "{\"sentiment\":\"positive|neutral|negative\", "
                            "\"sentiment_intensity\": <0-10 float>, "
                            "\"interest\": \"<topic (<=2 words)>\", "
                            "\"interest_intensity\": <0-10 float>}"
                        ),
                    },
                    {"role": "user", "content": utter},
                ],
            )
            raw = rsp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].strip()
            data = json.loads(raw)

            sentiment = str(data.get("sentiment", "")).lower().strip()
            sent_intensity = float(data.get("sentiment_intensity", 0.0))
            topic = str(data.get("interest", "")).strip()
            topic_intensity = float(data.get("interest_intensity", 0.0))

            if sentiment in {"positive", "neutral", "negative"}:
                try:
                    api.log_sentiment(utter, sentiment, sent_intensity, req_id=req_id, res_id=res_id)
                except Exception as e:
                    log.warning("sentiment log failed: %s", e)

            if topic:
                try:
                    api.log_interest(topic, topic_intensity, req_id=req_id, res_id=res_id)
                    log.info("💡 interest → %s (%.1f)", topic, topic_intensity)
                except Exception as e:
                    log.warning("interest log failed: %s", e)
        except Exception as e:
            log.warning("analytics error: %s", e)

    threading.Thread(target=worker, daemon=True).start()

# ───────────── main loop ─────────────
def main():
    global INTRO_LOGGED
    if not silenced() and should_intro():
        # Throttle intro logging across crashes/restarts
        now = time.time()
        allow_intro_log = not INTRO_LOGGED
        try:
            if INTRO_THROTTLE_FILE.exists():
                last = INTRO_THROTTLE_FILE.stat().st_mtime
                if now - last < INTRO_THROTTLE_SEC:
                    allow_intro_log = False
            INTRO_THROTTLE_FILE.touch()
        except Exception:
            pass

        intro = chat_gpt("Introduce yourself to the child.")
        if allow_intro_log:
            req_id = api.log_request("Introduce yourself to the child.")
            res_id = api.log_response(intro, req_id=req_id)
            INTRO_LOGGED = True

        # Speak intro in background; don't block startup indefinitely
        intro_done = threading.Event()

        def _intro_worker():
            try:
                streaming_speak(intro)
            finally:
                intro_done.set()

        threading.Thread(target=_intro_worker, daemon=True).start()

        max_wait = float(os.getenv("WAILO_INTRO_MAX_WAIT_SEC", "10"))
        # warn if TTS start is slow
        _warn_after(TTS_START_WARN_SEC, intro_done, "Having trouble speaking; please wait…")
        if not intro_done.wait(timeout=max_wait):
            log.warning("⏳ Intro playback exceeded %.1fs; continuing", max_wait)
        log.info("✅ Intro completed. Entering main loop...")

    first_mic_attempt = True

    while True:
        if STOP_EVENT.is_set():
            break
        if silenced():
            log.info("🔇 Silenced by parental controls. Retrying in 15s...")
            time.sleep(15)
            continue

        # Mic readiness cue if opening takes too long
        mic_ready_cancel = threading.Event()
        def mic_ready_worker():
            time.sleep(MIC_OPEN_WARN_SEC)
            if not STOP_EVENT.is_set() and not mic_ready_cancel.is_set():
                _alert("I'm ready.")
        threading.Thread(target=mic_ready_worker, daemon=True).start()

        raw = record()
        mic_ready_cancel.set()
        if not raw:
            # If first capture after startup has no speech, exit to avoid bad loops
            if first_mic_attempt and EXIT_ON_NO_MIC:
                log.error("No mic input detected on first attempt; exiting")
                sys.exit(1)
            log.warning("🎙️ No audio detected")
            # repeated no-speech prompt
            try:
                main.no_speech_streak += 1  # type: ignore[attr-defined]
            except Exception:
                main.no_speech_streak = 1   # type: ignore[attr-defined]
            if main.no_speech_streak >= NO_SPEECH_REPEATS_MAX:  # type: ignore[attr-defined]
                _alert("Please speak a little louder.")
                main.no_speech_streak = 0   # type: ignore[attr-defined]
            continue

        first_mic_attempt = False
        utter = transcribe(raw)
        if not utter:
            log.warning("❌ Transcription failed or returned empty")
            _alert("Could you repeat that?")
            continue

        log.info("🗣️ %s", utter)

        req_id = api.log_request(utter)

        # LLM thinking warning if slow
        llm_cancel = threading.Event()
        _warn_after(LLM_WARN_SEC, llm_cancel, "Thinking…")
        reply = chat_gpt(utter)
        llm_cancel.set()

        # Start speaking immediately in a background thread to avoid blocking the loop
        spoken = {"done": False}
        def speak_worker():
            try:
                streaming_speak(reply)
            finally:
                spoken["done"] = True
        threading.Thread(target=speak_worker, daemon=True).start()

        # Log response while audio plays
        res_id = api.log_response(reply, req_id=req_id)

        # Fire-and-forget combined analytics
        analyze_and_log_async(utter, req_id, res_id)

        # (speech already played above)

        # Optionally wait for TTS to finish before opening the mic again.
        # Some SBC audio stacks behave half-duplex; overlapping output/input can mute output.
        if LISTEN_AFTER_TTS:
            waited = 0.0
            while not spoken["done"] and waited < LISTEN_WAIT_SEC:
                time.sleep(0.05)
                waited += 0.05
            if not spoken["done"]:
                log.warning("Waiting for TTS exceeded %.1fs; continuing", LISTEN_WAIT_SEC)
        # If ALSA/PortAudio needs a moment to release output before input, add a micro gap
        time.sleep(0.05)

        if utter.lower().strip() in {"exit", "quit", "bye"}:
            # Attempt graceful stop after speaking goodbye
            streaming_speak("Goodbye!")
            break

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye-bye!")
