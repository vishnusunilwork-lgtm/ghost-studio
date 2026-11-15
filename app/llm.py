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


def unload():
    """Frees the script AI's memory so pictures, video and music run without swapping."""
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


PREFER = ["qwen2.5", "qwen3", "llama3", "gemma", "mistral", "phi"]


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
    r = _post("/api/chat", {"model": model, "messages": [msg], "stream": False, "format": "json",
                            "keep_alive": "10m", "options": {"temperature": temperature, "num_ctx": 8192}})
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
    body = {"model": model, "messages": messages, "stream": False, "keep_alive": "10m",
            "options": {"temperature": temperature, "num_ctx": 8192}}
    if tools:
        body["tools"] = tools
    return _post("/api/chat", body, timeout=timeout).get("message", {}) or {}
