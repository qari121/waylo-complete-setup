#!/usr/bin/env python3
# sentiment_tracker.py – robust sentiment + intensity (0-10)

import json, langdetect, logging
from enum import Enum
from typing import Tuple, Optional
from openai import OpenAI
from config import OPENAI_API_KEY

log     = logging.getLogger("sentiment")
client  = OpenAI(api_key=OPENAI_API_KEY)

class Sentiment(Enum):
    POSITIVE = "positive"
    NEUTRAL  = "neutral"
    NEGATIVE = "negative"

# ───────────────── helper ─────────────────
def _detect_lang(txt: str) -> str:
    if not txt or len(txt.split()) < 3:
        return "en"
    try:
        langs = langdetect.detect_langs(txt)
        return langs[0].lang if langs[0].prob > .85 else "en"
    except Exception:
        return "en"

# ───────────────── core ───────────────────
def _analyze(txt: str) -> Optional[Tuple[Sentiment, float]]:
    lang = _detect_lang(txt)
    if lang not in ("en", "de"):
        return None                              # unsupported language → skip

    rsp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        temperature=0.3,
        messages=[
            {"role":"system","content":
               "Return ONLY JSON: "
               "{\"sentiment\":\"positive|neutral|negative\","
               " \"intensity\": <0-1 float>}"},
            {"role":"user","content": txt},
        ],
    )

    raw = rsp.choices[0].message.content.strip()
    if raw.startswith("```"):                   # strip ```json … ```
        raw = raw.split("```")[1].strip()

    try:
        data = json.loads(raw)
        sent = Sentiment(data["sentiment"])
        inten = round(float(data["intensity"]) * 10, 1)
        return sent, inten                      # success
    except Exception as e:
        log.warning("⚠️  sentiment JSON parse failed: %s | raw=%r", e, raw)
        return None                             # let caller ignore

# ───────────────── public API ──────────────
def track_sentiment(txt: str) -> Optional[Tuple[Sentiment, float]]:
    """Return (Sentiment, 0-10) or None on failure."""
    try:
        return _analyze(txt)
    except Exception as e:
        log.warning("⚠️  sentiment GPT error: %s", e)
        return None
