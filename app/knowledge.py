"""What Ghost knows and learns.

1. A built-in Indian dictionary: words like "desi dog", "auto", "cutting chai" mean something specific in India,
   and picture models (trained mostly on Western photos) get them wrong. Ghost adds the exact look to the prompt.
2. What you teach it: "When I write X, I mean Y" and "Always: Z". Kept on this computer in .ghost/learned.json
   (updates never touch it). Ghost also notices when you ask for the same change twice and offers to remember it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / ".ghost" / "learned.json"
_lock = threading.Lock()

# term pattern -> how it really looks in India (positive descriptions only: picture models ignore "not")
DESI: list[tuple[str, str]] = [
    (r"desi (dog|kutta|puppy|pup)s?|indie dogs?|indian (street |pariah )?dogs?|street dogs?|pariah dogs?|stray dogs?|kutta",
     "an Indian pariah dog (desi indie street dog, INDog breed): medium-sized lean athletic body, short smooth coat in ginger, tan, fawn or "
     "brindle, often a white chest and white paws, large erect pointed ears, wedge-shaped fox-like head, almond-shaped brown eyes, "
     "slim legs, long tail curled over the back, photographed on a real Indian street or village lane"),
    (r"desi (cat|billi)s?|indian cats?|street cats?|stray cats?|billi",
     "an Indian street cat (desi billi): small lean short-haired domestic cat, tabby, ginger or calico coat, large ears, slim face, "
     "in a real Indian home, courtyard or lane"),
    (r"desi (cow|gaay|gai)s?|indian cows?|gaay|gau ?mata",
     "an Indian zebu cow (desi gaay, like Gir or Ongole): prominent shoulder hump, loose skin dewlap under the neck, long drooping ears, "
     "upward or curved horns, white, grey or reddish-brown coat"),
    (r"desi bull|nandi|indian bull", "an Indian zebu bull: large shoulder hump, dewlap, curved horns often painted, white or grey coat"),
    (r"(water )?buffalo(es)?|bhains", "an Indian water buffalo (bhains): black hairless skin, heavy body, swept-back curved horns"),
    (r"desi (chicken|murga|murgi|hen)s?|murga|murgi", "an Indian country chicken (desi murgi): small, lean, mixed brown, black and gold feathers, free-range in a village yard"),
    (r"indian (crow|crows)|kauwa|kaua", "an Indian house crow: grey neck and nape, glossy black body"),
    (r"monkeys?|bandar", "an Indian rhesus macaque or grey langur (as seen in Indian towns and temples)"),
    (r"auto ?rickshaws?|\bautos?\b|tuk ?tuks?", "an Indian auto-rickshaw: three-wheeler with a yellow and black (or green and yellow) body, black canvas roof, open sides"),
    (r"cycle rickshaws?", "an Indian cycle rickshaw: a bicycle front with a covered two-seat carriage behind"),
    (r"(indian )?trucks? art|desi trucks?|indian trucks?", "an Indian Tata truck with hand-painted colourful art, 'Horn OK Please' style decorations, tassels"),
    (r"cutting chai|desi chai|\bchai\b|masala chai", "Indian milky masala chai served in a small clear glass or a clay kulhad, light brown with a thin froth"),
    (r"kulhads?", "a small unglazed red clay cup (kulhad)"),
    (r"thali", "an Indian steel thali: round stainless-steel plate with small katoris of dal, sabzi, curd, rice, roti and pickle"),
    (r"dosa", "a South Indian dosa: thin golden crisp crepe, served on a banana leaf or steel plate with coconut chutney and sambar"),
    (r"idli", "South Indian idli: soft white steamed rice cakes with coconut chutney and sambar"),
    (r"biryani", "Indian biryani: long-grain basmati rice with saffron-orange and white grains, pieces of meat or vegetables, fried onions, mint"),
    (r"samosas?", "Indian samosas: golden fried triangular pastries with a spiced potato filling"),
    (r"jalebis?", "Indian jalebi: bright orange spiral sweets glistening with sugar syrup"),
    (r"kirana( store| shop)?", "a small Indian kirana shop: narrow counter, shelves packed with packets and jars, sacks of rice and dal in front"),
    (r"dhaba", "a roadside Indian dhaba: charpai cots, steel plates, tandoor, simple painted walls"),
    (r"charpai|khaat", "an Indian charpai: wooden frame cot woven with jute rope"),
    (r"matka", "a round red clay water pot (matka)"),
    (r"diyas?", "small clay oil lamps (diyas) with a cotton wick flame"),
    (r"rangoli|kolam", "colourful rangoli or kolam patterns drawn with coloured powder or rice flour on the floor"),
    (r"(kerala )?mundu|veshti|lungi", "a white or off-white South Indian mundu / veshti wrapped around the waist, often with a gold border"),
    (r"kasavu", "a Kerala kasavu saree or set-mundu: off-white cotton with a gold zari border"),
    (r"chawl", "a Mumbai chawl: old multi-storey building with long shared balconies and rows of doors"),
    (r"(kerala )?houseboat|kettuvallam", "a Kerala houseboat (kettuvallam): long wooden boat with a curved woven bamboo-and-palm roof on backwaters lined with coconut palms"),
    (r"gopuram|south indian temple", "a South Indian temple gopuram: tall pyramid tower covered with colourful carved figures"),
]

_DESI_RE = [(re.compile(r"\b(" + p + r")\b", re.I), d) for p, d in DESI]


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return {"means": d.get("means") or [], "always": d.get("always") or [], "seen": d.get("seen") or {}}
    except Exception:
        return {"means": [], "always": [], "seen": {}}


def _save(d: dict):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def learned() -> dict:
    d = _load()
    return {"means": d["means"], "always": d["always"]}


def add(kind: str, text: str, means: str = "") -> dict:
    text, means = " ".join((text or "").split())[:200], " ".join((means or "").split())[:600]
    if not text:
        raise ValueError("Write something to remember")
    with _lock:
        d = _load()
        if kind == "means":
            if not means:
                raise ValueError("Write what it means")
            d["means"] = [m for m in d["means"] if m["when"].lower() != text.lower()] + [{"when": text, "means": means, "t": int(time.time())}]
        else:
            if text.lower() not in (a["text"].lower() for a in d["always"]):
                d["always"].append({"text": text, "t": int(time.time())})
        _save(d)
    return learned()


def remove(kind: str, text: str) -> dict:
    with _lock:
        d = _load()
        if kind == "means":
            d["means"] = [m for m in d["means"] if m["when"].lower() != (text or "").lower()]
        else:
            d["always"] = [a for a in d["always"] if a["text"].lower() != (text or "").lower()]
        _save(d)
    return learned()


def seen(instruction: str) -> bool:
    """Count a Change request. True the 2nd time the same request is made (Ghost then offers to always do it)."""
    key = " ".join((instruction or "").lower().split())[:120]
    if not key:
        return False
    with _lock:
        d = _load()
        if key in (a["text"].lower() for a in d["always"]):
            return False
        n = int(d["seen"].get(key, 0)) + 1
        d["seen"][key] = n
        if len(d["seen"]) > 300:  # keep it small
            d["seen"] = dict(sorted(d["seen"].items(), key=lambda kv: -kv[1])[:200])
        _save(d)
    return n == 2


def meanings(text: str) -> list[str]:
    """'term = look' lines for every Indian or taught word found in the text."""
    out, low = [], (text or "").lower()
    for m in _load()["means"]:  # what you taught wins over the built-in dictionary
        if re.search(r"\b" + re.escape(m["when"].lower()) + r"\b", low):
            out.append(f"'{m['when']}' means {m['means']}")
    taught = " ".join(m["when"].lower() for m in _load()["means"])
    for rx, desc in _DESI_RE:
        hit = rx.search(text or "")
        if hit and hit.group(0).lower() not in taught:
            out.append(f"'{hit.group(0)}' means {desc}")
    return out


def hints(idea: str) -> str:
    """For the script AI: what the words in the idea mean, plus your standing wishes."""
    parts = meanings(idea)
    al = [a["text"] for a in _load()["always"]]
    s = ""
    if parts:
        s += "MEANING OF WORDS (follow exactly, describe these looks in the prompts): " + "; ".join(parts) + ". "
    if al:
        s += "THE USER ALWAYS WANTS: " + "; ".join(al) + ". "
    return s


def apply(prompt: str) -> str:
    """For the picture model: make sure every Indian/taught word is spelled out as a look, and add standing wishes."""
    p = (prompt or "").strip()
    low = p.lower()
    extra = []
    for line in meanings(p):
        look = line.split(" means ", 1)[1]
        if look[:40].lower() not in low:  # not already described by the script AI
            extra.append(line.split(" means ", 1)[0].strip("'") + ": " + look)
    for a in _load()["always"]:
        if a["text"].lower() not in low:
            extra.append(a["text"])
    if not extra:
        return p
    # put the subject's true look FIRST so the model weighs it most
    return ". ".join(extra) + ". " + p
