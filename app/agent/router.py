"""Router: pick the cheapest path that can do the job (like a fast model + a reasoning model behind one box).

  Tier 0  exact commands ("undo", "retake 2", "yes") -> handled by a grammar, no model, instant
  Tier 1  one simple request -> the smallest installed script model (fast)
  Tier 2  several steps, vague or long -> the biggest installed script model (careful)
If no script model is installed, Tier 0 still works and Ghost says what it understood.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import llm

YES = r"(yes|yeah|yep|ok|okay|go|go ahead|do it|sure|confirm|haan|ha|han|theek hai|ശരി|हाँ|हां)"
NO = r"(no|nope|cancel|stop|don'?t|nahi|nahin|वेंडा|नहीं)"
NUM_WORDS = {"one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3, "four": 4, "fourth": 4, "five": 5,
             "fifth": 5, "six": 6, "sixth": 6, "seven": 7, "eight": 8, "last": -1}
N = r"(\d+|one|two|three|four|five|six|seven|eight|first|second|third|fourth|fifth|sixth|last)"
ITEM = r"(?:pic(?:ture)?|image|photo|img|shot|clip|scene|take|song)"


@dataclass
class Route:
    tier: int
    model: str = ""
    calls: list[tuple[str, dict]] = field(default_factory=list)
    confirm: bool | None = None  # tier 0 yes/no to a pending plan
    why: str = ""


def num(s: str) -> int:
    s = s.lower()
    return int(s) if s.isdigit() else NUM_WORDS.get(s, 1)


def _target(word: str, project: dict | None) -> str:
    w = word.lower()
    if w.startswith(("shot", "clip", "scene")):
        return "shot"
    if w.startswith(("take", "song")):
        return "song"
    if w.startswith(("pic", "image", "photo", "img")):
        return "picture"
    return "shot" if project and project.get("kind") == "video" else "picture"


def _last(project, what) -> int:
    if not project:
        return 1
    return len(project.get({"picture": "stills", "shot": "clips", "song": "tracks"}[what]) or []) or 1


def grammar(text: str, project: dict | None, pending: bool) -> Route | None:
    """Tier 0. Only very clear commands; anything else goes to a model."""
    t = " ".join(text.strip().lower().split()).rstrip(".!")
    if pending:
        if re.fullmatch(YES + r"( please| thanks)?", t):
            return Route(0, confirm=True, why="yes to the plan")
        if re.fullmatch(NO + r"( thanks)?", t):
            return Route(0, confirm=False, why="no to the plan")
    if re.fullmatch(r"(undo|undo that|undo it|go back|revert)( please)?", t):
        if project and project.get("undo"):
            return Route(0, calls=[("undo", {"target": "all"})], why="undo")
        return None  # which item? let the model look at the project
    if re.fullmatch(r"(pause|stop)( it| this| making)?( please)?", t):
        return Route(0, calls=[("pause", {})], why="pause")
    if re.fullmatch(r"(continue|resume|carry on|make it)( please)?", t):
        return Route(0, calls=[("resume", {})], why="continue")
    m = re.fullmatch(r"(retake|redo|new take(?: of)?|fresh take(?: of)?) (?:" + ITEM + r" )?(?:no\.? |number |#)?" + N, t)
    if m:
        rest = t[m.end(1):]
        hit = re.search(r"\b" + ITEM + r"\b", rest)
        what = _target(hit.group(0) if hit else "", project)
        if what == "song":
            what = "picture" if not project or project.get("kind") != "video" else "shot"
        i = num(m.group(2))
        i = _last(project, what) if i == -1 else i
        fresh = m.group(1).startswith(("new", "fresh"))
        if what == "shot":
            return Route(0, calls=[("retake_shot", {"index": i})], why="retake shot")
        if what == "picture":
            return Route(0, calls=[("retake_picture", {"index": i, "fresh": fresh})], why="retake picture")
    if re.fullmatch(r"(another|one more|more) (song )?take(s)?( please)?", t):
        return Route(0, calls=[("more_song_takes", {})], why="more takes")
    m = re.fullmatch(r"(?:make )?(?:" + ITEM + r" )?" + N + r" (?:the )?main look", t)
    if m:
        return Route(0, calls=[("set_main_look", {"index": num(m.group(1))})], why="main look")
    m = re.fullmatch(r"remember[:,]? (.+?) (?:means|=|is) (.+)", text.strip(), re.I)
    if m:
        return Route(0, calls=[("remember", {"kind": "means", "text": m.group(1).strip(" '\""), "means": m.group(2).strip()})], why="teach")
    m = re.fullmatch(r"always[:,]? (.+)", text.strip(), re.I)
    if m and len(m.group(1).split()) <= 12:
        return Route(0, calls=[("remember", {"kind": "always", "text": m.group(1).strip()})], why="teach")
    return None


def complexity(text: str) -> float:
    """Rough difficulty: long, multi-step or vague requests go to the bigger model."""
    t = text.lower()
    steps = len(re.findall(r"\b(and then|then|also|after that|and|,|;|phir|aur)\b", t))
    vague = len(re.findall(r"\b(better|nicer|something|like|feel|vibe|kind of|maybe|why|how)\b", t))
    return len(t.split()) / 14 + steps * 0.6 + vague * 0.4 + (0.5 if "?" in t else 0)


def pick_models(cfg: dict) -> tuple[str, str]:
    """(fast, careful): the newest model family installed; its smallest for speed, its biggest for care."""
    have = llm.text_models()
    if not have:
        return "", ""
    best = min(llm.family_rank(m) for m in have)
    fam = sorted([m for m in have if llm.family_rank(m) == best], key=llm.size_of)
    want = cfg.get("models", {}).get("llm", "")
    careful = want if want in fam else fam[-1]
    fast = fam[0] if llm.size_of(fam[0]) <= 5 else careful
    return fast, careful


def route(cfg: dict, text: str, project: dict | None, pending: bool) -> Route:
    g = grammar(text, project, pending)
    if g:
        return g
    fast, careful = pick_models(cfg)
    if not careful:
        return Route(-1, why="no script model installed")
    c = complexity(text)
    if c < 1.2 and fast and fast != careful:
        return Route(1, model=fast, why=f"simple request (score {c:.1f})")
    return Route(2, model=careful, why=f"multi-step or open request (score {c:.1f})")
