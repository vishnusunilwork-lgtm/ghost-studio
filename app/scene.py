"""Scene logic: real-world sizes, deliberate size swaps, counts and camera, spelled out for the picture model.

Picture models know what things look like, not how big they are next to each other. Asked for "an elephant and an
ant" they often make a giant ant (the two words get equal weight). Ghost reads the words first, the way a person would:

  1 find the subjects and their real sizes (ant ≈ 0.5 cm, cat ≈ 25 cm tall, elephant ≈ 3 m …)
  2 read size words attached to each one ("a tiny elephant", "a giant ant") — those are deliberate and win
  3 write it out: who is how big next to whom, about how many times, and a camera that can show both
     (a macro lens at ground level when one is tiny), or a clearly surreal scene when the person swapped the sizes
  4 counts ("3 cats" = exactly three)
No model call: it takes under a millisecond and runs on every picture prompt (knowledge.apply → engines.image).
"""
from __future__ import annotations

import re

# typical size in cm (height, or length for long low animals), for a sense of scale
SIZES = {
    "ant": 0.5, "mosquito": 0.5, "fly": 0.8, "bee": 1.5, "ladybug": 0.8, "spider": 2, "butterfly": 6, "dragonfly": 7,
    "frog": 7, "mouse": 8, "rat": 20, "squirrel": 20, "sparrow": 15, "parrot": 30, "pigeon": 30, "crow": 45, "hen": 40,
    "rabbit": 40, "cat": 25, "kitten": 15, "puppy": 25, "dog": 60, "goat": 70, "monkey": 60, "peacock": 110, "deer": 100,
    "child": 110, "kid": 110, "boy": 140, "girl": 140, "baby": 60, "man": 172, "woman": 160, "person": 165, "farmer": 168,
    "cow": 140, "buffalo": 150, "horse": 160, "camel": 200, "tiger": 100, "lion": 120, "bear": 150, "elephant": 300,
    "giraffe": 500, "whale": 2500, "dinosaur": 600,
    "cup": 10, "glass": 12, "phone": 15, "mobile": 15, "bottle": 25, "book": 25, "laptop": 35, "chair": 90, "bicycle": 100,
    "bike": 110, "scooter": 115, "auto": 170, "rickshaw": 170, "car": 150, "bus": 300, "truck": 350, "train": 400,
    "house": 600, "tree": 1000, "coconut tree": 2000, "building": 3000, "temple": 1500, "tower": 5000, "mountain": 300000,
}
THINGS = {"cup", "glass", "phone", "mobile", "bottle", "book", "laptop", "chair", "bicycle", "bike", "scooter", "auto", "rickshaw", "car",
          "bus", "truck", "train", "house", "tree", "coconut tree", "building", "temple", "tower", "mountain"}
SMALL = r"tiny|small|little|mini|miniature|toy-?sized|pocket-?sized|baby|pygmy|chhota|chhoti|kunj[ui]"
BIG = r"giant|huge|big|large|enormous|massive|colossal|gigantic|monster|towering|bada|badi|valiya"
NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1, "an": 1}


def _human(cm: float) -> str:
    if cm < 1:
        return f"{cm * 10:.0f} mm"
    if cm < 100:
        return f"{cm:.0f} cm"
    if cm < 100000:
        return f"{cm / 100:.1f} m".replace(".0 m", " m")
    return f"{cm / 100000:.0f} km"


def subjects(text: str) -> list[dict]:
    """Subjects in the order they appear: name, real size, size word in front of it (if any), count."""
    low = (text or "").lower()
    names = sorted(SIZES, key=len, reverse=True)
    pat = re.compile(r"(?:\b(\d+|" + "|".join(NUMS) + r")\s+)?(?:\b(" + SMALL + "|" + BIG + r")\s+(?:\w+\s+)?)?\b(" +
                     "|".join(re.escape(n) for n in names) + r")(?:e?s)?\b")
    out, seen = [], set()
    for m in pat.finditer(low):
        name = m.group(3)
        if name in seen:
            continue
        seen.add(name)
        mod = m.group(2) or ""
        out.append({"name": name, "cm": SIZES[name], "mod": "small" if re.fullmatch(SMALL, mod) else "big" if mod else "",
                    "count": int(m.group(1)) if (m.group(1) or "").isdigit() else NUMS.get(m.group(1) or "", 0), "at": m.start()})
    return out


def explain(text: str) -> str:
    """The scale / count / camera sentence for this prompt ('' when there is nothing to add)."""
    subs = subjects(text)
    if not subs:
        return ""
    parts = []
    counts = [s for s in subs if s["count"] >= 2]
    if counts:
        parts.append("Exactly " + ", ".join(f"{s['count']} {s['name']}s" for s in counts) + " — no more, no fewer.")
    alive = [s for s in subs if s["name"] not in THINGS]  # everyday objects already come out the right size
    pair = alive if len(alive) >= 2 else subs if any(s["mod"] for s in subs) else []
    if len(pair) >= 2:
        a, b = sorted(pair[:4], key=lambda s: s["cm"])[0], sorted(pair[:4], key=lambda s: s["cm"])[-1]
        ratio = b["cm"] / a["cm"]
        swapped = (a["mod"] == "big") or (b["mod"] == "small")
        if swapped:
            parts.append(f"Surreal scale, exactly as asked: the {a['name']} is {'giant' if a['mod'] == 'big' else 'its normal size'} and the "
                         f"{b['name']} is {'tiny' if b['mod'] == 'small' else 'its normal size'}, so the {a['name']} looks "
                         f"{'as big as or bigger than' if a['mod'] == 'big' or b['mod'] == 'small' else 'about as big as'} the {b['name']}. "
                         f"Both keep their true shape and look ({a['name']}: correct number of legs and body parts). "
                         "A clear, believable photograph of an impossible scene.")
        elif ratio >= 8:
            cam = ("Camera: macro lens at ground level, focused on the " + a["name"] + ", with the " + b["name"] +
                   " towering behind and partly out of frame, so both read clearly.") if ratio >= 40 else \
                  ("Camera: framed wide enough to show both at their true sizes.")
            parts.append(f"True-to-life scale: the {a['name']} is about {_human(a['cm'])} and the {b['name']} about {_human(b['cm'])}"
                         f" — the {a['name']} is about {ratio:,.0f} times smaller and must look tiny next to the {b['name']}"
                         f"{' (smaller than its toe)' if b['name'] == 'elephant' and ratio > 100 else ''}. Do not enlarge the {a['name']}. {cam}")
    elif len(subs) == 1 or not pair:
        s = next((x for x in subs if x["mod"]), subs[0])
        if s["mod"] == "big" and s["cm"] < 200:
            parts.append(f"The {s['name']} is unusually giant: show everyday objects or people around it for scale.")
        elif s["mod"] == "small" and s["cm"] > 50:
            parts.append(f"The {s['name']} is unusually tiny, like a toy: show a hand, cup or leaves around it for scale.")
    return " ".join(parts)


def apply(prompt: str) -> str:
    extra = explain(prompt)
    if not extra or extra[:30] in (prompt or ""):
        return prompt
    return f"{prompt.rstrip(' .')}. {extra}"
