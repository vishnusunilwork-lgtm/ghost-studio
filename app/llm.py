"""Local language + vision model through Ollama (http://localhost:11434). No keys, no internet."""
from __future__ import annotations

import base64
import json
import re
import urllib.request

OLLAMA = "http://127.0.0.1:11434"


def _post(path: str, body: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def available() -> bool:
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
    On computers with 32 GB or more both fit, so the script AI stays loaded and answers instantly next time."""
    if not force and _ram_gb() >= 31:
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
    """Newer families first: qwen3.6 > qwen3.5 > qwen3 > qwen2.5 > others."""
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
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", name.lower())
    return float(m.group(1)) if m else 7.0


def text_models() -> list[str]:
    return [m for m in installed() if not any(v in m for v in ("vl", "llava", "vision", "embed"))]


def chat(model: str, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2,
         timeout: int = 300) -> dict:
    """One model step with native tool calling (Ollama /api/chat 'tools'). Returns the assistant message."""
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


def chat_stream(model: str, messages: list[dict], temperature: float = 0.5, timeout: int = 600):
    """Yields the answer piece by piece as the model writes it (first words in about a second)."""
    body = {"model": model, "messages": messages, "stream": True, "keep_alive": "30m",
            "options": {"temperature": temperature, "num_ctx": 8192}}
    if thinks(model):
        body["think"] = False
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            try:
                d = json.loads(line)
            except Exception:
                continue
            piece = (d.get("message") or {}).get("content") or ""
            if piece:
                yield piece
            if d.get("done"):
                return


def warm(model: str):
    """Load a model into memory ahead of time (an empty request), so the first real answer starts at once."""
    try:
        _post("/api/generate", {"model": model, "keep_alive": "30m"}, timeout=120)
    except Exception:
        pass
