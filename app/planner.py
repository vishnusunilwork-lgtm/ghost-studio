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
"music_style":"background music for this ad: genre, instruments, mood, tempo (no vocals)",
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


# Each shot = framing + lens + what is in frame + pose/action. Prompts START with this, so shots really differ.
SHOT_SPECS = [
    ("Close-up portrait", "Tight close-up portrait, 85mm lens, the face fills most of the frame from forehead to chin, eyes sharp and looking just past the camera, softly blurred background"),
    ("Medium shot", "Medium shot from the waist up, 50mm lens, the person in the left third of the frame mid-action, background readable but soft"),
    ("Detail of hands", "Macro detail shot of only the hands and the key object, 100mm macro lens, face NOT visible, very shallow depth of field, the object sharp"),
    ("Wide establishing shot", "Wide establishing shot, 24mm lens, full body small in the frame, the whole place and its surroundings visible, environmental storytelling"),
    ("Over-the-shoulder", "Over-the-shoulder shot from behind the person, 35mm lens, looking toward what they look at, their shoulder and hair soft in the foreground"),
    ("Candid laugh", "Candid moment, 50mm lens, laughing naturally with eyes crinkled, slight motion, three-quarter angle"),
    ("Low angle", "Low-angle hero shot, 28mm lens, looking up at the person with lights and sky behind, confident pose"),
    ("Top-down flat lay", "Top-down flat lay of the key objects only, no people, arranged on a textured surface, even soft light"),
]
QUALITY = ("Photo-real, natural skin texture with pores, anatomically correct hands with five relaxed fingers each, "
           "natural symmetrical eyes with catchlights, true-to-life proportions, sharp focus on the subject, "
           "professional colour grade, no text, no watermark, no logos.")
# kept for older code paths
SHOTS = [s[1] for s in SHOT_SPECS]


def plan_images(cfg: dict, *, idea: str = "", reference: dict | None = None, count: int = 4, aspect: str = "9:16",
                language: str | None = None) -> dict:
    """Images only: a set of clearly different still images (close-up, detail, wide…) of the same people and place."""
    count = max(1, min(8, int(count or 4)))
    shots = [SHOT_SPECS[i % len(SHOT_SPECS)] for i in range(count)]
    src = (f"REFERENCE AD to take the look from (same kind of people, place, light and framing, new fictional faces): {reference}"
           if reference else f"IDEA: {idea}")
    shot_list = "\n".join(f"{i + 1}. {c}: {d}" for i, (c, d) in enumerate(shots))
    prompt = f"""You are an art director and prompt engineer for a photo-real image model.
Make {count} still images for {cfg['brand']['name']} ({cfg['brand']['about']}), {aspect} frame.
{src}
Rules: {RULES}
First invent ONE fictional main character and ONE place, described precisely (age, face, hair, skin tone, outfit with colours and pattern, jewellery; place, time, light).
Then write one prompt per shot below. Each prompt MUST start with that shot's framing text, then the character (same words every time),
then a DIFFERENT action/pose/expression for that shot, then the place and light. Shots must look clearly different.
Shots:
{shot_list}
End every prompt with: {QUALITY}
Return JSON only: {{"title":"short name","character":"","place":"","images":[{{"caption":"3-5 words","prompt":""}}]}}"""
    try:
        if not llm.available():
            raise RuntimeError("Ollama is not running")
        data = llm.ask_json(cfg["models"]["llm"], prompt, temperature=0.7)
        imgs = [i for i in data.get("images", []) if i.get("prompt")][:count]
        if len(imgs) < count:
            raise RuntimeError("too few prompts")
        for i, im in enumerate(imgs):
            if QUALITY[:30] not in im["prompt"]:
                im["prompt"] = im["prompt"].rstrip(". ") + ". " + QUALITY
        data["images"] = imgs
    except Exception as e:
        who = ("a fictional Indian woman in her late twenties, oval face, warm brown skin, long dark hair, small red bindi, "
               "gold jhumka earrings, deep red silk saree with a gold border")
        where = idea or "a festive home"
        actions = ["gently smiling as she looks at the flame", "lighting a brass diya with a matchstick", "holding a brass thali with three lit diyas",
                   "standing at the balcony railing among rows of lit diyas", "watching the city lights", "laughing with family off-camera",
                   "holding a sparkler", "arranged marigold flowers, diyas and sweets"]
        data = {"title": (idea or "Images")[:40], "note": "Written without the script AI (" + str(e)[:80] + "). Install Script AI in ⚙ → Add-ons for better prompts.",
                "images": [{"caption": c, "prompt": f"{d}. Subject: {who}, {actions[i % len(actions)]}. Setting: {where}, warm practical light, bokeh of string lights, night. {QUALITY}"}
                           for i, (c, d) in enumerate(shots)]}
    return data


LANG_CODES = {"Hindi": "hi", "English": "en", "Marathi": "mr", "Tamil": "ta", "Telugu": "te", "Bengali": "bn",
              "Gujarati": "gu", "Kannada": "kn", "Malayalam": "ml", "Punjabi": "pa"}

STYLES = {
    "bollywood": "Bollywood pop, catchy hook, dholak and tabla with modern drums, strings, warm vocals",
    "bhajan": "devotional Indian bhajan, harmonium, tabla, manjira, temple bells, calm and uplifting",
    "sufi": "Sufi qawwali-inspired, harmonium, claps, soulful soaring vocals, building energy",
    "bhangra": "Punjabi bhangra, dhol, tumbi, high energy, celebratory",
    "festive": "festive Indian celebration music, shehnai, dhol, joyful, family festival mood",
    "acoustic": "indie acoustic, guitar and soft percussion, intimate warm vocals",
    "lofi": "lo-fi chill beat, soft keys, vinyl texture, relaxed",
    "cinematic": "cinematic orchestral score, emotional strings, piano, big build",
    "edm": "EDM dance track, punchy kick, synth drops, energetic",
    "hiphop": "Indian hip-hop, 808 bass, crisp drums, confident rap flow",
    "classical": "Indian classical fusion, sitar, bansuri flute, tabla, raga feel",
    "ghazal": "ghazal, soft harmonium, tabla, tender romantic vocals, slow tempo",
}


def plan_music(cfg: dict, *, idea: str = "", style: str = "bollywood", vocals: str = "ai", lyrics: str = "",
               seconds: int = 60, language: str | None = None) -> dict:
    """Music / song: a style description (caption) and, for songs, lyrics with [Verse]/[Chorus] tags."""
    lang = language or cfg.get("language", "Hindi")
    code = LANG_CODES.get(lang, "unknown")
    base = STYLES.get(style, style or STYLES["bollywood"])
    out = {"title": (idea or "Untitled song")[:40], "caption": f"{base}. {idea}".strip(), "lyrics": "", "language": code,
           "seconds": int(seconds), "describe": ""}
    if vocals == "instrumental":
        out["lyrics"] = "[Instrumental]"
        out["caption"] += ". Instrumental, no vocals."
        return out
    if vocals == "mine" and lyrics.strip():
        out["lyrics"] = lyrics.strip()
    want_lyrics = vocals != "mine" or not lyrics.strip()
    try:
        if not llm.available():
            raise RuntimeError("no local script AI")
        verses = max(1, round(seconds / 45))
        data = llm.ask_json(cfg["models"]["llm"], f"""You are a hit songwriter and music producer.
Brand context (do not force the name in): {cfg['brand']['about']}
Song idea: {idea or 'a warm festival song'}
Style: {base}
Length: about {seconds} seconds.
{'Write the lyrics in ' + lang + (' using Devanagari script' if lang in ('Hindi', 'Marathi') else ' in its own script') + f'. Structure with tags on their own lines: [Verse], [Chorus], [Bridge], [Outro]. About {verses} verse(s) and a memorable chorus. Natural singable lines, no brand claims, no real people.' if want_lyrics else 'Lyrics are given; do not change them.'}
Return JSON only: {{"title":"short song title","caption":"one line for the music model: genre, instruments, mood, tempo, vocal type (e.g. warm female vocals)","bpm":100{', "lyrics":"..."' if want_lyrics else ''}}}""", temperature=0.8)
        out["title"] = data.get("title") or out["title"]
        out["caption"] = (data.get("caption") or out["caption"]).strip()
        if data.get("bpm"):
            out["bpm"] = int(data["bpm"])
        if want_lyrics and data.get("lyrics"):
            out["lyrics"] = data["lyrics"]
    except Exception as e:
        if want_lyrics:  # let the music engine write the lyrics itself
            out["describe"] = f"a {lang} song, {base}, about: {idea or 'a joyful festival'}"
            out["note"] = f"Lyrics written by the music engine (script AI not available: {e})."
    return out
