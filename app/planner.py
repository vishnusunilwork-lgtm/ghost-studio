"""Turns an idea or a reference ad into a plan: script lines, one first-frame image
prompt and one video prompt per clip, with a camera move for every clip."""
from __future__ import annotations

import re
from pathlib import Path

from . import llm

RULES = ("No money, income, debt, loan, cure or guaranteed-result claims. No fear or threats. "
         "No TV-news look, no channel names, no logos, no readable on-screen text. "
         "Every person is a fictional adult and must NOT resemble any real or famous person.")

MOVES = ["none", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down"]


def split_lines(text: str, max_words: int = 16) -> list[dict]:
    """'Name: words' lines keep their speaker; long speeches split at natural points."""
    out = []
    for raw in [l.strip() for l in text.splitlines() if l.strip()]:
        m = re.match(r"^([^:：]{1,24})[:：]\s*(.+)$", raw)
        spk, body = (m.group(1).strip(), m.group(2)) if m and len(m.group(1).split()) <= 3 else ("", raw)
        sents = re.split(r"(?<=[।!?.…])\s+", body)
        cur = ""
        for s in sents:
            parts = [s] if len(s.split()) <= max_words else re.split(r"(?<=[,،;])\s+", s)
            for p in parts:
                words = p.split()
                while len(words) > max_words:
                    out.append({"speaker": spk, "line": " ".join(words[:max_words])})
                    words = words[max_words:]
                p = " ".join(words)
                if cur and len((cur + " " + p).split()) > max_words:
                    out.append({"speaker": spk, "line": cur})
                    cur = p
                else:
                    cur = (cur + " " + p).strip()
        if cur:
            out.append({"speaker": spk, "line": cur})
    return out


def analyse_reference(cfg: dict, frames: list[dict], transcript: str) -> dict:
    imgs = [Path(f["frame"]).read_bytes() for f in frames[:6]]
    labels = "; ".join(f"image {i + 1} = scene {f['scene']} ({f['start']}-{f['end']} s)" for i, f in enumerate(frames[:6]))
    return llm.ask_json(cfg["models"]["vision"], f"""These are key frames of a vertical Indian video ad: {labels}.
What is said: {transcript or '(unknown)'}
Describe it for remaking with new, fictional people. Return JSON only:
{{"summary":"one line","format":"e.g. one person talks to camera / street Q&A / two people talk","theme":"festival or deity if any",
"setting":"place, time of day, light, colours","camera":"framing, height, movement, phone feel",
"people":[{{"key":"A","role":"","look":"age, build, hair, outfit, accessories (we will change the face)","voice":"age, gender, tone"}}],
"performance":"moment by moment: gestures, expressions, eye-line, energy",
"scenes":[{{"scene":1,"what":"what happens","camera_move":"one of {', '.join(MOVES)}"}}]}}""", images=imgs, temperature=0.3)


def plan(cfg: dict, *, idea: str = "", dialogue: str = "", reference: dict | None = None, seconds: int = 30,
         voice_mode: str = "oncamera", language: str | None = None, aspect: str = "9:16") -> dict:
    lang = language or cfg.get("language", "Hindi")
    b = cfg["brand"]
    max_s = float(cfg["quality"]["clip_seconds"])
    src = (f"REFERENCE AD to recreate (same kind of people, place, framing, camera moves, action and micro-expressions, "
           f"but new fictional faces): {reference}") if reference else f"IDEA: {idea}"
    need_script = not dialogue.strip()
    prompt = f"""You are a film director and prompt engineer for the open video model LTX-2 (it makes video AND the spoken audio with lip-sync from a first-frame image).
Make a {seconds} s {aspect} ad for {b['name']} in {lang}. About the brand: {b['about']}
{src}
{'Write the script yourself: a strong 2-second hook, natural spoken ' + lang + ', a clear call to action to download ' + b['spoken'] + ' at the end.' if need_script else 'Use EXACTLY this dialogue, every word, in order:' + chr(10) + dialogue}
Voice: {'people speak on camera' if voice_mode == 'oncamera' else 'a narrator speaks over the pictures (people do not talk)' if voice_mode == 'narrator' else 'mostly on camera, with a short narrator line at the end'}.
Rules: {RULES}
Split the speech into clips of at most {max_s} s each (about 15 {lang} words), at natural pauses, never mid-phrase.
For every clip write an LTX-2 prompt as one rich paragraph: who (same character description every time), exact action and micro-expressions second by second, where, light, camera framing and move, then the speech as: He/She says in {lang}: "<line>" (spoken once, natural pace, ends before the clip ends), then ambient sound. No music, no subtitles, no text.
Pick a camera_move per clip from {MOVES} and a handheld shake 0-1.5 (0 = tripod).
Return JSON only:
{{"title":"short name","character":{{"look":"full description of the main fictional character","voice":"voice description"}},
"people":[{{"key":"A","look":"","voice":""}}],
"image_prompt":"first-frame photo prompt, {aspect}, real phone photo, every visible detail",
"clips":[{{"speaker":"A","line":"","seconds":7,"image_prompt":"photo prompt for THIS moment: same people and place, the action of this line, framing","video_prompt":"","camera_move":"none","shake":0.6,"narration":""}}]}}"""
    try:
        if not llm.available():
            raise RuntimeError("Ollama is not running")
        data = llm.ask_json(cfg["models"]["llm"], prompt)
    except Exception as e:  # works without any model, so the app is usable from minute one
        data = _fallback(idea, dialogue, reference, seconds, lang, b, str(e), aspect)
    for c in data.get("clips", []):
        c["seconds"] = max(2.0, min(max_s + 0.5, float(c.get("seconds") or max_s)))
        if c.get("camera_move") not in MOVES:
            c["camera_move"] = "none"
        c["shake"] = max(0.0, min(1.5, float(c.get("shake") or 0)))
    return data


def _fallback(idea, dialogue, reference, seconds, lang, b, why, aspect="9:16"):
    lines = split_lines(dialogue) if dialogue.strip() else [
        {"speaker": "", "line": f"{idea or 'इस दिवाली'}…"},
        {"speaker": "", "line": f"{b['spoken']} ऐप डाउनलोड करें।"}]
    look = "a fictional Indian adult in festive everyday clothes, warm smile, real skin texture"
    moves = ["push_in", "none", "pan_right", "push_in", "tilt_up", "none"]
    clips = []
    for i, l in enumerate(lines):
        secs = min(7.5, max(3.0, len(l["line"].split()) / 2.3 + 0.8))
        shots = ["close-up, looking at camera", "medium shot, warm gesture", "detail of hands and diyas", "wide shot of the home",
                 "close-up smiling", "medium shot holding a phone"]
        clips.append({"speaker": l["speaker"] or "A", "line": l["line"], "seconds": round(secs, 1), "camera_move": moves[i % len(moves)], "shake": 0.6,
                      "image_prompt": f"{aspect} real phone photo, {look}, {idea or 'festive home'}, {shots[i % len(shots)]}, natural light, no text.",
                      "video_prompt": f"{look} speaks warmly to a phone camera, small natural gestures, blinks, breath before speaking. Says in {lang}: \"{l['line']}\". Natural pace. Soft ambient sound. No music, no subtitles, no text."})
    return {"title": (idea or "New ad")[:40], "character": {"look": look, "voice": "warm adult voice"}, "people": [],
            "image_prompt": f"{aspect} real phone photo, {look}, {idea or 'festive home'}, natural light, no text.",
            "clips": clips, "note": "Written without the local language model (" + why + "). Start Ollama for real scripts."}


SHOTS = ["close-up portrait, looking at camera, warm smile", "medium shot, natural gesture", "detail shot of hands and key object",
         "wide shot showing the whole place", "over-the-shoulder shot", "low angle, festive light behind",
         "candid moment, laughing", "top-down flat lay of the key objects"]


def plan_images(cfg: dict, *, idea: str = "", reference: dict | None = None, count: int = 4, aspect: str = "9:16",
                language: str | None = None) -> dict:
    """Images only: a set of still images (ad stills, status posts, thumbnails) with one prompt each."""
    count = max(1, min(8, int(count or 4)))
    src = (f"REFERENCE AD to take the look from (same kind of people, place, light and framing, new fictional faces): {reference}"
           if reference else f"IDEA: {idea}")
    prompt = f"""You are an art director and prompt engineer for a photo-real image model.
Make {count} different still images for {cfg['brand']['name']} ({cfg['brand']['about']}), {aspect} frame.
{src}
Rules: {RULES}
Keep the same fictional people, outfits and place across all images, but vary the moment, framing and angle.
Each prompt: one rich paragraph, real phone-photo look, every visible detail (people, clothes, place, light, colours, framing, lens feel). No text in the image.
Return JSON only: {{"title":"short name","images":[{{"caption":"3-6 words","prompt":""}}]}}"""
    try:
        if not llm.available():
            raise RuntimeError("Ollama is not running")
        data = llm.ask_json(cfg["models"]["llm"], prompt)
        imgs = [i for i in data.get("images", []) if i.get("prompt")][:count]
        if not imgs:
            raise RuntimeError("empty")
        data["images"] = imgs
    except Exception as e:
        look = "a fictional Indian adult in festive everyday clothes, real skin texture"
        data = {"title": (idea or "Images")[:40], "note": "Written without the local language model (" + str(e) + ").",
                "images": [{"caption": SHOTS[i % len(SHOTS)].split(",")[0].capitalize(),
                            "prompt": f"{aspect} real phone photo, {look}, {idea or 'festive home'}, {SHOTS[i % len(SHOTS)]}, natural light, no text."}
                           for i in range(count)]}
    return data
