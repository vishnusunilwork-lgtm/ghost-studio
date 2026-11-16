"""Local language + vision model through Ollama (http://localhost:11434). No keys, no internet."""
from __future__ import annotations

import base64
import json
import re
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
# Optional "Smarter brain": a CHAIN of OpenAI-compatible services (Groq, NVIDIA, OpenRouter, Gemini… free tiers, or paid)
# with YOUR keys. Ghost uses the first one that is ready; when one hits its limit or fails, it switches to the next at
# once, and when none is ready it uses the free brain on this computer (Ollama). Every request carries the whole
# conversation, so a switch keeps the context; an answer cut off mid-way is finished by the next brain from where it
# stopped. PRIVATE = keep everything on this computer (no cloud at all). Keys live only in config.local.yaml.
import time as _time
import urllib.error

CHAIN: list[dict] = []           # [{id, provider, name, base, key, model}] in the order they are tried
STATE: dict[str, dict] = {}      # id -> {cool_until, why, day, n, ms}
PRIVATE = {"on": False}
COMPARE = {"on": True}  # two different brains (e.g. Gemini + ChatGPT) both write scripts / prompts / ideas; the better one is used
CLOUD = {"base": "", "key": "", "model": ""}  # the brain that answered last (shown in the app)
# Brains that sign in instead of using a key (e.g. "google" = your Google Cloud sign-in): name -> (is_on(), headers())
AUTH: dict = {}


# Some services (Groq, behind Cloudflare) refuse Python's default "Python-urllib" User-Agent with 403 / error 1010
UA = "GhostStudio/3 (+https://github.com/vishnusunilwork-lgtm/ghost-studio)"


class CloudDown(Exception):
    pass


class Skip(Exception):
    """This brain can't do this kind of request (e.g. tool calls on a ChatGPT plan): the next one takes it."""


def _responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """Chat messages -> (instructions, Responses API input items)."""
    sys_, items = [], []
    for m in _to_openai(messages):
        if m["role"] == "system":
            sys_.append(m.get("content") or "")
        elif m["role"] == "tool":
            items.append({"role": "user", "content": "[tool result] " + (m.get("content") or "")})
        elif m.get("content"):
            items.append({"role": "user" if m["role"] == "user" else "assistant", "content": m["content"]})
    return "\n\n".join(sys_), items or [{"role": "user", "content": "."}]


def _responses_stream(e: dict, messages: list[dict], timeout: int = 300):
    """Streams text from the Responses API (how a ChatGPT plan is used: store=false, stream=true, no temperature)."""
    instructions, items = _responses_input(messages)
    body = {"model": e["model"], "input": items, "store": False, "stream": True}
    if instructions:
        body["instructions"] = instructions
    done = False
    with _req(e, "/responses", body, timeout=timeout, stream=True) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            t = d.get("type", "")
            if t == "response.output_text.delta" and d.get("delta"):
                yield d["delta"]
            elif t == "response.completed":
                done = True
                break
            elif t in ("response.failed", "response.incomplete", "error"):
                err = (d.get("response") or {}).get("error") or d.get("error") or {}
                raise ConnectionError(f"{t}: {err.get('code') or ''} {err.get('message') or ''}".strip())
    if not done:
        raise ConnectionError("the answer stopped half-way")


def _st(e: dict) -> dict:
    s = STATE.setdefault(e["id"], {"cool_until": 0.0, "why": "", "day": "", "n": 0, "ms": 0})
    today = _time.strftime("%Y-%m-%d", _time.gmtime())
    if s["day"] != today:
        s.update(day=today, n=0)
    return s


def ready() -> list[dict]:
    if PRIVATE["on"]:
        return []
    now = _time.time()
    return [e for e in CHAIN if e.get("key") and e.get("model") and e.get("base") and _st(e)["cool_until"] <= now
            and (not e.get("auth") or (e["auth"] in AUTH and AUTH[e["auth"]][0]()))]


def cloud_on() -> bool:
    return bool(ready())


def _midnight_utc() -> float:
    t = _time.gmtime()
    return _time.time() + (24 * 3600 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)) + 60


def _req(e: dict, path: str, body: dict | None = None, timeout: int = 120, stream: bool = False):
    auth = AUTH[e["auth"]][1]() if e.get("auth") in AUTH else {"Authorization": "Bearer " + e["key"]}
    req = urllib.request.Request(e["base"].rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**auth, "Content-Type": "application/json", "User-Agent": UA,
                                          **({"HTTP-Referer": "https://github.com/vishnusunilwork-lgtm/ghost-studio", "X-Title": "Ghost Studio"}
                                             if "openrouter" in e["base"] else {})},
                                 method="POST" if body is not None else "GET")
    r = urllib.request.urlopen(req, timeout=timeout)
    if stream:
        return r
    with r:
        return json.loads(r.read().decode())


def _cloud_req(path: str, body: dict | None = None, timeout: int = 120, stream: bool = False):
    """Kept for older callers: the first ready brain."""
    r = ready()
    if not r:
        raise CloudDown("no cloud brain ready")
    return _req(r[0], path, body, timeout, stream)


def _why(e: dict, err: Exception) -> str:
    """Decide what an error means for this brain; returns 'skip' (only this request) or sets a cool-down."""
    s = _st(e)
    now = _time.time()
    if isinstance(err, Skip):
        return "skip"
    if isinstance(err, urllib.error.HTTPError):
        try:
            txt = err.read().decode("utf-8", "ignore")[:600].lower()
        except Exception:
            txt = ""
        code = err.code
        if code == 429 and "subscription_sharing" in txt:
            s.update(cool_until=now + 3600, why="your ChatGPT plan's share for Ghost is used up for now (Manage usage in ChatGPT)")
            return "cool"
        if code == 429:
            if re.search(r"per day|daily|rpd|tpd|quota|exhaust|free-models-per-day", txt):
                s.update(cool_until=_midnight_utc(), why="daily free limit reached (back after midnight UTC)")
            else:
                try:
                    wait = float(err.headers.get("retry-after") or 60)
                except Exception:
                    wait = 60
                s.update(cool_until=now + max(5, min(3600, wait)), why=f"busy / per-minute limit (waiting {int(max(5, min(3600, wait)))} s)")
            return "cool"
        if code == 413 or (code == 400 and re.search(r"too large|context|maximum.*tokens|token limit|tools?|function", txt)):
            return "skip"  # this request doesn't fit this model: the next brain takes it, this one stays ready
        if code in (401, 403):
            s.update(cool_until=now + 6 * 3600, why="key not accepted (check it under ⚙)")
            return "cool"
        if code == 402:
            s.update(cool_until=_midnight_utc(), why="out of credits")
            return "cool"
        if code == 404:
            s.update(cool_until=now + 6 * 3600, why="model not available any more (pick another under ⚙)")
            return "cool"
        s.update(cool_until=now + 30, why=f"error {code}")
        return "cool"
    s.update(cool_until=now + 30, why="no answer (network)")
    return "cool"


def _run(fn, label: str = ""):
    """Try each ready brain in order; the first answer wins."""
    tried = []
    for e in ready():
        t0 = _time.time()
        try:
            out = fn(e)
            s = _st(e)
            s.update(n=s["n"] + 1, ms=int((_time.time() - t0) * 1000), why="")
            CLOUD.update(base=e["base"], key="", model=e["model"], name=e.get("name", ""))
            return out
        except Exception as err:
            tried.append(f"{e.get('name')}: {_why(e, err)}")
    raise CloudDown("; ".join(tried) or "no cloud brain ready")


def cloud_models(e: dict) -> list[str]:
    if e.get("auth") == "chatgpt":
        from . import chatgpt_auth
        return chatgpt_auth.models()
    d = _req(e, "/models", timeout=20)
    return sorted(m.get("id", "").removeprefix("models/") for m in d.get("data", []) if m.get("id"))


def _to_openai(messages: list[dict]) -> list[dict]:
    """Ghost keeps messages in Ollama's shape; OpenAI-style APIs need ids that pair tool calls with results."""
    out, pending, n = [], [], 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                n += 1
                f = c.get("function", {})
                calls.append({"id": f"call_{n}", "type": "function",
                              "function": {"name": f.get("name"), "arguments": json.dumps(f.get("arguments") or {})}})
            pending = [c["id"] for c in calls]
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m.get("role") == "tool":
            if pending:
                out.append({"role": "tool", "tool_call_id": pending.pop(0), "content": m.get("content", "")})
            else:  # a result without a call (e.g. after trimming): keep the information as plain text
                out.append({"role": "user", "content": "[tool result] " + m.get("content", "")})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _cloud_chat(messages, tools=None, temperature=0.3, json_mode=False) -> dict:
    def one(e):
        if e.get("api") == "responses":
            if tools:
                raise Skip("tools")
            return {"content": "".join(_responses_stream(e, messages))}
        body = {"model": e["model"], "messages": _to_openai(messages), "temperature": temperature}
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            return _req(e, "/chat/completions", body, timeout=300)["choices"][0]["message"]
        except urllib.error.HTTPError as h:
            if json_mode and h.code == 400:  # some models don't take JSON mode: ask once more without it
                body.pop("response_format", None)
                return _req(e, "/chat/completions", body, timeout=300)["choices"][0]["message"]
            raise
    msg = _run(one)
    calls = []
    for c in msg.get("tool_calls") or []:
        a = c.get("function", {}).get("arguments") or "{}"
        try:
            a = json.loads(a) if isinstance(a, str) else a
        except Exception:
            a = {}
        calls.append({"function": {"name": c.get("function", {}).get("name"), "arguments": a}})
    return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}


def _local_best(small: bool = False) -> str:
    have = [m for m in installed() if not any(v in m for v in ("vl", "llava", "vision", "embed"))]
    if not have:
        raise CloudDown("No cloud brain is ready and the free brain on this computer (Ollama) isn't running.")
    best = min(family_rank(m) for m in have)
    fam = sorted([m for m in have if family_rank(m) == best], key=size_of)
    return fam[0] if small else fam[-1]


def _post(path: str, body: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def available() -> bool:
    if cloud_on():
        return True
    return _ollama_up()


def _ollama_up() -> bool:
    try:
        _post("/api/tags", {}, timeout=3)
        return True
    except Exception:
        try:
            with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3):
                return True
        except Exception:
            return False


def _json_from(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("The local model did not return JSON: " + text[:300])


def _ram_gb() -> float:
    try:
        import os
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except Exception:
        try:
            import subprocess
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()) / 1e9
        except Exception:
            return 16.0


def unload(force: bool = False):
    """Frees the script AI's memory so pictures, video and music run without swapping.
    Only on 64 GB+ computers do both fit comfortably; below that, keeping it loaded slows pictures down badly."""
    if not force and _ram_gb() >= 60:
        return
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=3) as r:
            running = [m["name"] for m in json.loads(r.read()).get("models", [])]
        for m in running:
            _post("/api/generate", {"model": m, "keep_alive": 0}, timeout=20)
    except Exception:
        pass


def installed() -> list[str]:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


PREFER = ["qwen3.6", "qwen3.5", "qwen3", "qwen2.5", "llama3", "gemma", "mistral", "phi"]


def family_rank(name: str) -> int:
    """The optional cloud brain first, then newer local families: qwen3.6 > qwen3.5 > qwen3 > qwen2.5 > others."""
    if name.startswith("cloud:"):
        return -1
    for n, fam in enumerate(PREFER):
        if name.startswith(fam):
            return n
    return len(PREFER)


def thinks(model: str) -> bool:
    """Qwen 3.x can 'think' before answering: great for hard problems, slow for quick jobs. We turn it off."""
    return model.startswith("qwen3")


def pick(model: str, vision: bool = False) -> str:
    """The configured model if it is downloaded, else the best one that is."""
    have = installed()
    if not have or model in have or (model + ":latest") in have:
        return model
    pool = [m for m in have if ("vl" in m or "llava" in m or "vision" in m) == vision] or have
    for fam in PREFER:
        for m in pool:
            if m.startswith(fam):
                return m
    return pool[0]


def ask_json(model: str, prompt: str, images: list[bytes] | None = None, temperature: float = 0.6) -> dict:
    """One call, JSON answer. images = raw PNG/JPEG bytes (vision model only)."""
    if not images and cloud_on():
        try:
            m = _cloud_chat([{"role": "user", "content": prompt}], temperature=temperature, json_mode=True)
            return _json_from(m.get("content", ""))
        except CloudDown:
            model = _local_best(small=True)  # every cloud brain is at its limit: the free brain on this computer
    if model.startswith("cloud:"):
        model = _local_best(small=True)
    model = pick(model, vision=bool(images))
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = [base64.b64encode(b).decode() for b in images]
    body = {"model": model, "messages": [msg], "stream": False, "format": "json",
            "keep_alive": "30m", "options": {"temperature": temperature, "num_ctx": 8192}}
    if thinks(model):
        body["think"] = False
    r = _post("/api/chat", body)
    return _json_from(r.get("message", {}).get("content", ""))


def _ask_one(e: dict, prompt: str, temperature: float = 0.6) -> dict:
    """One JSON answer from one particular brain."""
    if e.get("api") == "responses":
        t0 = _time.time()
        text = "".join(_responses_stream(e, [{"role": "user", "content": prompt}]))
        st = _st(e)
        st.update(n=st["n"] + 1, ms=int((_time.time() - t0) * 1000), why="")
        return _json_from(text)
    body = {"model": e["model"], "messages": [{"role": "user", "content": prompt}], "temperature": temperature,
            "response_format": {"type": "json_object"}}
    t0 = _time.time()
    try:
        m = _req(e, "/chat/completions", body, timeout=300)["choices"][0]["message"]
    except urllib.error.HTTPError as h:
        if h.code != 400:
            raise
        body.pop("response_format", None)
        m = _req(e, "/chat/completions", body, timeout=300)["choices"][0]["message"]
    st = _st(e)
    st.update(n=st["n"] + 1, ms=int((_time.time() - t0) * 1000), why="")
    return _json_from(m.get("content") or "")


def pair() -> list[dict]:
    """Two ready brains from different services, the strongest kinds first (Google Gemini, OpenAI / ChatGPT)."""
    out, seen = [], set()
    for e in sorted(ready(), key=lambda e: 0 if e.get("provider") in ("google", "openai", "chatgpt") else 1):
        if e.get("provider") not in seen:
            seen.add(e.get("provider"))
            out.append(e)
    return out[:2]


def ask_best(model: str, prompt: str, what: str = "answer", temperature: float = 0.6) -> tuple[dict, str]:
    """Like ask_json, but with two different brains ready (and Compare on) both write it at the same time and one of them
    judges the two drafts blind (shown as A / B in random order). Returns (answer, a note for the log)."""
    p = pair() if COMPARE["on"] else []
    if len(p) < 2:
        return ask_json(model, prompt, temperature=temperature), ""
    from concurrent.futures import ThreadPoolExecutor
    import random as _r
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [(e, ex.submit(_ask_one, e, prompt, temperature)) for e in p]
    got = []
    for e, f in futs:
        try:
            d = f.result()
            if isinstance(d, dict) and d:
                got.append((e, d))
        except Exception as err:
            _why(e, err)
    if not got:
        return ask_json(model, prompt, temperature=temperature), ""
    if len(got) == 1:
        return got[0][1], f"{got[0][0].get('name')} wrote the {what} (the other brain didn't answer)"
    _r.shuffle(got)
    jp = (f"Two drafts, A and B, written for the same brief. Judge them blind.\nBRIEF:\n{prompt[:3500]}\n\n"
          f"A:\n{json.dumps(got[0][1], ensure_ascii=False)[:9000]}\n\nB:\n{json.dumps(got[1][1], ensure_ascii=False)[:9000]}\n\n"
          "Which is better? Follows the brief and all its rules exactly, complete, specific and vivid, natural in its language, "
          "creative but believable, easy to film. Return JSON only: {\"best\": \"A\" or \"B\", \"why\": \"one short sentence\"}")
    try:
        j = _ask_one(p[0], jp, 0.0)
        best = 1 if str(j.get("best", "A")).strip().upper().startswith("B") else 0
    except Exception:
        j, best = {}, 0
    win, lose = got[best][0], got[1 - best][0]
    CLOUD.update(base=win["base"], key="", model=win["model"], name=win.get("name", ""))
    return got[best][1], f"Compared {what}s: {win.get('name')}'s was better than {lose.get('name')}'s — {str(j.get('why') or '')[:160]}"


def size_of(name: str) -> float:
    """'qwen2.5:14b' -> 14.0 (billions of parameters, from the tag)."""
    if name.startswith("cloud:"):
        return 1000.0  # treat as the biggest: used for careful work and chat
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", name.lower())
    return float(m.group(1)) if m else 7.0


def text_models() -> list[str]:
    local = [m for m in installed() if not any(v in m for v in ("vl", "llava", "vision", "embed"))]
    r = ready()
    return (["cloud:" + r[0]["model"]] if r else []) + local


def chat(model: str, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2,
         timeout: int = 300) -> dict:
    """One model step with native tool calling (Ollama /api/chat 'tools'). Returns the assistant message."""
    if model.startswith("cloud:"):
        try:
            return _cloud_chat(messages, tools, temperature)
        except CloudDown:
            model = _local_best()
    body = {"model": model, "messages": messages, "stream": False, "keep_alive": "30m",
            "options": {"temperature": temperature, "num_ctx": 8192}}
    if tools:
        body["tools"] = tools
    if thinks(model):
        body["think"] = False  # tool picking needs no long reasoning: answer fast
    return _post("/api/chat", body, timeout=timeout).get("message", {}) or {}


def fast_model(want: str) -> str:
    """Smallest model of the newest installed family (Fast mode, quick jobs). Falls back to the usual pick."""
    have = text_models()
    if not have:
        return want
    best = min(family_rank(m) for m in have)
    fam = sorted([m for m in have if family_rank(m) == best], key=size_of)
    return fam[0] if size_of(fam[0]) <= 5 else pick(want)


def chat_stream(model: str, messages: list[dict], temperature: float = 0.5, timeout: int = 900, think: bool = False, on_think=None):
    """Yields the answer piece by piece as the model writes it (first words in about a second).
    think=True: Qwen 3.x reasons first (better on hard questions, slower); on_think(piece) gets that reasoning."""
    if model.startswith("cloud:"):
        got = []
        for e in ready():
            msgs = _to_openai(messages)
            if got:  # the handoff: the next brain sees what was already said and carries on from there
                msgs += [{"role": "assistant", "content": "".join(got)},
                         {"role": "user", "content": "(Your answer above was cut off. Continue exactly from where it stopped. "
                                                     "Do not repeat anything, do not apologise, do not mention the interruption.)"}]
            body = {"model": e["model"], "messages": msgs, "temperature": temperature, "stream": True}
            t0 = _time.time()
            finished = False
            try:
                if e.get("api") == "responses":
                    for piece in _responses_stream(e, msgs, timeout):
                        got.append(piece)
                        yield piece
                    finished = True
                    s = _st(e)
                    s.update(n=s["n"] + 1, ms=int((_time.time() - t0) * 1000), why="")
                    CLOUD.update(base=e["base"], model=e["model"], name=e.get("name", ""))
                    return
                with _req(e, "/chat/completions", body, timeout=timeout, stream=True) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            finished = True
                            break
                        try:
                            ch = json.loads(data)["choices"][0]
                        except Exception:
                            continue
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if ch.get("finish_reason"):
                            finished = True
                        if piece:
                            got.append(piece)
                            yield piece
                if not finished:
                    raise ConnectionError("the answer stopped half-way")
                s = _st(e)
                s.update(n=s["n"] + 1, ms=int((_time.time() - t0) * 1000), why="")
                CLOUD.update(base=e["base"], model=e["model"], name=e.get("name", ""))
                return
            except Exception as err:
                _why(e, err)
                continue
        model = _local_best()  # no cloud brain left today: finish on this computer
        if got:
            messages = messages + [{"role": "assistant", "content": "".join(got)},
                                   {"role": "user", "content": "(Your answer above was cut off. Continue exactly from where it stopped, without repeating.)"}]
    body = {"model": model, "messages": messages, "stream": True, "keep_alive": "30m",
            "options": {"temperature": temperature, "num_ctx": 8192}}
    if thinks(model):
        body["think"] = bool(think)
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("message") or {}
            if m.get("thinking") and on_think:
                on_think(m["thinking"])
            if m.get("content"):
                yield m["content"]
            if d.get("done"):
                return


def warm(model: str):
    """Load a model into memory ahead of time (an empty request), so the first real answer starts at once."""
    if model.startswith("cloud:"):
        return
    try:
        _post("/api/generate", {"model": model, "keep_alive": "30m"}, timeout=120)
    except Exception:
        pass
