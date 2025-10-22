#!/usr/bin/env python3
# interest_tracker.py – extract one “dominant” interest topic

import json, langdetect
from typing import Tuple, Optional
from openai import OpenAI
from config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)

def _lang(txt:str)->str:
    try:  return langdetect.detect_langs(txt)[0].lang
    except: return "en"

def track_interest(txt:str) -> Optional[Tuple[str,float]]:
    if not txt.strip(): return None
    if _lang(txt) not in ("en","de"): return None

    rsp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        temperature=0.3,
        messages=[
            {"role":"system","content":
             "Return ONLY JSON → {\"interest\":\"<topic>\",\"intensity\":<0-1>}.\n"
             "Topic must be max two words, most central idea."},
            {"role":"user","content": txt},
        ],
    )
    raw = rsp.choices[0].message.content.strip()
    if raw.startswith("```"): raw = raw.split("```")[1].strip()
    data = json.loads(raw)
    return data["interest"], round(float(data["intensity"]) * 10, 1)
