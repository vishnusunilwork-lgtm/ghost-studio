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


VERB = re.compile(r"\b(make|create|generate|draw|change|edit|fix|retake|redo|redraw|undo|delete|remove|rename|open|show|pause|"
                  r"continue|resume|replace|split|rewrite|upgrade|dub|translate the video|add|give me \d+|"
                  r"banao|bana|badlo|badal|hatao|kholo)\b", re.I)
MEDIA = re.compile(r"\b(pic(ture)?s?|images?|photos?|img|shots?|clips?|scenes?|songs?|takes?|music|lyrics|stems?|video|ad|ads|"
                   r"project|projects|dub|voice ?over|narrat\w*|main look|chitra|gaana|gana|tasveer)\b", re.I)


STRONG = re.compile(r"\b(pic(ture)?s?|images?|photos?|shots?|clips?|scenes?|songs?|takes?|music|stems?|video|dub|main look|project|"
                    r"chitra|gaana|tasveer)\b", re.I)
WRITE = re.compile(r"\b(taglines?|slogans?|captions?|lines|ideas?|poem|story|stories|email|message|post|hashtags?|tips|explain|meaning|"
                   r"translate|summar\w+|what|why|how|who|when|where|which)\b", re.I)
NUM = re.compile(r"\b(\d+|first|second|third|fourth|fifth|last|one|two|three|four)\b", re.I)
MOD = re.compile(r"\b(again|different|another|instead|more|less|older|younger|bigger|smaller|brighter|darker|but|use|with)\b", re.I)
PRON = re.compile(r"\b(this|that|it|her|him|them|his|she|he|one|ones)\b", re.I)


def wants_action(text: str, project: dict | None) -> bool:
    """Do something in Ghost (tools) or just talk (answered like ChatGPT)?
    Action: something Ghost makes + an action word, a number or a change word ("make 3 pictures", "shot 3 again",
    "take 1 but with a male voice"), or a short request about what is open ("make her older").
    Talk: questions and writing help ("Give me 3 Onam ad taglines", "how do I make good thumbnails?")."""
    t = text.strip()
    if create_request(t):
        return True
    if WRITE.search(t) and not STRONG.search(t):
        return False
    has_v, has_m = bool(VERB.search(t)), bool(MEDIA.search(t))
    if has_m and (has_v or NUM.search(t) or MOD.search(t)):
        return True
    if project and (PRON.search(t) or NUM.search(t)) and (has_v or (MOD.search(t) and len(t.split()) <= 12)):
        return True
    return False


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
    c = create_request(text)
    simple = not re.search(r"\b(and then|then|but|also|after that|phir)\b|,", t)
    explicit = re.search(r"\b(new|pictures?|images?|photos?|pics?|videos?|reels?|songs?|music)\b", t)
    if c and simple and (not project or explicit):  # one clear "make X": no model needed; anything richer goes to a model
        return Route(0, calls=[("create_project", c)], why="make: " + c["kind"])
    m = re.fullmatch(r"remember[:,]? (.+?) (?:means|=|is) (.+)", text.strip(), re.I)
    if m:
        return Route(0, calls=[("remember", {"kind": "means", "text": m.group(1).strip(" '\""), "means": m.group(2).strip()})], why="teach")
    m = re.fullmatch(r"always[:,]? (.+)", text.strip(), re.I)
    if m and len(m.group(1).split()) <= 12:
        return Route(0, calls=[("remember", {"kind": "always", "text": m.group(1).strip()})], why="teach")
    return None


CREATE = re.compile(r"^(?:please |pls |can you |could you |ghost,? )?(generate|create|make|draw|design|paint|render|imagine|show me|"
                    r"give me)(?: me)?(?: (an?|the|some|one|two|three|four|five|six|seven|eight|\d+))? (.+?)[.!?]*$", re.I)
NOT_MEDIA = re.compile(r"\b(taglines?|slogans?|captions?|poems?|story|stories|essay|email|letter|message|post|hashtags?|tips|list|"
                       r"plan|summary|script|lyrics only|recipe|code|table|names?|ideas?|joke|quiz|translation|answer)\b", re.I)
EDITISH = re.compile(r"^(her|his|him|it|its|this|that|these|those|them|their|she|he|the same)\b|\b(shorter|longer|older|younger|brighter|darker|"
                     r"bigger|smaller|again|instead|(picture|pic|image|shot|clip|take|scene) (no\.? ?)?\d+|number \d+)\b", re.I)
WORDS_N = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "a": 1, "an": 1}


def create_request(text: str) -> dict | None:
    """"generate a cat", "make 3 pictures of a desi dog", "create a video of rain in Kochi", "make a song about Onam"
    -> make it straight away (like ChatGPT / Gemini do), no model needed to understand it."""
    t = " ".join(text.strip().split())
    m = CREATE.match(t)
    if not m or NOT_MEDIA.search(m.group(3)) or EDITISH.search(m.group(3)) or len(t.split()) > 60:
        return None  # "make her hair shorter in picture 2" is a change, not something new
    what, n = m.group(3).strip(), m.group(2)
    low = what.lower()
    kind = "images"
    if re.search(r"\b(video|reel|clip|animation|ad film|commercial|short film|ad)\b", low):
        kind = "video"
    elif re.search(r"\b(song|music|jingle|bhajan|lullaby|rap|track|tune|bgm|gaana|paattu)\b", low):
        kind = "music"
    idea = re.sub(r"^(?:(?:\d+|an?|one|two|three|four|five|six|seven|eight) )?(?:(?:ai |hd |realistic |nice |beautiful )?(?:pictures?|images?|photos?|pics?|"
                  r"wallpapers?|posters?|videos?|reels?|clips?|songs?|music|jingles?)\s*(?:of|about|for|on|showing|with)?\s*)", "", what, flags=re.I).strip()
    idea = what if kind == "music" else (idea or what)  # a song keeps "a song about Onam": the words matter
    a = {"kind": kind, "idea": idea}
    if kind == "images":
        num_ = re.match(r"(\d+|one|two|three|four|five|six|seven|eight)\b", low)
        cnt = int(n) if n and n.isdigit() else WORDS_N.get((n or "").lower(), 0) or (
            int(num_.group(1)) if num_ and num_.group(1).isdigit() else WORDS_N.get(num_.group(1), 0) if num_ else 0)
        a["count"] = max(1, min(8, cnt or 1))
    if kind == "video":
        sec = re.search(r"(\d+)\s*(?:s|sec|seconds?)\b", low)
        a["seconds"] = int(sec.group(1)) if sec else 15
    return a


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
    if not wants_action(text, project):
        return Route(3, model=careful, why="conversation")
    c = complexity(text)
    if c < 1.2 and fast and fast != careful:
        return Route(1, model=fast, why=f"simple request (score {c:.1f})")
    return Route(2, model=careful, why=f"multi-step or open request (score {c:.1f})")
