"""Performance ad mode: ads built to sell, ready to upload to Meta or Google.

  1 Brief      product facts, audience, offer; every claim marked approved (it is in your material) or blocked
  2 Ideas      4 angles (problem→solution, product demo, offer-first, lifestyle), scored 0-5 on 5 rules; top 2 go on
  3 Script     15 s each: 0-2 hook · 2-5 problem · 5-11 product in action · 11-15 offer + call to action
  4 Pictures   one still per shot, your product photos as references (they are also the static ads)
  5 Video      first frame = the still; one frame a second is checked; only failed shots are re-made
  6 Finishing  logo, text in your font and colours, disclaimer, end slate, 4 sizes, even loudness: BY SCRIPT,
               never by the AI, so it is exact and the same every time
  7 Check      size, length, frame rate, file size, safe zones, logo present, every word = the approved copy,
               no banned words, disclaimer present
This module holds the logic that doesn't need the picture/video engines (so it can be tested on its own);
pipeline.Runner._perf runs the stages in order.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import urllib.request
from pathlib import Path

from .editor import ffmpeg_bin, probe

FORMATS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "1.91:1": (1200, 628)}
# Areas the app's own buttons and captions cover (fractions of the frame): text and logo stay out of them
SAFE = {"9:16": {"top": 0.14, "bottom": 0.35, "side": 0.06},  # Reels / Stories / Shorts
        "1:1": {"top": 0.05, "bottom": 0.10, "side": 0.05},
        "16:9": {"top": 0.08, "bottom": 0.18, "side": 0.05},  # YouTube player bar
        "1.91:1": {"top": 0.06, "bottom": 0.10, "side": 0.05}}
BEATS = [("hook", 0, 2), ("problem", 2, 5), ("product", 5, 8), ("product", 8, 11), ("offer", 11, 15)]
ANGLES = ["problem_solution", "product_demo", "offer_first", "lifestyle"]
ANGLE_NAME = {"problem_solution": "Problem → solution", "product_demo": "Product demo", "offer_first": "Offer first", "lifestyle": "Lifestyle"}
RULES = ["hook", "one_message", "product_on_screen", "policy_safe", "fits_market"]
# Words ad platforms reject or that need proof (Meta / Google ad policies, Indian ASCI guidelines)
BANNED = ["guaranteed", "guarantee", "100% cure", "cure", "cures", "miracle", "risk-free", "risk free", "no side effects",
          "best in the world", "number 1", "no. 1", "#1", "lose weight fast", "get rich", "free money", "instant results",
          "doctor recommended", "clinically proven", "permanent", "magic"]
LANG_FONTS = {  # free fonts that draw each script correctly (SIL Open Font License)
    "latin": "NotoSans-Bold.ttf", "Devanagari": "NotoSansDevanagari-Bold.ttf", "Malayalam": "NotoSansMalayalam-Bold.ttf",
    "Tamil": "NotoSansTamil-Bold.ttf", "Telugu": "NotoSansTelugu-Bold.ttf", "Kannada": "NotoSansKannada-Bold.ttf",
    "Bengali": "NotoSansBengali-Bold.ttf", "Gujarati": "NotoSansGujarati-Bold.ttf", "Gurmukhi": "NotoSansGurmukhi-Bold.ttf"}
SCRIPT_RANGES = [("Devanagari", 0x0900, 0x097F), ("Bengali", 0x0980, 0x09FF), ("Gurmukhi", 0x0A00, 0x0A7F),
                 ("Gujarati", 0x0A80, 0x0AFF), ("Tamil", 0x0B80, 0x0BFF), ("Telugu", 0x0C00, 0x0C7F),
                 ("Kannada", 0x0C80, 0x0CFF), ("Malayalam", 0x0D00, 0x0D7F)]
FONT_URL = "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/{fam}/hinted/ttf/{file}"


# ---------------------------------------------------------------- 1 brief
def fetch_page(url: str, limit: int = 12000) -> str:
    """The visible text of a landing page (the URL you gave)."""
    if not re.match(r"^https?://", url or ""):
        return ""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GhostStudio"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", errors="ignore")
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    return ((html.unescape(title.group(1)).strip() + ". ") if title else "") + text[:limit]


def _words(t: str) -> list[str]:
    return [w.strip(".") for w in re.findall(r"[\w₹%.]+", (t or "").lower()) if w.strip(".")]


STOP = set("a an the and or of for to in on with your you our we is are be it this that at by from as all any more most "
           "very can will just now get new only".split())


def claim_ok(claim: str, source: str) -> bool:
    """Approved only if it's in your material: every number must appear there, and most of its words."""
    src = (source or "").lower()
    src_words = set(_words(src))
    nums = re.findall(r"\d+(?:[.,]\d+)?", claim or "")
    src_nums = set(re.findall(r"\d+(?:[.,]\d+)?", src))
    if any(n not in src_nums for n in nums):  # whole numbers only: "1" is not in "199"
        return False
    words = [w for w in _words(claim) if w not in STOP and len(w) > 2 and not re.fullmatch(r"\d+(?:[.,]\d+)?", w)]
    if not words:
        return bool(nums)
    found = sum(1 for w in words if w in src_words or any(w[:5] == s[:5] for s in src_words if len(s) > 4 and len(w) > 4))
    return found / len(words) >= 0.7


def banned_in(text: str) -> list[str]:
    t = " " + (text or "").lower() + " "
    return [b for b in BANNED if re.search(r"(?<![\w])" + re.escape(b) + r"(?![\w])", t)]


def brief_prompt(idea: str, source: str, language: str, platform: str) -> str:
    return f"""You prepare a performance-ad brief. Use ONLY the material below; never invent facts, prices or numbers.
MATERIAL (from the person: idea, landing page, notes):
\"\"\"{(idea + chr(10) + source)[:9000]}\"\"\"
Market: India by default. Ad language: {language}. Platform: {platform}.
Answer JSON:
{{"product": {{"name": "", "what": "one line: what it does", "benefits": ["", "", ""], "offer": "price or offer exactly as written, or empty"}},
 "audience": {{"who": "", "problem": "their main problem", "market": "India / {language}"}},
 "claims": ["each factual claim the ad could make, copied from the material"],
 "cta": "a 2-4 word call to action in {language}, e.g. Shop now / Download now",
 "missing": ["only essential facts that are truly missing: product name, what it does, or the offer"]}}"""


def fallback_brief(idea: str, source: str, language: str) -> dict:
    name = (re.search(r"(?i)\b(?:for|of|called|named)\s+([A-Z][\w-]+(?:\s[A-Z][\w-]+)?)", idea) or [None, ""])[1] or \
        (source.split(".")[0][:40] if source else "") or "Your product"
    offer = (re.search(r"(?i)((?:₹|rs\.?|inr)\s?\d[\d,]*[^.\n]{0,40}|\d+%\s*off[^.\n]{0,30}|free [^.\n]{0,30})", idea + " " + source) or [None, ""])[1]
    sents = [s.strip() for s in re.split(r"[.!\n]", idea + ". " + source) if 3 <= len(s.split()) <= 18
             and not re.match(r"(?i)\s*(an? )?(ad|ads|advert\w*|make|create|generate|promote|video|reel)\b", s.strip())][:6]
    return {"product": {"name": name, "what": idea[:120], "benefits": sents[:3] or [idea[:80]], "offer": offer.strip()},
            "audience": {"who": "everyday Indian customers", "problem": "", "market": f"India / {language}"},
            "claims": sents[:6], "cta": "Shop now" if language == "English" else "अभी खरीदें" if language == "Hindi" else "Shop now", "missing": []}


def finish_brief(b: dict, source: str, extra: dict) -> dict:
    """Mark claims, apply the person's own offer / CTA / disclaimer, add the platform rules."""
    b.setdefault("product", {})
    b.setdefault("audience", {})
    if extra.get("offer"):
        b["product"]["offer"] = extra["offer"]
    if extra.get("cta"):
        b["cta"] = extra["cta"]
    if extra.get("name"):
        b["product"]["name"] = extra["name"]
    full = source + " " + (extra.get("offer") or "") + " " + (extra.get("name") or "")
    cl = []
    for c in [*(b.get("claims") or []), *(b["product"].get("benefits") or [])]:
        c = str(c).strip()
        if c and c not in [x["text"] for x in cl]:
            ok, bad = claim_ok(c, full), banned_in(c)
            cl.append({"text": c, "status": "approved" if ok and not bad else "blocked",
                       "why": "" if ok and not bad else ("uses " + ", ".join(bad) if bad else "not in your material")})
    b["claims"] = cl
    b["disclaimer"] = extra.get("disclaimer", "")
    b["platform"] = extra.get("platform") or "both"
    b["rules"] = {"meta": "Reels/Stories: keep text out of the top 14% and bottom 35%. Feed 1:1 and 1.91:1. 15 s works best.",
                  "google": "YouTube / Demand Gen: 16:9, 1:1, 9:16; keep the bottom 18% clear on 16:9. Display images 1.91:1 and 1:1."}
    return b


# ---------------------------------------------------------------- 2 ideas
def ideas_prompt(b: dict, language: str) -> str:
    approved = [c["text"] for c in b["claims"] if c["status"] == "approved"]
    return f"""Write 4 performance-ad angles for this brief, one of each kind: problem_solution, product_demo, offer_first, lifestyle.
Product: {json.dumps(b['product'], ensure_ascii=False)}
Audience: {json.dumps(b['audience'], ensure_ascii=False)}
You may ONLY use these approved claims: {json.dumps(approved, ensure_ascii=False)}
Language of spoken and on-screen words: {language}. Indian people, places and everyday life.
Score each angle 0-5 on: hook (stops the scroll in 2 seconds), one_message (one clear idea), product_on_screen
(product visible at least 60% of the time), policy_safe (no medical, money or exaggerated promises), fits_market.
JSON: {{"angles": [{{"kind": "problem_solution", "hook": "the first 2 seconds", "message": "the one idea", "scene": "where and who",
 "scores": {{"hook": 0, "one_message": 0, "product_on_screen": 0, "policy_safe": 0, "fits_market": 0}}}}]}}"""


def fallback_ideas(b: dict) -> dict:
    n, prob, off = b["product"].get("name") or "it", b["audience"].get("problem") or "an everyday problem", b["product"].get("offer") or ""
    base = {"problem_solution": (f"Tired of {prob}?", f"{n} fixes it"), "product_demo": (f"Watch {n} in action", f"See how {n} works"),
            "offer_first": (off or f"{n}, now", f"{off or n} — today only"), "lifestyle": (f"Your day, better with {n}", f"{n} fits your life")}
    return {"angles": [{"kind": k, "hook": h, "message": m, "scene": "an everyday Indian home",
                        "scores": {r: 3 for r in RULES}} for k, (h, m) in base.items()]}


def rank(angles: list[dict]) -> list[dict]:
    """Totals out of 25; a policy problem in the words themselves sets policy_safe to 0."""
    out = []
    for a in angles:
        sc = {r: max(0, min(5, int(float((a.get("scores") or {}).get(r, 0) or 0)))) for r in RULES}
        if banned_in(" ".join(str(a.get(k, "")) for k in ("hook", "message", "scene"))):
            sc["policy_safe"] = 0
        out.append({**a, "scores": sc, "total": sum(sc.values())})
    return sorted(out, key=lambda a: (-(a["scores"]["policy_safe"] > 0), -a["total"]))


# ---------------------------------------------------------------- 3 script
def script_prompt(b: dict, a: dict, language: str) -> str:
    approved = [c["text"] for c in b["claims"] if c["status"] == "approved"]
    return f"""Write a 15-second performance ad, angle "{ANGLE_NAME.get(a['kind'], a['kind'])}": hook "{a.get('hook')}", message "{a.get('message')}".
Product: {json.dumps(b['product'], ensure_ascii=False)}. Audience: {json.dumps(b['audience'], ensure_ascii=False)}. Scene: {a.get('scene', '')}.
Exactly 5 shots: 0-2s hook, 2-5s problem, 5-8s product in action, 8-11s product in action, 11-15s offer and call to action.
The product is clearly visible in shots 3, 4 and 5 (and in the hook if you can).
Rules: on-screen text max 6 words, ONLY from: the product name, the offer "{b['product'].get('offer', '')}", the call to action
"{b.get('cta', '')}", or these approved claims: {json.dumps(approved, ensure_ascii=False)}. Never draw text, logos or prices in the pictures.
Voiceover in {language}, in its own script, max 7 words per 3 seconds. Picture prompts in English: photorealistic, Indian people and places.
JSON: {{"shots": [{{"beat": "hook", "image_prompt": "", "motion": "what moves, camera move", "voiceover": "", "on_screen": "", "product_visible": true}}]}}"""


def fallback_script(b: dict, a: dict) -> dict:
    n = b["product"].get("name") or "the product"
    what = b["product"].get("what") or n
    offer, cta = b["product"].get("offer") or n, b.get("cta") or "Shop now"
    ok = [c["text"] for c in b["claims"] if c["status"] == "approved"]
    lines = [a.get("hook", ""), b["audience"].get("problem") or a.get("message", ""), f"{n}: {what}", ok[0] if ok else a.get("message", ""), f"{offer}. {cta}"]
    scene = a.get("scene") or "an everyday Indian home"
    return {"shots": [{"beat": bt, "image_prompt": f"{'Close-up of ' + n if bt == 'product' else 'An Indian person'} in {scene}, {bt} moment, photorealistic, natural light",
                       "motion": "slow push in, natural movement", "voiceover": lines[i][:60],
                       "on_screen": [n, "", n, ok[0] if ok else n, offer][i], "product_visible": bt in ("product", "offer") or i == 0}
                      for i, (bt, *_rest) in enumerate(BEATS)]}


def approved_copy(b: dict) -> list[str]:
    """Every piece of on-screen text that may appear, word for word."""
    out = [b["product"].get("name") or "", b["product"].get("offer") or "", b.get("cta") or "", b.get("disclaimer") or ""]
    out += [c["text"] for c in b["claims"] if c["status"] == "approved"]
    return [x.strip() for x in out if x and x.strip()]


def clean_script(s: dict, b: dict) -> dict:
    """Exactly 5 shots on the 15-second beat; on-screen text that isn't approved copy is removed (never invented)."""
    shots = (s.get("shots") or [])[:5]
    while len(shots) < 5:
        shots.append(dict(fallback_script(b, {"hook": "", "message": ""})["shots"][len(shots)]))
    approved = approved_copy(b)
    low = {a.lower(): a for a in approved}
    out = []
    for (beat, t0, t1), sh in zip(BEATS, shots):
        txt = str(sh.get("on_screen") or "").strip().strip('"')
        exact = low.get(txt.lower())
        if not exact:  # a shortened approved line is fine only if it is a word-for-word part of one
            exact = next((a for a in approved if txt and txt.lower() in a.lower() and len(txt.split()) >= 2), "")
            exact = txt if exact else ""
        if beat == "offer":
            exact = exact or (b["product"].get("offer") or b.get("cta") or "")
        out.append({"beat": beat, "start": t0, "end": t1, "image_prompt": str(sh.get("image_prompt") or "")[:600],
                    "motion": str(sh.get("motion") or "")[:300], "voiceover": str(sh.get("voiceover") or "").strip()[:160],
                    "on_screen": exact, "product_visible": bool(sh.get("product_visible", beat in ("product", "offer")))})
    share = sum(x["end"] - x["start"] for x in out if x["product_visible"]) / 15
    return {"shots": out, "product_share": round(share, 2)}


# ---------------------------------------------------------------- 6 finishing (exact, by script)
def script_of(text: str) -> str:
    for name, lo, hi in SCRIPT_RANGES:
        if any(lo <= ord(ch) <= hi for ch in text or ""):
            return name
    return "latin"


def font_for(text: str, fonts_dir: Path, brand_font: str | None = None) -> str:
    """The brand font for Latin text; a Noto font for Indian scripts (downloaded once, so letters join correctly)."""
    sc = script_of(text)
    if sc == "latin" and brand_font and Path(brand_font).exists():
        return brand_font
    f = fonts_dir / LANG_FONTS[sc]
    if not f.exists():
        fonts_dir.mkdir(parents=True, exist_ok=True)
        fam = LANG_FONTS[sc].split("-")[0]
        try:
            urllib.request.urlretrieve(FONT_URL.format(fam=fam, file=LANG_FONTS[sc]), f)
        except Exception:
            pass
    if f.exists() and f.stat().st_size > 10000:
        return str(f)
    for sysf in ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if Path(sysf).exists():
            return sysf
    return ""


def hexcolor(c: str, default=(255, 255, 255)) -> tuple:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (c or "").strip())
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else default


def safe_box(fmt: str) -> tuple[int, int, int, int]:
    w, h = FORMATS[fmt]
    s = SAFE[fmt]
    return int(w * s["side"]), int(h * s["top"]), int(w * (1 - s["side"])), int(h * (1 - s["bottom"]))


def _wrap(draw, text, font, maxw):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if draw.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def text_layer(fmt: str, text: str, where, brand: dict, fonts_dir: Path, size_frac: float = 0.06):
    """A transparent full-frame picture with one text block (rounded box in the brand colour), inside the safe zone.
    Returns (PIL image, box) — box is recorded for the final check."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = FORMATS[fmt]
    x0, y0, x1, y1 = safe_box(fmt)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if not text:
        return im, None
    d = ImageDraw.Draw(im)
    text = re.sub(r"\s*[·•]\s*", " | ", text)  # Indian-script fonts have no middle dot: draw a bar (the copy itself is unchanged)
    fpath = font_for(text, fonts_dir, brand.get("font"))
    px = max(18, int(min(w, h) * size_frac))
    while True:
        font = ImageFont.truetype(fpath, px) if fpath else ImageFont.load_default()
        lines = _wrap(d, text, font, (x1 - x0) * 0.9)
        lh = int(px * 1.35)
        bw = max(d.textlength(t, font=font) for t in lines) + px
        bh = lh * len(lines) + px * 0.6
        if (bw <= x1 - x0 and bh <= (y1 - y0) * 0.45) or px <= 16:
            break
        px = int(px * 0.9)
    cx = (x0 + x1) / 2
    if isinstance(where, tuple):  # ("px", n): the box starts n pixels under the top of the safe area
        top = min(y0 + where[1], y1 - bh - 2)
    else:
        pos = {"top": 0.0, "middle": 0.5, "bottom": 1.0}.get(where, where)  # 0 = top of the safe area, 1 = bottom
        top = y0 + 2 + (y1 - y0 - bh - 4) * max(0.0, min(1.0, float(pos)))
    box = (int(cx - bw / 2), int(top), int(cx + bw / 2), int(top + bh))
    bg = hexcolor(brand.get("color"), (20, 20, 20)) + (225,)
    d.rounded_rectangle(box, radius=int(px * 0.4), fill=bg)
    fg = hexcolor(brand.get("text_color"), (255, 255, 255))
    for i, t in enumerate(lines):
        tw = d.textlength(t, font=font)
        d.text((cx - tw / 2, top + px * 0.3 + i * lh), t, font=font, fill=fg)
    return im, box


def below_logo(fmt: str) -> tuple:
    """Just under the logo, so text never covers it."""
    w, h = FORMATS[fmt]
    return ("px", int(min(w, h) * 0.115))


def logo_layer(fmt: str, logo: str | None):
    from PIL import Image
    w, h = FORMATS[fmt]
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if not logo or not Path(logo).exists():
        return im, None
    x0, y0, x1, y1 = safe_box(fmt)
    lg = Image.open(logo).convert("RGBA")
    lh = int(min(w, h) * 0.08)
    lw = min(int(lg.width * lh / lg.height), int((x1 - x0) * 0.35))
    lh = int(lg.height * lw / lg.width)
    lg = lg.resize((lw, lh), Image.LANCZOS)
    im.alpha_composite(lg, (x0, y0))
    return im, (x0, y0, x0 + lw, y0 + lh)


def end_slate(fmt: str, brand: dict, b: dict, logo: str | None, fonts_dir: Path):
    """The last 2.5 seconds: brand-colour panel, big logo, offer, call to action, disclaimer."""
    from PIL import Image
    w, h = FORMATS[fmt]
    x0, y0, x1, y1 = safe_box(fmt)
    im = Image.new("RGBA", (w, h), hexcolor(brand.get("color"), (20, 20, 20)) + (240,))
    boxes, texts = [], []
    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        lh = int((y1 - y0) * 0.22)
        lw = min(int(lg.width * lh / lg.height), int((x1 - x0) * 0.6))
        lh = int(lg.height * lw / lg.width)
        lg = lg.resize((lw, lh), Image.LANCZOS)
        pos = (int((w - lw) / 2), int(y0 + (y1 - y0) * 0.04))
        im.alpha_composite(lg, pos)
        boxes.append(("logo", (pos[0], pos[1], pos[0] + lw, pos[1] + lh)))
    inv = {**brand, "color": brand.get("accent") or "#FFFFFF", "text_color": brand.get("color") or "#141414"}
    for txt, y, frac, br in ((b["product"].get("offer") or b["product"].get("name") or "", 0.45, 0.07, inv),
                             (b.get("cta") or "", 0.72, 0.06, inv), (b.get("disclaimer") or "", 1.0, 0.022, {**brand, "color": "#000000"})):
        if txt:
            lay, box = text_layer(fmt, txt, y, br, fonts_dir, frac)
            im.alpha_composite(lay)
            texts.append(txt)
            boxes.append(("text", box))
    return im, texts, boxes


def _fit_filter(fmt: str, src_w: int, src_h: int) -> str:
    """Vertical master -> each size: 9:16 and 1:1 crop to fill; wide sizes keep the whole picture over a blurred copy."""
    w, h = FORMATS[fmt]
    if fmt in ("9:16", "1:1"):
        return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1"
    return (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=30:2,eq=brightness=-0.08[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")


def finish_video(master: Path, out: Path, fmt: str, script: dict, b: dict, brand: dict, logo: str | None,
                 fonts_dir: Path, work: Path, seconds: float = 15.0) -> dict:
    """Logo, on-screen text per shot, disclaimer, end slate, size, even loudness: one ffmpeg run, exact every time."""
    work.mkdir(parents=True, exist_ok=True)
    w, h = FORMATS[fmt]
    info = probe(master)
    layers, record = [], {"texts": [], "boxes": [], "logo": False}
    lg, lbox = logo_layer(fmt, logo)
    if lbox:
        f = work / f"{fmt.replace(':', 'x')}_logo.png"
        lg.save(f)
        layers.append((f, 0, seconds - 2.5))
        record["logo"] = True
        record["boxes"].append(("logo", lbox))
    for i, sh in enumerate(script["shots"]):
        if sh.get("on_screen") and sh["beat"] != "offer":
            lay, box = text_layer(fmt, sh["on_screen"], (below_logo(fmt) if lbox else 0.0) if sh["beat"] in ("hook", "problem") else 0.8, brand, fonts_dir)
            f = work / f"{fmt.replace(':', 'x')}_t{i}.png"
            lay.save(f)
            layers.append((f, sh["start"], min(sh["end"], seconds - 2.5)))
            record["texts"].append(sh["on_screen"])
            record["boxes"].append(("text", box))
    if b.get("disclaimer"):
        lay, box = text_layer(fmt, b["disclaimer"], 1.0, {**brand, "color": "#000000"}, fonts_dir, 0.022)
        f = work / f"{fmt.replace(':', 'x')}_disc.png"
        lay.save(f)
        layers.append((f, 0, seconds - 2.5))
        record["boxes"].append(("text", box))
    slate, stexts, sboxes = end_slate(fmt, brand, b, logo, fonts_dir)
    f = work / f"{fmt.replace(':', 'x')}_slate.png"
    slate.save(f)
    layers.append((f, seconds - 2.5, seconds))
    record["texts"] += stexts
    record["boxes"] += sboxes
    record["slate"] = True
    args = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(master)]
    for f, _, _ in layers:
        args += ["-i", str(f)]
    fc = [f"[0:v]tpad=stop_mode=clone:stop_duration={seconds},trim=0:{seconds},setpts=PTS-STARTPTS,fps=30,{_fit_filter(fmt, info['width'], info['height'])}[v0]"]
    last = "v0"
    for k, (_, a, z) in enumerate(layers, 1):
        fc.append(f"[{last}][{k}:v]overlay=0:0:enable='between(t,{a:.2f},{z:.2f})'[v{k}]")
        last = f"v{k}"
    if info.get("has_audio"):
        fc.append(f"[0:a]apad,atrim=0:{seconds},loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
    else:
        fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{seconds}[a]")
    args += ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "[a]", "-t", f"{seconds}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", "30",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Finishing failed: " + r.stderr[-400:])
    return record


def finish_still(img: Path, out: Path, fmt: str, headline: str, b: dict, brand: dict, logo: str | None, fonts_dir: Path) -> dict:
    """A static ad: the shot's picture + headline + offer/CTA + logo (+ disclaimer), in one size."""
    from PIL import Image, ImageFilter, ImageOps
    w, h = FORMATS[fmt]
    src = Image.open(img).convert("RGB")
    if fmt in ("9:16", "1:1"):
        base = ImageOps.fit(src, (w, h), Image.LANCZOS)
    else:
        base = ImageOps.fit(src, (w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
        fg = ImageOps.contain(src, (w, h), Image.LANCZOS)
        base.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    im = base.convert("RGBA")
    rec = {"texts": [], "boxes": [], "logo": False}
    lg, lbox = logo_layer(fmt, logo)
    if lbox:
        im.alpha_composite(lg)
        rec["logo"] = True
        rec["boxes"].append(("logo", lbox))
    offer = " · ".join(x for x in (b["product"].get("offer"), b.get("cta")) if x)
    top = below_logo(fmt) if lbox else 0.0
    for txt, y, frac, br in ((headline, top, 0.06, brand), (offer, 0.8, 0.05, brand),
                             (b.get("disclaimer") or "", 1.0, 0.02, {**brand, "color": "#000000"})):
        if txt:
            lay, box = text_layer(fmt, txt, y, br, fonts_dir, frac)
            im.alpha_composite(lay)
            rec["texts"] += [t.strip() for t in txt.split(" · ")] if txt == offer else [txt]
            rec["boxes"].append(("text", box))
    im.convert("RGB").save(out, "JPEG", quality=92)
    return rec


# ---------------------------------------------------------------- 5 review (cheap checks; the vision check is in the pipeline)
def scene_cuts(video: Path) -> int:
    """How many hard cuts inside one shot (a shot should be one continuous take)."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(video), "-vf", "select='gt(scene,0.45)',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.stderr.count("pts_time:")


def frames_1fps(video: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(video), "-vf", "fps=1,scale=512:-2", str(out_dir / "f%03d.jpg")], check=True)
    return sorted(out_dir.glob("f*.jpg"))


# ---------------------------------------------------------------- 7 final check (by script)
SPEC = {"max_seconds": 15.5, "min_seconds": 14.5, "fps": (23.9, 30.1), "max_mb_video": 4000, "max_mb_image": 30}


def check_output(path: Path, fmt: str, rec: dict, b: dict, video: bool, brand: dict) -> list[str]:
    """Problems found (empty = passed)."""
    probs = []
    w, h = FORMATS[fmt]
    if not path.exists():
        return ["file missing"]
    mb = path.stat().st_size / 1e6
    if video:
        info = probe(path)
        if (info["width"], info["height"]) != (w, h):
            probs.append(f"size {info['width']}×{info['height']}, expected {w}×{h}")
        if not SPEC["min_seconds"] <= info["duration"] <= SPEC["max_seconds"]:
            probs.append(f"length {info['duration']:.1f} s")
        rr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
                            capture_output=True, text=True).stdout.strip() or "0/1"
        n_, d_ = (rr.split("/") + ["1"])[:2]
        fr = round(float(n_) / max(1.0, float(d_)), 2)
        if fr and not SPEC["fps"][0] <= fr <= SPEC["fps"][1]:
            probs.append(f"frame rate {fr}")
        if not info["has_audio"]:
            probs.append("no sound track")
        if mb > SPEC["max_mb_video"]:
            probs.append(f"file {mb:.0f} MB")
    else:
        from PIL import Image
        if Image.open(path).size != (w, h):
            probs.append("wrong picture size")
        if mb > SPEC["max_mb_image"]:
            probs.append(f"file {mb:.0f} MB")
    x0, y0, x1, y1 = safe_box(fmt)
    for kind, box in rec.get("boxes", []):
        if box and kind != "slate" and (box[0] < x0 - 2 or box[1] < y0 - 2 or box[2] > x1 + 2 or box[3] > y1 + 2):
            probs.append(f"{kind} outside the safe zone")
    allowed = {a.strip().lower() for a in approved_copy(b)}
    for t in rec.get("texts", []):
        if t.strip().lower() not in allowed and not any(t.strip().lower() in a for a in allowed):
            probs.append(f"text not in the approved copy: “{t}”")
        if banned_in(t):
            probs.append(f"banned word in “{t}”")
    if b.get("disclaimer") and b["disclaimer"] not in rec.get("texts", []):
        probs.append("disclaimer missing")
    if brand.get("logo") and not rec.get("logo"):
        probs.append("logo missing")
    return probs
