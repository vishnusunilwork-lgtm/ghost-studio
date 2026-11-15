"""Turns an idea or a reference ad into a plan: script lines, one first-frame image
prompt and one video prompt per clip, with a camera move for every clip."""
from __future__ import annotations

import re
from pathlib import Path

from . import knowledge, llm

RULES = ("No money, income, debt, loan, cure or guaranteed-result claims. No fear or threats. "
         "No TV-news look, no channel names, no logos, no readable on-screen text. "
         "Every person is a fictional adult and must NOT resemble any real or famous person.")

MOVES = ["none", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down"]

# People are Indian unless the idea says otherwise; states/regions (even misspelled) set the look, clothes and place.
REGIONS = {
    "kerala": "a Malayali from Kerala, South India, warm brown skin; clothes like a cream-and-gold kasavu saree or set-mundu, jasmine in the hair; places like coconut palms, backwaters, red-tiled homes",
    "tamil nadu": "a Tamil person from Tamil Nadu, South India, brown skin; silk saree or veshti, jasmine flowers; temple towers, kolam patterns, banana leaves",
    "karnataka": "a Kannadiga from Karnataka, South India, brown skin; Mysore silk saree; stone temples, coffee estates",
    "andhra": "a Telugu person from Andhra Pradesh / Telangana, South India, brown skin; cotton or pattu saree; courtyards, rangoli",
    "maharashtra": "a Marathi person from Maharashtra, India, wheatish to brown skin; nauvari or paithani saree, nath nose ring; Mumbai streets, wadas",
    "gujarat": "a Gujarati person from Gujarat, India, wheatish skin; bandhani and mirror-work chaniya choli; havelis, bright textiles",
    "punjab": "a Punjabi person from Punjab, India, wheatish skin; phulkari dupatta, salwar kameez or turban; mustard fields, haveli courtyards",
    "bengal": "a Bengali person from West Bengal, India, brown skin; red-and-white saree, big red bindi; Kolkata lanes, terracotta",
    "rajasthan": "a Rajasthani person, India, wheatish to brown skin; leheriya and gota-patti, silver jewellery; sandstone havelis, desert light",
    "assam": "an Assamese person from Assam, North-East India; mekhela chador; tea gardens, bamboo homes",
    "odisha": "an Odia person from Odisha, India, brown skin; sambalpuri saree; temples, sea-side villages",
    "goa": "a Goan person, India, brown skin; summery cotton dresses or saree; Portuguese-style houses, beaches",
    "bihar": "a Bihari person, India, brown skin; cotton saree, sindoor; village courtyards, ghats",
    "uttar pradesh": "a person from Uttar Pradesh, India, wheatish to brown skin; chikankari kurta; Lucknow and Varanasi streets, ghats",
}
ALIASES = {"kera": "kerala", "kerla": "kerala", "keral": "kerala", "keralam": "kerala", "malayali": "kerala", "malayalee": "kerala", "mallu": "kerala",
           "kochi": "kerala", "trivandrum": "kerala", "tamil": "tamil nadu", "tamilnadu": "tamil nadu", "chennai": "tamil nadu",
           "kannada": "karnataka", "bangalore": "karnataka", "bengaluru": "karnataka", "mysore": "karnataka", "telugu": "andhra",
           "hyderabad": "andhra", "telangana": "andhra", "marathi": "maharashtra", "mumbai": "maharashtra", "pune": "maharashtra",
           "gujarati": "gujarat", "ahmedabad": "gujarat", "punjabi": "punjab", "amritsar": "punjab", "bengali": "bengal", "bangla": "bengal",
           "kolkata": "bengal", "rajasthani": "rajasthan", "jaipur": "rajasthan", "assamese": "assam", "odia": "odisha", "goan": "goa",
           "bihari": "bihar", "lucknow": "uttar pradesh", "varanasi": "uttar pradesh", "banaras": "uttar pradesh"}
FOREIGN = ("korean", "japanese", "chinese", "american", "african", "european", "british", "french", "arab", "russian", "mexican",
           "brazilian", "thai", "vietnamese", "filipino", "indonesian", "german", "italian", "spanish", "caucasian", "white", "black")
INDIAN_WORDS = ("indian", "south asian", "desi", *[r.split()[0] for r in REGIONS], *ALIASES.keys(), "hindi", "kannadiga", "kerala")


def region_of(text: str) -> str:
    """'a kera ayoung woman' -> 'kerala' (handles typos)."""
    import difflib
    words = re.findall(r"[a-z]+", (text or "").lower())
    joined = " ".join(words)
    for r in REGIONS:
        if r in joined:
            return r
    keys = list(ALIASES) + [r for r in REGIONS if " " not in r]
    for w in words:
        if w in ALIASES:
            return ALIASES[w]
        if w in REGIONS:
            return w
        if len(w) >= 4:
            m = difflib.get_close_matches(w, keys, n=1, cutoff=0.8)
            if m:
                return ALIASES.get(m[0], m[0])
    return ""


def people_note(idea: str) -> str:
    low = (idea or "").lower()
    if any(re.search(rf"\b{f}\b", low) for f in FOREIGN):
        return ""
    r = region_of(idea)
    if r:
        return f"All people are {REGIONS[r]}. (In the idea, any similar word or typo means {r.title()}.)"
    return "All people are Indian (South Asian features, brown skin tones), in Indian places and clothes, unless the idea clearly says otherwise."


def ground(prompt: str, idea: str) -> str:
    """Makes sure a picture prompt says who the people are, so the model never guesses another country."""
    if not prompt:
        return prompt
    low = (idea or "").lower()
    if any(re.search(rf"\b{f}\b", low) for f in FOREIGN):
        return prompt
    r = region_of(idea)
    who = REGIONS[r].split(";")[0] if r else "Indian people with South Asian features and brown skin"
    if r and r.split()[0] in prompt.lower():
        return prompt
    if not r and re.search(r"\b(" + "|".join(re.escape(w) for w in INDIAN_WORDS) + r")\b", prompt.lower()):
        return prompt
    return f"{prompt.rstrip(' .')}. The people are {who}."


def ground_plan(data: dict, idea: str) -> dict:
    for k in ("image_prompt",):
        if data.get(k):
            data[k] = ground(data[k], idea)
    for c in data.get("clips", []) or []:
        for k in ("image_prompt", "video_prompt"):
            if c.get(k):
                c[k] = ground(c[k], idea)
    for pp in data.get("people", []) or []:
        if pp.get("single_prompt"):
            pp["single_prompt"] = ground(pp["single_prompt"], idea)
    for im in data.get("images", []) or []:
        if im.get("prompt"):
            im["prompt"] = ground(im["prompt"], idea)
    return data

# Direction presets (from how real interview / film / newsroom / phone videos are shot)
STYLES_DIR = {
    "interview2": {"name": "Interview · 2 cameras", "grade": "interview",
        "guide": ("Two-camera seated conversation (interview / podcast). Exactly TWO fictional adults: A (host) and B (guest), seated facing "
                  "each other at a slight angle in a real office with frosted glass panels and warm wood, soft window key light, natural colours. "
                  "Camera A = medium close-up of A in the left third, looking screen-right just past the lens. Camera B = medium close-up of B in "
                  "the right third, looking screen-left. 85mm look, f/2, background softly out of focus. Cut on every line: the speaker alternates "
                  "A, B, A, B; reactions are allowed. Cameras are locked off with only a tiny drift."),
        "moves": ["push_in", "none"], "amount": 0.35, "shake": 0.15},
    "cinematic": {"name": "Cinematic film", "grade": "cinematic",
        "guide": ("Feature-film scene. Motivated practical lights, warm amber highlights with teal shadows, deep sets with real background "
                  "action, people walking in depth. Mix wide establishing shots (24mm) with movement toward camera, mediums (35mm) and close-up "
                  "reactions (85mm). Slow, steady push-ins; composed frames; film grain; no handheld wobble."),
        "moves": ["push_in", "pull_out", "pan_right", "push_in", "tilt_up"], "amount": 0.7, "shake": 0.1},
    "news": {"name": "Newsroom explainer", "grade": "news",
        "guide": ("Newsroom explainer: one fictional presenter in a modern studio with soft screens behind (no logos, no readable text), "
                  "crisp bright key light, medium shots to camera intercut with detail cutaways that illustrate each point."),
        "moves": ["push_in", "none", "pan_left"], "amount": 0.5, "shake": 0.05},
    "ugc": {"name": "Phone UGC", "grade": "",
        "guide": ("Phone selfie video at arm's length, vertical, natural window light in a real home, casual and authentic, "
                  "small handheld movement, eye contact with the lens."),
        "moves": ["none", "push_in"], "amount": 0.5, "shake": 0.9},
}


def style_from_reference(ref: dict | None) -> str:
    t = " ".join(str(ref.get(k, "")) for k in ("format", "summary", "camera")).lower() if isinstance(ref, dict) else ""
    if any(w in t for w in ("two people", "interview", "conversation", "podcast", "two person")):
        return "interview2"
    if any(w in t for w in ("film", "movie", "cinematic", "scene", "trailer")):
        return "cinematic"
    if any(w in t for w in ("news", "anchor", "presenter")):
        return "news"
    if any(w in t for w in ("selfie", "phone", "vlog", "ugc")):
        return "ugc"
    return ""


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
         voice_mode: str = "oncamera", language: str | None = None, aspect: str = "9:16", style: str = "auto") -> dict:
    lang = language or cfg.get("language", "Hindi")
    b = cfg["brand"]
    max_s = float(cfg["quality"]["clip_seconds"])
    src = (f"REFERENCE AD to recreate (same kind of people, place, framing, camera moves, action and micro-expressions, "
           f"but new fictional faces): {reference}") if reference else f"IDEA: {idea}"
    need_script = not dialogue.strip()
    n_clips = max(1, round(seconds / (max_s - 1.0))) if need_script else max(1, len(split_lines(dialogue)))
    if reference and not (reference.get("transcript") or "").strip():
        src += "\n(The reference's speech could not be heard: copy its look, people, framing and camera moves, and write a fresh script.)"
    prompt = f"""You are a film director and prompt engineer for an image-to-video model (it makes video AND the spoken audio with lip-sync from a first-frame image).
Make a {seconds} s {aspect} ad for {b['name']} in {lang}. About the brand: {b['about']}
{src}
{'Write the script yourself: a strong 2-second hook, natural spoken ' + lang + ', a clear call to action to download ' + b['spoken'] + ' at the end.' if need_script else 'Use EXACTLY this dialogue, every word, in order:' + chr(10) + dialogue}
Voice: {'people speak on camera' if voice_mode == 'oncamera' else 'a narrator speaks over the pictures (people do not talk)' if voice_mode == 'narrator' else 'mostly on camera, with a short narrator line at the end'}.
Rules: {RULES} {people_note(idea or dialogue)} {knowledge.hints((idea or '') + ' ' + (dialogue or ''))}
{('DIRECTION STYLE (follow it exactly): ' + STYLES_DIR[style]['guide']) if style in STYLES_DIR else ''}
Split the speech into clips of at most {max_s} s each (about 15 {lang} words), at natural pauses, never mid-phrase.
Make EXACTLY {n_clips} clips so the ad really lasts about {seconds} seconds. Give each clip a different shot (close-up, medium, detail, wide…) and a different camera move.
The title must be a real 2-5 word name for this ad (never "short name").
For every clip write a video prompt as one rich paragraph: who (same character description every time), exact action and micro-expressions second by second, where, light, camera framing and move, then the speech as: He/She says in {lang}: "<line>" (spoken once, natural pace, ends before the clip ends), then ambient sound. No music, no subtitles, no text.
Pick a camera_move per clip from {MOVES} and a handheld shake 0-1.5 (0 = tripod).
Return JSON only:
{{"title":"short name","character":{{"look":"full description of the main fictional character","voice":"voice description"}},
"people":[{{"key":"A","look":"","voice":"","single_prompt":"photo prompt of this person alone in their own camera framing, same place and light"}}],
"image_prompt":"first-frame photo prompt, {aspect}, real phone photo, every visible detail",
"music_style":"background music for this ad: genre, instruments, mood, tempo (no vocals)",
"clips":[{{"speaker":"A","line":"","seconds":7,"image_prompt":"photo prompt for THIS moment: same people and place, the action of this line, framing","video_prompt":"","camera_move":"none","shake":0.6,"narration":""}}]}}"""
    try:
        if not llm.available():
            raise RuntimeError("Ollama is not running")
        data = llm.ask_json(cfg["models"]["llm"], prompt)
        if len(data.get("clips", [])) < max(1, int(n_clips * 0.7)):  # too short: ask once more, firmly
            more = llm.ask_json(cfg["models"]["llm"], prompt + f"\n\nIMPORTANT: your last answer had only {len(data.get('clips', []))} clip(s). "
                                f"Return the full JSON again with EXACTLY {n_clips} clips.", temperature=0.4)
            if len(more.get("clips", [])) > len(data.get("clips", [])):
                data = more
        if len(data.get("clips", [])) < max(1, int(n_clips * 0.7)):
            raise RuntimeError(f"the script AI wrote only {len(data.get('clips', []))} of {n_clips} clips")
    except Exception as e:  # works without any model, so the app is usable from minute one
        data = _fallback(idea, dialogue, reference, seconds, lang, b, str(e), aspect)
    t = (data.get("title") or "").strip()
    if not t or t.lower() in ("short name", "title", "name", "new ad"):
        first = (data.get("clips") or [{}])[0].get("line", "") or idea or "New ad"
        data["title"] = " ".join(first.split()[:5]).strip(" .,!?:;") or "New ad"
    sd = STYLES_DIR.get(style)
    alternate = style == "interview2" and (len({x.get("speaker") for x in data.get("clips", [])}) < 2
                                           or any(x.get("speaker") not in ("A", "B") for x in data.get("clips", [])))
    for i, c in enumerate(data.get("clips", [])):
        if sd:
            c["camera_move"] = c.get("camera_move") if c.get("camera_move") in sd["moves"] else sd["moves"][i % len(sd["moves"])]
            c["amount"] = sd["amount"]
            c["shake"] = sd["shake"]
        if alternate:
            c["speaker"] = "AB"[i % 2]
    if style == "interview2" and len(data.get("people") or []) < 2:
        data["people"] = [
            {"key": "A", "look": "fictional Indian woman in her early thirties, shoulder-length dark hair, taupe knit top, small earrings",
             "single_prompt": "Medium close-up, 85mm, a fictional Indian woman in her early thirties with shoulder-length dark hair and a taupe knit top, "
                              "seated, framed in the left third, looking screen-right just past the camera, frosted-glass office behind, soft window light, shallow depth of field."},
            {"key": "B", "look": "fictional Indian woman in her late twenties, long dark hair, black shirt, red pendant earrings",
             "single_prompt": "Medium close-up, 85mm, a fictional Indian woman in her late twenties with long dark hair and a black shirt, "
                              "seated, framed in the right third, looking screen-left just past the camera, warm wood panel and frosted glass behind, soft window light, shallow depth of field."}]
        if "seated facing each other" not in data.get("image_prompt", ""):
            data["image_prompt"] = ("Wide two-shot, 35mm, two fictional Indian women seated facing each other at a slight angle in a modern office with frosted glass "
                                    "panels and warm wood, soft window light, natural colours, real photo, no text, no logos.")
    data["style"] = style
    ground_plan(data, idea or dialogue)
    for c in data.get("clips", []):
        c["seconds"] = max(2.0, min(max_s + 0.5, float(c.get("seconds") or max_s)))
        if c.get("camera_move") not in MOVES:
            c["camera_move"] = "none"
        c["shake"] = max(0.0, min(1.5, float(c.get("shake") if c.get("shake") is not None else 0)))
    return data


def _fallback(idea, dialogue, reference, seconds, lang, b, why, aspect="9:16"):
    if dialogue.strip():
        lines = split_lines(dialogue)
    else:
        n = max(2, round(seconds / 6.5))
        body = [f"{idea or 'इस दिवाली'}…", "अपनों के साथ हर पल ख़ास बनाइए।", "अपनी फ़ोटो और नाम के साथ शुभकामनाएँ भेजिए।",
                "हर त्योहार, अपनी भाषा में।", "आज ही शुरू कीजिए।"] if lang == "Hindi" else \
               [f"{idea or 'This festival'}…", "Make every moment with your family special.", "Send wishes with your own photo and name.",
                "Every festival, in your language.", "Start today."]
        lines = [{"speaker": "", "line": body[i % len(body)]} for i in range(n - 1)] + [{"speaker": "", "line": f"{b['spoken']} ऐप डाउनलोड करें।" if lang == "Hindi" else f"Download {b['spoken']} today."}]
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
# Shots for a subject that is not a person (a cat, a product, a temple, food…)
SUBJECT_SHOTS = [
    ("Close-up", "Close-up of the main subject filling most of the frame, 85mm lens, sharp detail, softly blurred background"),
    ("Medium shot", "Medium shot of the whole main subject in the left third of the frame, 50mm lens, background readable but soft"),
    ("Macro detail", "Macro detail of one striking part of the main subject (texture, eyes, pattern), 100mm macro lens, very shallow depth of field"),
    ("Wide shot", "Wide shot, 24mm lens, the main subject small in the frame, the whole place around it visible"),
    ("Low angle", "Low-angle hero shot, 28mm lens, looking up at the main subject with light and sky behind"),
    ("Side profile", "Side profile of the main subject, 70mm lens, rim light outlining its shape"),
    ("Top-down", "Top-down view of the main subject and the objects around it, soft even light"),
    ("Candid moment", "Candid moment of the main subject in motion, 50mm lens, slight motion, three-quarter angle"),
]
QUALITY_SUBJECT = ("Photo-real, true-to-life textures and proportions, sharp focus on the subject, natural light, "
                   "professional colour grade, no text, no watermark, no logos.")
PEOPLE_WORDS = ("person", "people", "woman", "women", "man", "men", "girl", "girls", "boy", "boys", "lady", "ladies", "guy", "family",
                "mother", "mom", "mum", "amma", "father", "dad", "papa", "appa", "child", "children", "kid", "kids", "baby", "couple",
                "bride", "groom", "aunty", "auntie", "uncle", "grandma", "grandmother", "grandpa", "grandfather", "dadi", "nani",
                "friend", "friends", "student", "students", "teacher", "farmer", "worker", "model", "she", "he", "her", "his", "they",
                "selfie", "portrait", "face", "dancer", "singer", "priest", "devotee", "shopkeeper", "customer", "doctor", "influencer",
                "host", "presenter", "anchor", "chef", "cook", "actor", "actress", "hero", "heroine", "character",
                "wedding", "party", "celebration", "celebrating", "dance", "dancing", "crowd", "team", "audience")


def has_people(idea: str) -> bool:
    """Does the idea ask for people? "cat" -> no (only the cat), "girl with a cat" -> yes."""
    low = (idea or "").lower()
    return bool(re.search(r"\b(" + "|".join(PEOPLE_WORDS) + r")\b", low)) or any(
        w in low for w in ("लड़की", "लड़का", "औरत", "आदमी", "परिवार", "माँ", "बच्चा"))


# kept for older code paths
SHOTS = [s[1] for s in SHOT_SPECS]


def plan_images(cfg: dict, *, idea: str = "", reference: dict | None = None, count: int = 4, aspect: str = "9:16",
                language: str | None = None, style: str = "auto") -> dict:
    """Images only: a set of clearly different still images (close-up, detail, wide…) of the same people and place."""
    count = max(1, min(8, int(count or 4)))
    people = bool(reference) or has_people(idea)
    specs, quality = (SHOT_SPECS, QUALITY) if people else (SUBJECT_SHOTS, QUALITY_SUBJECT)
    shots = [specs[i % len(specs)] for i in range(count)]
    src = (f"REFERENCE AD to take the look from (same kind of people, place, light and framing, new fictional faces): {reference}"
           if reference else f"IDEA: {idea}")
    shot_list = "\n".join(f"{i + 1}. {c}: {d}" for i, (c, d) in enumerate(shots))
    prompt = f"""You are an art director and prompt engineer for a photo-real image model.
Make {count} still images for {cfg['brand']['name']} ({cfg['brand']['about']}), {aspect} frame.
{src}
Rules: {RULES} {people_note(idea) if people else ''} {knowledge.hints(idea)}
{('LOOK AND DIRECTION: ' + STYLES_DIR[style]['guide']) if style in STYLES_DIR else ''}
{"First invent ONE fictional main character and ONE place, described precisely (age, face, hair, skin tone, outfit with colours and pattern, jewellery; place, time, light)."
 if people else
 f"The MAIN SUBJECT is exactly what the idea names: '{idea}'. It is the star of every picture. Do NOT add any person, face, hands or body parts, "
 "and do not turn it into a statue, toy, drawing or deity unless the idea says so. Describe ONE specific subject precisely "
 "(for an animal: breed, fur colour and pattern, eye colour; for an object: material, colour, shape) and ONE place (time, light)."}
Then write one prompt per shot below. Each prompt MUST start with that shot's framing text, then the {"character" if people else "main subject"} (same words every time),
then a DIFFERENT action/pose/expression for that shot, then the place and light. Shots must look clearly different.
Shots:
{shot_list}
End every prompt with: {quality}
Return JSON only: {{"title":"short name","character":"","place":"","images":[{{"caption":"3-5 words","prompt":""}}]}}"""
    try:
        if not llm.available():
            raise RuntimeError("Ollama is not running")
        data = llm.ask_json(cfg["models"]["llm"], prompt, temperature=0.7)
        imgs = [i for i in data.get("images", []) if i.get("prompt")][:count]
        if len(imgs) < count:
            raise RuntimeError("too few prompts")
        for i, im in enumerate(imgs):
            if quality[:30] not in im["prompt"]:
                im["prompt"] = im["prompt"].rstrip(". ") + ". " + quality
            if not people:
                im["prompt"] = im["prompt"].rstrip(". ") + ". No people, no human faces or hands in the frame."
        data["images"] = imgs
        if people:
            ground_plan(data, idea)
    except Exception as e:
        if not people:
            subject = idea or "the subject"
            return {"title": (idea or "Images")[:40], "note": "Written without the script AI (" + str(e)[:80] + "). Install Script AI in ⚙ → Add-ons for better prompts.",
                    "images": [{"caption": c, "prompt": f"{d}. Main subject: {subject}, photographed for real in a natural Indian setting, soft natural light. "
                                                        f"No people, no human faces or hands. {quality}"} for c, d in shots]}
        r = region_of(idea)
        who = (f"a fictional young woman, {REGIONS[r].split(';')[0]}, oval face, long dark hair, {REGIONS[r].split(';')[1].strip()}" if r else
               "a fictional Indian woman in her late twenties, oval face, warm brown skin, long dark hair, small red bindi, "
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
        plan_lines = ("one verse of 4 short lines and a chorus of 2 lines" if seconds <= 40 else
                      "verse (4 lines), chorus (4 lines), verse (4 lines), chorus" if seconds <= 100 else
                      "verse, chorus, verse, chorus, bridge (2 lines), chorus")
        script = (" using Devanagari script" if lang in ("Hindi", "Marathi") else " in its own script")
        data = llm.ask_json(cfg["models"]["llm"], f"""You are a professional {lang} lyricist for ad jingles and film songs.
Song idea: {idea or 'a warm festival song'}
Style: {base}
Length: about {seconds} seconds, so write {plan_lines}.
{('Write the lyrics in simple, everyday, correct ' + lang + script + '. Every word must be a real common word a child would know; no invented words, no mixed scripts. '
  'Each line 4-7 words, easy to sing, gentle rhymes. Tags on their own lines: [Verse], [Chorus], [Bridge]. No brand names, no claims, no real people.') if want_lyrics else 'Lyrics are given; do not change them.'}
Return JSON only: {{"title":"short song title","caption":"one line for the music model: genre, instruments, mood, tempo, vocal type (e.g. warm female vocals)","bpm":100{', "lyrics":"..."' if want_lyrics else ''}}}""", temperature=0.7)
        if want_lyrics and data.get("lyrics"):
            try:  # second pass: a strict proofread catches made-up words
                fix = llm.ask_json(cfg["models"]["llm"], f"""Proofread these {lang} song lyrics. Replace any word that is not a real, common {lang} word,
fix grammar, keep the meaning, keep every [tag] line and the number of lines. Return JSON only: {{"lyrics":"..."}}
Lyrics:
{data['lyrics']}""", temperature=0.2)
                if fix.get("lyrics") and len(fix["lyrics"]) > 0.5 * len(data["lyrics"]):
                    data["lyrics"] = fix["lyrics"]
            except Exception:
                pass
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


# ---------------------------------------------------------------- live editing ("Change")
KIND_NOTE = {
    "image_prompt": "a photo description for an image AI (keep it one paragraph, concrete, visual)",
    "line": "one spoken line of an ad (keep it short and natural to say)",
    "lyrics": "song lyrics (keep the [Verse]/[Chorus] section tags and roughly the same length so it still fits the music)",
    "caption": "a music style description for a music AI (genre, instruments, mood, tempo, voice)",
    "text": "ad copy",
}


def rewrite(cfg: dict, text: str, instruction: str, kind: str = "text", language: str | None = None) -> str:
    """Change the SAME text by a plain-words instruction. Keeps everything the instruction doesn't mention."""
    from . import llm
    text, instruction = (text or "").strip(), (instruction or "").strip()
    if not instruction:
        return text
    if not llm.available():
        if kind in ("image_prompt", "caption"):  # no script AI: add the change as an explicit instruction
            return f"{text.rstrip(' .')}. Change: {instruction.rstrip('.')}." if text else instruction
        raise RuntimeError("Rewriting needs the Script add-on (⚙ → Add-ons).")
    lang = f" Write it in {language}." if language and kind in ("line", "lyrics") else ""
    data = llm.ask_json(cfg["models"]["llm"], f"""You edit {KIND_NOTE.get(kind, KIND_NOTE['text'])}.
Apply ONLY this change: "{instruction}".
Keep every other detail exactly as it is (people, place, wording, structure) unless the change needs it.{lang}
Use only real, common words. Do not add explanations. {knowledge.hints(text + ' ' + instruction) if kind in ('image_prompt', 'caption') else ''}

TEXT:
{text}

Answer as JSON: {{"text": "<the edited text>"}}""", temperature=0.3)
    out = str(data.get("text") or "").strip()
    return out or text



def translate_lines(cfg: dict, lines: list[str], src: str, target: str, context: str = "") -> list[str]:
    """Dubbing: each line in natural spoken target language, about the same length (it must fit the same moment)."""
    from . import llm
    if not llm.available():
        raise RuntimeError("Dubbing needs the Script AI add-on (⚙ → Add-ons) to translate.")
    out: list[str] = []
    for k in range(0, len(lines), 20):
        chunk = lines[k:k + 20]
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk))
        for attempt in range(2):
            data = llm.ask_json(cfg["models"]["llm"], f"""Translate these {len(chunk)} spoken lines from {src or 'the original language'} into natural, everyday spoken {target}
for dubbing a video{(' (' + context[:200] + ')') if context else ''}.
Rules: keep the meaning and the emotion; keep names, brands and numbers; use words people really say, not formal or literary words;
each line about the same length as the original so it fits the same moment; write {target} in its own script.
{knowledge.hints(' '.join(chunk))}
LINES:
{numbered}

Answer as JSON: {{"lines": ["<line 1 in {target}>", "<line 2>", ...]}} with exactly {len(chunk)} items.""", temperature=0.3)
            got = [str(x).strip() for x in (data.get("lines") or [])]
            if len(got) == len(chunk) and all(got):
                break
        if len(got) != len(chunk):
            got = (got + chunk)[:len(chunk)]  # keep timing honest: untranslated lines stay as they were
        out += got
    return out
