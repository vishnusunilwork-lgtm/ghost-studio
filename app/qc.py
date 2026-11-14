"""Quality check: a local vision model looks at each picture for bad hands, eyes and faces."""
from __future__ import annotations

import io
from pathlib import Path

from . import llm

VISION_HINTS = ("vl", "llava", "vision", "gemma3")


def vision_model() -> str:
    for m in llm.installed():
        if any(h in m for h in VISION_HINTS):
            return m
    return ""


def available() -> bool:
    return bool(vision_model())


def check(image: Path) -> dict:
    """Returns {"hands":1-10,"eyes":1-10,"face":1-10,"ok":bool,"problem":"..."}; ok=True if it looks clean."""
    from PIL import Image
    im = Image.open(image).convert("RGB")
    im.thumbnail((896, 896))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    r = llm.ask_json(vision_model(), """You are a strict photo retoucher checking an AI-generated photo for defects.
Look closely at every visible hand (finger count, fused or bent fingers, extra hands), the eyes (cross-eyed, uneven, smudged irises)
and the face (melted, asymmetric, plastic skin). If no hands are visible, hands = 10. If no face is visible, eyes = 10 and face = 10.
Return JSON only: {"hands": 1-10, "eyes": 1-10, "face": 1-10, "problem": "short description or empty"}""", images=[b.getvalue()], temperature=0.1)
    scores = [int(r.get(k, 10) or 10) for k in ("hands", "eyes", "face")]
    r["score"] = min(scores)
    r["ok"] = r["score"] >= 6
    return r
