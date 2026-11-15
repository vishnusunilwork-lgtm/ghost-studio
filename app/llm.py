"""Local language + vision model through Ollama (http://localhost:11434). No keys, no internet."""
from __future__ import annotations

import base64
import json
import re
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
# Optional "Smarter brain": any OpenAI-compatible service (OpenAI, Google Gemini, OpenRouter, Groq…) with YOUR key.
# Off unless a key is added in ⚙. When on, Talk to Ghost, scripts and prompts use it; pictures/music stay local.
CLOUD = {"base": "", "key": "", "model": ""}


def cloud_on() -> bool:
    return bool(CLOUD["base"] and CLOUD["key"] and CLOUD["model"])


def _cloud_req(path: str, body: dict | None = None, timeout: int = 120, stream: bool = False):
    req = urllib.request.Request(CLOUD["base"].rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + CLOUD["key"], "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    r = urllib.request.urlopen(req, timeout=timeout)
    if stream:
        return r
    with r:
        return json.loads(r.read().decode())


def cloud_models() -> list[str]:
    d = _cloud_req("/models", timeout=20)
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
    body = {"model": CLOUD["model"], "messages": _to_openai(messages), "temperature": temperature}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    msg = _cloud_req("/chat/completions", body, timeout=300)["choices"][0]["message"]
    calls = []
    for c in msg.get("tool_calls") or []:
        a = c.get("function", {}).get("arguments") or "{}"
        try:
            a = json.loads(a) if isinstance(a, str) else a
        except Exception:
            a = {}
        calls.append({"function": {"name": c.get("function", {}).get("name"), "arguments": a}})
    return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}


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
    if cloud_on():
        pass  # nothing local to keep warm for text
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
        m = _cloud_chat([{"role": "user", "content": prompt}], temperature=temperature, json_mode=True)
        return _json_from(m.get("content", ""))
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


def size_of(name: str) -> float:
    """'qwen2.5:14b' -> 14.0 (billions of parameters, from the tag)."""
    if name.startswith("cloud:"):
        return 1000.0  # treat as the biggest: used for careful work and chat
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", name.lower())
    return float(m.group(1)) if m else 7.0


def text_models() -> list[str]:
    local = [m for m in installed() if not any(v in m for v in ("vl", "llava", "vision", "embed"))]
    return (["cloud:" + CLOUD["model"]] if cloud_on() else []) + local


def chat(model: str, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2,
         timeout: int = 300) -> dict:
    """One model step with native tool calling (Ollama /api/chat 'tools'). Returns the assistant message."""
    if model.startswith("cloud:"):
        return _cloud_chat(messages, tools, temperature)
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
        body = {"model": CLOUD["model"], "messages": _to_openai(messages), "temperature": temperature, "stream": True}
        with _cloud_req("/chat/completions", body, timeout=timeout, stream=True) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    piece = (json.loads(data)["choices"][0].get("delta") or {}).get("content") or ""
                except Exception:
                    continue
                if piece:
                    yield piece
        return
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
