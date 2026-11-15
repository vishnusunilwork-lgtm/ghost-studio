"""Context layer: what the model sees on every step.

  system rules  +  long-term memory (what you taught Ghost)  +  the open project's state
  +  retrieved past projects (search, like retrieval-augmented generation)  +  the conversation
The window is limited (a small local model reads ~8k tokens), so old turns are compacted into a short
summary instead of being dropped. Conversation history is context, not training: the model's weights never change.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path

from .. import knowledge

ROOT = Path(__file__).resolve().parent.parent.parent
CHATS = ROOT / ".ghost" / "chats"
BUDGET = 14000          # characters of context (~3.5k tokens) so a small model stays fast and accurate
KEEP_TURNS = 10         # recent messages kept word for word
_lock = threading.Lock()

SYSTEM = """You are Ghost, the assistant inside Ghost Studio, an app that makes ads, pictures, video shots and songs on this computer.
You act through tools. Rules:
- When the person asks for something you can do with a tool, call the tool. Do not just describe it.
- Pictures, shots and takes are numbered from 1, as the person sees them. "this one"/"it" means the open project.
- Use change_* to edit the SAME picture/shot/song. Use retake/new take only when they want a different one.
- "More pictures like this", "the same for Diwali", "a new set", "another song like this" = create_project with same_as = the open project's id.
- "Replace / redo the part from 0:30 to 0:45" = replace_song_part. "Separate the vocals" = split_song_stems.
- Several steps are fine: call the tools one after another.
- Only say something was done if a tool result says so. If a tool reports an error, fix the call or ask.
- create_project, delete_project, start_again and changes to ALL items wait for the person's OK; the app asks them.
- Words like "desi", Indian places, foods and clothes mean their Indian look. People are Indian unless they say otherwise.
- If the request is unclear, ask ONE short question.
- After acting, reply in 1-2 short sentences, in the person's language (Hindi, Malayalam, English…).
- If no tool fits (a question, advice, writing help), just answer helpfully like a normal assistant."""


CHAT = """You are Ghost, a friendly, very capable assistant (like ChatGPT or Gemini) built into Ghost Studio, on this computer.
Ghost Studio is general purpose: anything a person wants to make or know, for any use, not only ads, greetings or devotion.
Answer any question on any topic: ideas, writing, scripts, captions, translations, explanations, planning, maths, coding, advice.
- YOU CAN MAKE pictures, video, songs, voice-overs and dubbing yourself, inside Ghost Studio. Never say you can't make images or
  video, and never send people to other tools (DALL-E, Midjourney, Bing, Canva, Veo…). If they want something made, say you are
  making it, or tell them to just type it, e.g. "make a picture of a cat" / "make a 15 s video of rain in Kochi".
- Language: reply in the person's language AND script. When they ask for a mix, use it exactly:
  Manglish = Malayalam written in English letters ("Njan sukhamayi irikkunnu, ningalo?"), NOT Hindi.
  Hinglish = Hindi in English letters ("Main theek hoon"). Tanglish = Tamil in English letters. Tenglish = Telugu, Kanglish = Kannada,
  Benglish = Bengali, Marathi/Gujarati in English letters likewise. Malayalam = മലയാളം script, Hindi = देवनागरी, unless asked otherwise.
  Keep using that language for the rest of the chat until they change it.
- Default context is India, but follow exactly what the person asks; add no religion, festival or devotion unless they ask.
- Be clear and useful. Short questions get short answers; use short paragraphs or bullet points for longer ones.
- When LIVE RESULTS are given, use them for anything current and cite them like [1]. Without them, say your knowledge may be out of date
  for news, prices or recent events. Never invent facts, names, numbers or quotes.
- Be creative when asked to create (ideas, taglines, stories, scripts): give fresh, specific, surprising options, not generic ones."""


# ---------------------------------------------------------------- conversation store
def _file(sid: str) -> Path:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid or "")[:40] or "default"
    return CHATS / f"{sid}.json"


def load(sid: str) -> dict:
    try:
        return json.loads(_file(sid).read_text(encoding="utf-8"))
    except Exception:
        return {"id": sid, "messages": [], "summary": [], "pending": None, "created": time.time()}


def save(s: dict):
    with _lock:
        CHATS.mkdir(parents=True, exist_ok=True)
        f = _file(s["id"])
        s["updated"] = time.time()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)


def compact(s: dict):
    """Move old messages into a one-line-per-turn summary (no extra model call: fast and exact)."""
    msgs = s["messages"]
    if len(msgs) <= KEEP_TURNS * 2:
        return
    old, s["messages"] = msgs[:-KEEP_TURNS], msgs[-KEEP_TURNS:]
    while s["messages"] and s["messages"][0].get("role") == "tool":  # never start on an orphan tool result
        old.append(s["messages"].pop(0))
    for m in old:
        if m["role"] == "user":
            s["summary"].append("Person: " + m["content"][:140])
        elif m["role"] == "tool":
            s["summary"].append("Done: " + m["content"][:140])
    s["summary"] = s["summary"][-30:]


# ---------------------------------------------------------------- retrieval over past projects (BM25)
def _tok(t: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9ऀ-ൿ]+", (t or "").lower()) if len(w) > 1]


def _doc(p: dict) -> str:
    parts = [p.get("title", ""), p.get("idea", ""), p.get("kind", ""), p.get("style", "")]
    parts += [s.get("caption", "") + " " + " ".join(s.get("edits") or []) for s in p.get("stills") or []]
    parts += [c.get("line", "") for c in p.get("clips") or []]
    parts += [(p.get("song") or {}).get("caption", "")]
    return " ".join(x for x in parts if x)


def search(projects: list[dict], query: str, k: int = 3) -> list[dict]:
    """BM25 ranking: the same scoring classic search engines use. Small, exact, no extra model."""
    docs = [(p, _tok(_doc(p))) for p in projects]
    q = _tok(query)
    if not q or not docs:
        return []
    n, avg = len(docs), sum(len(d) for _, d in docs) / max(1, len(docs))
    df = Counter(w for _, d in docs for w in set(d))
    scored = []
    for p, d in docs:
        tf, score = Counter(d), 0.0
        for w in q:
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(d) / max(1, avg)))
        if score > 0:
            scored.append((score, p))
    return [p for _, p in sorted(scored, key=lambda x: -x[0])[:k]]


# ---------------------------------------------------------------- the open project, compactly
def describe(p: dict | None) -> str:
    if not p:
        return "No project is open."
    head = (f"OPEN PROJECT id={p['id']} kind={p.get('kind', 'video')} title=\"{p.get('title', '')}\" status={p.get('status')} "
            f"quality={'fast' if p.get('quality') == 'draft' else 'quality'} aspect={p.get('aspect')} idea=\"{(p.get('idea') or '')[:160]}\"")
    lines = [head]
    for i, s in enumerate(p.get("stills") or [], 1):
        main = " (main look)" if p.get("anchor_i") == i - 1 else ""
        ed = f" changed: {', '.join(s['edits'][-2:])}" if s.get("edits") else ""
        lines.append(f"picture {i}{main}: {s.get('caption', '')}{ed}")
    for i, c in enumerate(p.get("clips") or [], 1):
        ed = f" changed: {', '.join(c['edits'][-2:])}" if c.get("edits") else ""
        lines.append(f"shot {i}: \"{(c.get('line') or '')[:70]}\"{ed}")
    for t in p.get("tracks") or []:
        lines.append(f"song take {t.get('n')} ({float(t.get('seconds') or 0):.0f} s)" + (f" (from take {t['from']}: {t['change']})" if t.get("change") else "")
                     + (" stems ready" if t.get("stems") else ""))
    if p.get("dub"):
        lines.append(f"dub {p['dub'].get('from')} -> {p['dub'].get('to')}, {len(p['dub'].get('segments') or [])} lines")
    if (p.get("undo") or []):
        lines.append("last change to all: " + p["undo"][-1].get("what", ""))
    return "\n".join(lines)[:3000]


def build(s: dict, project: dict | None, projects: list[dict], text: str) -> list[dict]:
    """Static part first (rules + tool schemas never change -> the model server reuses its cache of them, so each
    step only reads the new part). Everything that changes (memory, open project, search results) rides on the
    newest user message instead of the system message."""
    mem = knowledge.learned()
    parts = []
    if mem["means"] or mem["always"]:
        parts.append("WHAT THE PERSON TAUGHT YOU: " + "; ".join([f"'{m['when']}' = {m['means']}" for m in mem["means"]] +
                                                               [f"always {a['text']}" for a in mem["always"]]))
    hint = knowledge.hints(text)
    if hint:
        parts.append(hint)
    parts.append(describe(project))
    rel = [p for p in search(projects, text + " " + " ".join(m["content"] for m in s["messages"][-2:] if m["role"] == "user"))
           if not project or p["id"] != project.get("id")]
    if rel:
        parts.append("RELATED EARLIER PROJECTS: " + " | ".join(f"id={p['id']} {p.get('kind')} \"{p.get('title')}\" idea: {(p.get('idea') or '')[:80]}" for p in rel))
    if s.get("summary"):
        parts.append("EARLIER IN THIS CHAT: " + " / ".join(s["summary"][-12:]))
    dyn = "\n".join(parts)
    msgs = [dict(m) for m in s["messages"]]
    # keep within budget: drop oldest kept messages first (they are already in the summary trail)
    while len(msgs) > 1 and len(SYSTEM) + len(dyn) + sum(len(json.dumps(m)) for m in msgs) > BUDGET:
        msgs.pop(0)
    while msgs and msgs[0].get("role") == "tool":
        msgs.pop(0)
    last = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=None)
    if last is None:
        msgs.append({"role": "user", "content": f"[Ghost context]\n{dyn}"})
    else:
        msgs[last]["content"] = f"[Ghost context]\n{dyn}\n\n[Person says]\n{msgs[last]['content']}"
    return [{"role": "system", "content": SYSTEM}, *msgs]


def recall(text: str, sid: str, k: int = 3) -> list[str]:
    """Memory across chats: the most relevant things said in your EARLIER conversations (BM25 search, no model)."""
    docs = []
    try:
        files = sorted(CHATS.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:200]
    except Exception:
        return []
    for f in files:
        if f.stem == sid:
            continue
        try:
            msgs = json.loads(f.read_text(encoding="utf-8")).get("messages", [])
        except Exception:
            continue
        for a, b in zip(msgs, msgs[1:]):
            if a.get("role") == "user" and b.get("role") == "assistant" and b.get("content"):
                docs.append({"idea": a["content"][:300], "title": "", "kind": "", "text": b["content"][:500]})
    hits = search(docs, text, k=k)
    return [f"They asked: {h['idea'][:160]} → you answered: {h['text'][:300]}" for h in hits]


def build_chat(s: dict, project: dict | None, live_ctx: str = "") -> list[dict]:
    """Normal conversation: no tools, the chat so far, plus what is open (so 'this ad' still makes sense)."""
    mem = knowledge.learned()
    import time as _t
    extra = [f"Right now it is {_t.strftime('%A %d %B %Y, %H:%M')} (the person's local time)."]
    if live_ctx:
        extra.append(live_ctx)
    last = next((m["content"] for m in reversed(s["messages"]) if m["role"] == "user"), "")
    old = recall(last, s.get("id", ""))
    if old:
        extra.append("FROM YOUR EARLIER CHATS WITH THIS PERSON (use only if relevant):\n" + "\n".join(old))
    if project:
        extra.append("For reference, the person has this open in Ghost Studio:\n" + describe(project))
    if mem["means"] or mem["always"]:
        extra.append("What the person taught you: " + "; ".join([f"'{m['when']}' = {m['means']}" for m in mem["means"]] +
                                                                [f"always {a['text']}" for a in mem["always"]]))
    if s.get("summary"):
        extra.append("Earlier in this chat: " + " / ".join(s["summary"][-12:]))
    system = CHAT + ("\n\n" + "\n\n".join(extra) if extra else "")
    msgs = [m for m in s["messages"] if m["role"] in ("user", "assistant") and not m.get("tool_calls") and m.get("content")]
    while len(msgs) > 1 and len(system) + sum(len(m["content"]) for m in msgs) > BUDGET:
        msgs.pop(0)
    return [{"role": "system", "content": system}, *[{"role": m["role"], "content": m["content"]} for m in msgs]]
