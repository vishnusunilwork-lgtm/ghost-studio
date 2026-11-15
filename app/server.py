"""Ghost Studio - web app on http://localhost:7860 (only this computer)."""
from __future__ import annotations

import json
import re
import time
import shutil
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engines as E
from . import llm
from . import knowledge, planner
from . import updater
from .editor import MOVES
from .pipeline import ASPECTS, LABEL, STEPS, Runner, Store

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def _merge(a: dict, b: dict) -> dict:
    for k, v in (b or {}).items():
        a[k] = _merge(a.get(k, {}), v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


# Your own settings go in config.local.yaml: updates never overwrite that file.
if (ROOT / "config.local.yaml").exists():
    CFG = _merge(CFG, yaml.safe_load((ROOT / "config.local.yaml").read_text(encoding="utf-8")) or {})
VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"
BUILD = ""
STORE = Store(ROOT / CFG["paths"]["projects"])
RUNNER = Runner(CFG, STORE)
app = FastAPI(title="Ghost Studio")

# ---------------------------------------------------------------- local-only guard
# Ghost listens only on this computer, but any website open in the browser could still try to send requests to
# localhost (cross-site request forgery) or reach it through a renamed address (DNS rebinding). So:
#   - the Host must be this computer's own name, and
#   - anything that changes something (POST/PATCH/DELETE) must come from Ghost's own page.
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _is_local(url_or_host: str) -> bool:
    h = url_or_host.split("://", 1)[-1].split("/", 1)[0]
    name = h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"
    return name in _LOCAL_HOSTS


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if host and not _is_local(host):
        return JSONResponse({"detail": "Ghost Studio only answers this computer."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if (origin is not None and (origin == "null" or not _is_local(origin))) or (origin is None and site not in (None, "same-origin", "none")):
            return JSONResponse({"detail": "Blocked: this request did not come from Ghost Studio's own page."}, status_code=403)
    return await call_next(request)


def _background_boost():
    """After start: give Apple chips the fast picture engine, then pre-load the picture model when idle."""
    import time as _t
    from . import addons
    _t.sleep(5)
    try:
        addons.ensure_fast_pictures(CFG)
        if addons.memory_gb() >= 16 and not RUNNER.busy:
            RUNNER.eng.warm()  # the picture model is ready before the first request
    except Exception as e:
        print("boost skipped:", e)


import threading as _th  # noqa: E402
_th.Thread(target=_background_boost, daemon=True).start()
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


LOCAL = ROOT / "config.local.yaml"


def _local() -> dict:
    return (yaml.safe_load(LOCAL.read_text(encoding="utf-8")) or {}) if LOCAL.exists() else {}


def _current() -> dict | None:
    if not RUNNER.current:
        return None
    try:
        p = STORE.load(RUNNER.current)
        return {"id": p["id"], "title": p.get("title", ""), "progress": p.get("progress", 0)}
    except Exception:
        return None


def _addon_summary() -> dict:
    from . import addons
    try:
        return {a["id"]: {"installed": a["installed"], "state": a["state"], "pct": a["pct"]} for a in addons.listing(CFG)}
    except Exception:
        return {}


@app.get("/api/status")
def status():
    f = RUNNER.eng.fal
    m = E.Spend.month()
    return {"engine": RUNNER.eng.name, "gpu": E.gpu_info(), "llm": llm.available(), "models": CFG["models"],
            "makers": RUNNER.eng.plan(), "addons": _addon_summary(), "current": _current(),
            "api": {"on": f.on, "key_end": f.key[-4:] if f.on else "", "limit": f.limit, "spent": m["usd"],
                    "price": f.price, "model": f.video_model},
            "google": {"on": RUNNER.eng.goog.on, "project": RUNNER.eng.goog.project, "key": bool(RUNNER.eng.goog.key),
                       "key_end": RUNNER.eng.goog.key[-4:] if RUNNER.eng.goog.key else "", "model": RUNNER.eng.goog.model,
                       "price": RUNNER.eng.goog.price, "limit": RUNNER.eng.goog.limit, "spent": m["usd"]},
            "brain": {"on": llm.cloud_on(), "model": (llm.ready() or [{}])[0].get("model", ""), "n": len(llm.CHAIN),
                      "private": llm.PRIVATE["on"], "name": (llm.ready() or [{}])[0].get("name", "")},
            "web_search": bool((CFG.get("search") or {}).get("brave_key")),
            "moves": MOVES, "steps": STEPS, "labels": LABEL, "language": CFG.get("language", "Hindi"),
            "brand": CFG["brand"]["name"], "aspects": list(ASPECTS),
            "version": VERSION + (" (" + BUILD + ")" if BUILD else "")}


@app.get("/api/projects")
def projects():
    return STORE.list()


@app.post("/api/projects")
async def create(idea: str = Form(""), dialogue: str = Form(""), seconds: int = Form(30),
                 voice_mode: str = Form("oncamera"), language: str = Form(""), title: str = Form(""),
                 aspect: str = Form("9:16"), quality: str = Form("final"), kind: str = Form("video"), count: int = Form(4),
                 review: str = Form(""), same_look: str = Form("1"), style: str = Form("auto"), voice_gender: str = Form("female"), voice_style: str = Form("warm"), cast: str = Form("auto"), music_style: str = Form("bollywood"), vocals: str = Form("ai"), music_seconds: int = Form(60), ai_music: str = Form(""),
                 reference: UploadFile | None = File(None), music: UploadFile | None = File(None),
                 refs: list[UploadFile] | None = File(None)):
    if kind == "music" and not idea.strip() and not dialogue.strip():
        raise HTTPException(400, "Describe the song (mood, story, occasion), or paste your lyrics")
    if kind == "dub" and not reference:
        raise HTTPException(400, "Add the video to dub")
    if not idea.strip() and not dialogue.strip() and not reference:
        raise HTTPException(400, "Write an idea, paste a dialogue or add a reference video")
    p = STORE.new(idea=idea.strip(), dialogue=dialogue.strip(), seconds=max(8, min(180, seconds)),
                  voice_mode=voice_mode, language=language or CFG.get("language", "Hindi"), title=title.strip() or "New ad",
                  mode="reference" if reference else "idea",
                  aspect=aspect if aspect in ASPECTS else "9:16", quality=quality if quality in ("draft", "best") else "final",
                  kind=kind if kind in ("images", "music", "dub") else "video", count=max(1, min(8, count)),
                  music_style=music_style, vocals=vocals if vocals in ("ai", "mine", "instrumental") else "ai",
                  music_seconds=max(10, min(600, music_seconds)), ai_music=ai_music == "1", review=review == "1", same_look=same_look != "0", voice_gender="male" if voice_gender == "male" else "female",
                  voice_style=voice_style if voice_style in ("warm", "excited", "calm", "news", "devotional") else "warm",
                  cast=cast if cast in ("auto", "people", "animals", "cartoon") else "auto", style=style if style in ("auto", "interview2", "cinematic", "news", "ugc") else "auto")
    d = STORE.dir(p["id"])
    for up, key in ((reference, "reference"), (music, "music")):
        if up and up.filename:
            dst = d / ("input_" + key + Path(up.filename).suffix.lower())
            with dst.open("wb") as f:
                shutil.copyfileobj(up.file, f)
            p[key] = str(dst)
    p["refs"] = []
    for n, up in enumerate([u for u in (refs or []) if u and u.filename][:6]):
        dst = d / f"input_ref{n + 1}{Path(up.filename).suffix.lower() or '.png'}"
        with dst.open("wb") as f:
            shutil.copyfileobj(up.file, f)
        p["refs"].append(str(dst))
    STORE.save(p)
    RUNNER.submit(p["id"], "all")
    return {"id": p["id"]}


def _get(pid: str) -> dict:
    try:
        return STORE.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "Project not found")


@app.get("/api/projects/{pid}")
def project(pid: str):
    return _get(pid)


@app.get("/api/update")
def update_check(force: bool = False):
    return updater.check(force)


@app.post("/api/update")
def update_apply(force: bool = False):
    """force: pause what is being made and update anyway. The job shows as Paused after the restart: press Continue."""
    if RUNNER.busy and not force:
        raise HTTPException(409, "An ad is being made right now. Update when it is finished.")
    if RUNNER.current:
        RUNNER.stop(RUNNER.current)
    try:
        sha = updater.apply()
    except Exception as e:
        raise HTTPException(500, "Update failed: " + str(e)[:300])
    updater.restart_soon()
    return {"ok": True, "sha": sha[:7]}


@app.post("/api/stop")
def stop_app():
    """Stops the background engine until the Ghost Studio icon is opened again."""
    import os
    import threading
    import time as _t
    (ROOT / ".ghost").mkdir(exist_ok=True)
    (ROOT / ".ghost" / "stop").write_text("1")
    threading.Thread(target=lambda: (_t.sleep(0.8), os._exit(0)), daemon=True).start()
    return {"ok": True}


class HfToken(BaseModel):
    token: str = ""


def _load_hf():
    from . import addons
    addons.HF_TOKEN["v"] = str((CFG.get("hf") or {}).get("token") or "")


_load_hf()


@app.post("/api/settings/hf")
def set_hf(body: HfToken):
    """Free Hugging Face read token for models whose makers ask people to accept terms (kept only in config.local.yaml)."""
    t = body.token.strip()
    if t and (not t.startswith("hf_") or len(t) < 20 or " " in t):
        raise HTTPException(400, "That does not look like a Hugging Face token (it starts with hf_).")
    loc = _local()
    if t:
        loc.setdefault("hf", {})["token"] = t
    else:
        loc.pop("hf", None)
    LOCAL.write_text(yaml.safe_dump(loc, allow_unicode=True), encoding="utf-8")
    try:
        LOCAL.chmod(0o600)
    except Exception:
        pass
    CFG["hf"] = {"token": t}
    _load_hf()
    from . import addons
    return {"ok": True, "access": addons.voices_access()}


# Free (and paid) OpenAI-compatible brains. Free limits change often: Ghost reads each one's real answer (429 = limit)
# and moves on, so a wrong number here never breaks anything. "prefer" = the models tried first if the key can use them.
PROVIDERS = {
    "groq": {"name": "Groq", "base": "https://api.groq.com/openai/v1", "free": "free: about 1,000 requests a day per model, very fast",
             "prefer": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.3-70b", "llama"], "keys": "https://console.groq.com/keys"},
    "nvidia": {"name": "NVIDIA NIM", "base": "https://integrate.api.nvidia.com/v1", "free": "free with an NVIDIA developer account: about 10,000 requests a day per model",
               "prefer": ["nvidia/nemotron-3-super-120b-a12b", "openai/gpt-oss-120b", "qwen/qwen3", "meta/llama-3.3-70b"],
               "keys": "https://build.nvidia.com"},
    "gemini": {"name": "Google Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "free": "free tier in AI Studio (Google may use free-tier prompts to improve its products)",
               "prefer": ["gemini-2.5-flash", "gemini-3-flash", "gemini-flash"], "keys": "https://aistudio.google.com/apikey"},
    "openrouter": {"name": "OpenRouter", "base": "https://openrouter.ai/api/v1", "free": "free models: 50 requests a day (1,000 after a one-time $10)",
                   "prefer": ["openrouter/free"], "keys": "https://openrouter.ai/keys"},
    "chutes": {"name": "Chutes", "base": "https://llm.chutes.ai/v1", "free": "check its current free plan", "prefer": ["qwen", "deepseek", "llama"],
               "keys": "https://chutes.ai"},
    "openai": {"name": "OpenAI", "base": "https://api.openai.com/v1", "free": "paid", "prefer": ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"],
               "keys": "https://platform.openai.com/api-keys"},
}


def _load_brain():
    from . import live
    loc = CFG
    chain = list(loc.get("brains") or [])
    old = loc.get("brain") or {}
    if old.get("key") and not any(e.get("key") == old["key"] for e in chain):  # the single key from before 3.25
        chain.insert(0, {"id": "b0", "provider": old.get("provider", "custom"), "name": PROVIDERS.get(old.get("provider", ""), {}).get("name", "My brain"),
                         "base": old.get("base", ""), "key": old["key"], "model": old.get("model", "")})
    llm.CHAIN[:] = chain
    llm.PRIVATE["on"] = bool(loc.get("brain_private"))
    live.BRAVE["key"] = (CFG.get("search") or {}).get("brave_key", "")


_load_brain()


def _save_local(loc):
    LOCAL.write_text(yaml.safe_dump(loc, allow_unicode=True), encoding="utf-8")
    try:
        LOCAL.chmod(0o600)
    except Exception:
        pass


def _save_chain():
    loc = _local()
    loc["brains"] = [dict(e) for e in llm.CHAIN]
    loc.pop("brain", None)
    loc["brain_private"] = llm.PRIVATE["on"]
    _save_local(loc)
    CFG["brains"], CFG["brain_private"] = loc["brains"], loc["brain_private"]
    CFG.pop("brain", None)


def _brains_view():
    now = time.time()
    out = []
    for e in llm.CHAIN:
        st = llm._st(e)
        out.append({"id": e["id"], "name": e.get("name"), "provider": e.get("provider"), "model": e.get("model"),
                    "key_end": (e.get("key") or "")[-4:], "today": st["n"], "ms": st["ms"],
                    "ready": st["cool_until"] <= now, "why": st["why"] if st["cool_until"] > now else "",
                    "back_in": max(0, int(st["cool_until"] - now))})
    return {"private": llm.PRIVATE["on"], "chain": out, "last": llm.CLOUD.get("name") or "",
            "providers": {k: {kk: v[kk] for kk in ("name", "free", "keys")} for k, v in PROVIDERS.items()}}


class BrainAdd(BaseModel):
    provider: str = "groq"
    base: str = ""
    key: str = ""
    model: str = ""


@app.get("/api/settings/brains")
def brains():
    return _brains_view()


@app.post("/api/settings/brains/add")
def brain_add(body: BrainAdd):
    """Add a brain to the chain. The key is checked against the service and kept only in config.local.yaml."""
    pv = PROVIDERS.get(body.provider, {})
    base = (body.base or pv.get("base", "")).strip().rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(400, "Choose a service, or paste its https:// address.")
    key = body.key.strip()
    if len(key) < 16 or " " in key:
        raise HTTPException(400, "That does not look like an API key.")
    e = {"id": "b" + str(int(time.time() * 1000) % 10**9), "provider": body.provider, "name": pv.get("name") or base.split("/")[2],
         "base": base, "key": key, "model": body.model.strip()}
    try:
        models = llm.cloud_models(e)
    except Exception as err:
        raise HTTPException(400, "The service did not accept the key: " + str(err)[:160])
    if not e["model"] or (models and e["model"] not in models):
        pick = next((m for p in pv.get("prefer", []) for m in models if m == p), None) or \
            next((m for p in pv.get("prefer", []) for m in models if p in m), None) or \
            next((m for m in models if re.search(r"instruct|chat|gpt|qwen|llama|gemini|nemotron", m, re.I) and not re.search(r"embed|whisper|tts|guard|vision|image", m, re.I)), None) or \
            (models[0] if models else "")
        e["model"] = pick
    if not e["model"]:
        raise HTTPException(400, "The key works, but this service listed no chat models.")
    llm.CHAIN.append(e)
    _save_chain()
    return {**_brains_view(), "models": models[:300], "added": e["id"]}


class BrainEdit(BaseModel):
    id: str
    move: int = 0
    model: str = ""
    remove: bool = False


@app.post("/api/settings/brains/edit")
def brain_edit(body: BrainEdit):
    i = next((n for n, e in enumerate(llm.CHAIN) if e["id"] == body.id), None)
    if i is None:
        raise HTTPException(404, "Not found")
    if body.remove:
        llm.CHAIN.pop(i)
    else:
        if body.model.strip():
            llm.CHAIN[i]["model"] = body.model.strip()
            llm.STATE.pop(body.id, None)
        if body.move:
            j = max(0, min(len(llm.CHAIN) - 1, i + body.move))
            llm.CHAIN.insert(j, llm.CHAIN.pop(i))
    _save_chain()
    return _brains_view()


@app.get("/api/settings/brains/{bid}/models")
def brain_models(bid: str):
    e = next((x for x in llm.CHAIN if x["id"] == bid), None)
    if not e:
        raise HTTPException(404, "Not found")
    try:
        return {"models": llm.cloud_models(e)[:400], "model": e["model"]}
    except Exception as err:
        raise HTTPException(400, str(err)[:200])


class Private(BaseModel):
    on: bool


@app.post("/api/settings/brains/private")
def brain_private(body: Private):
    """Keep everything on this computer: no cloud brain is used at all, whatever keys are saved."""
    llm.PRIVATE["on"] = body.on
    _save_chain()
    return _brains_view()


class Search(BaseModel):
    brave_key: str = ""


@app.post("/api/settings/search")
def set_search(body: Search):
    loc = _local()
    k = body.brave_key.strip()
    if k:
        loc.setdefault("search", {})["brave_key"] = k
    else:
        loc.pop("search", None)
    CFG["search"] = {"brave_key": k}
    _save_local(loc)
    _load_brain()
    return {"ok": True}


class ApiSettings(BaseModel):
    fal_key: str | None = None
    google_key: str | None = None
    google_project: str | None = None
    google_location: str | None = None
    remove_google: bool = False
    monthly_limit_usd: float | None = None
    remove: bool = False


@app.post("/api/settings/api")
def set_api(body: ApiSettings):
    """The optional video-service key lives only in config.local.yaml on this computer (never uploaded)."""
    loc = _local()
    a = loc.setdefault("api", {})
    if body.remove:
        a.pop("fal_key", None)
    elif body.fal_key is not None:
        k = body.fal_key.strip()
        if len(k) < 20 or " " in k:
            raise HTTPException(400, "That does not look like a key. Copy it again from fal.ai → Keys.")
        a["fal_key"] = k
    if body.remove_google:
        for k in ("google_key", "google_project", "google_location"):
            a.pop(k, None)
    else:
        if body.google_key:
            k = body.google_key.strip()
            if len(k) < 20 or " " in k:
                raise HTTPException(400, "That does not look like a Google Cloud API key. Copy it again from the console → Get API key.")
            a["google_key"] = k
        if body.google_project is not None:
            pr = body.google_project.strip()
            if pr and not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", pr):
                raise HTTPException(400, "A project id looks like my-project-123 (small letters, numbers, dashes).")
            a["google_project"] = pr
        if body.google_location:
            a["google_location"] = body.google_location.strip()
    if body.monthly_limit_usd is not None:
        a["monthly_limit_usd"] = max(1.0, min(5000.0, float(body.monthly_limit_usd)))
    LOCAL.write_text(yaml.safe_dump(loc, allow_unicode=True), encoding="utf-8")
    try:
        LOCAL.chmod(0o600)
    except Exception:
        pass
    CFG.setdefault("api", {}).update(a)
    if body.remove:
        CFG["api"].pop("fal_key", None)
    if body.remove_google:
        for k in ("google_key", "google_project", "google_location"):
            CFG["api"].pop(k, None)
    RUNNER.eng.fal = E.Fal(CFG)
    RUNNER.eng.goog = E.Google(CFG)
    return {"ok": True}


@app.post("/api/google/test")
def google_test():
    """One small picture through your Google Cloud (about $0.04) to prove the connection works."""
    import tempfile
    import time as _t
    g = RUNNER.eng.goog
    if not g.on:
        raise HTTPException(400, "Add a Google Cloud API key or project first.")
    st = _t.time()
    out = Path(tempfile.mkdtemp()) / "test.png"
    try:
        g.image("A steaming glass of masala chai on a wooden table by a window, morning light", out, 512, 512, seed=1)
    except Exception as e:
        raise HTTPException(400, str(e)[:400])
    return {"ok": True, "seconds": round(_t.time() - st, 1), "model": g.works.get("image", ("?",))[0]}


class ClipEdit(BaseModel):
    line: str | None = None
    video_prompt: str | None = None
    camera_move: str | None = None
    shake: float | None = None
    amount: float | None = None
    seconds: float | None = None


@app.patch("/api/projects/{pid}/clips/{i}")
def edit_clip(pid: str, i: int, body: ClipEdit):
    p = _get(pid)
    if not 0 <= i < len(p["clips"]):
        raise HTTPException(404, "No such clip")
    c = p["clips"][i]
    changed = {k: v for k, v in body.model_dump().items() if v is not None}
    if "camera_move" in changed and changed["camera_move"] not in MOVES:
        raise HTTPException(400, "Unknown camera move")
    if "line" in changed and changed["line"] != c.get("line") and "video_prompt" not in changed:
        old = c.get("line", "")
        c["video_prompt"] = c.get("video_prompt", "").replace(old, changed["line"]) if old and old in c.get("video_prompt", "") else c.get("video_prompt", "") + f' Says: "{changed["line"]}".'
    c.update(changed)
    STORE.save(p)
    return {"ok": True, "needs_new_video": bool({"line", "video_prompt", "seconds"} & set(changed))}


@app.post("/api/projects/{pid}/clips/{i}/redo")
def redo(pid: str, i: int):
    _get(pid)
    RUNNER.submit(pid, "redo_clip", i=i)
    return {"ok": True}


@app.post("/api/projects/{pid}/reedit")
def reedit(pid: str):
    _get(pid)
    RUNNER.submit(pid, "reedit")
    return {"ok": True}


@app.post("/api/projects/{pid}/upgrade")
def upgrade(pid: str):
    _get(pid)
    RUNNER.submit(pid, "upgrade")
    return {"ok": True}


@app.post("/api/projects/{pid}/images/{i}/redo")
def redo_image(pid: str, i: int, mode: str = "same"):
    p = _get(pid)
    if not 0 <= i < len(p.get("stills") or []):
        raise HTTPException(404, "No such image")
    RUNNER.submit(pid, "redo_still", i=i, mode="new" if mode == "new" else "same")
    return {"ok": True}


class Change(BaseModel):
    target: str  # image | clip | song
    i: int = 0
    instruction: str = ""
    scope: str = "one"  # one | all
    strength: float = 0.5


@app.post("/api/projects/{pid}/change")
def change(pid: str, body: Change):
    """Live editing: change the SAME picture, shot or song by a plain-words instruction."""
    p = _get(pid)
    ins = " ".join(body.instruction.split())[:300]
    if not ins:
        raise HTTPException(400, "Write what to change")
    scope = "all" if body.scope == "all" else "one"
    if body.target == "image":
        if not 0 <= body.i < len(p.get("stills") or []) or not p["stills"][body.i].get("image"):
            raise HTTPException(404, "No such image")
        if not RUNNER.eng.can_edit():
            raise HTTPException(400, "Editing pictures needs the Pictures add-on (⚙ → Add-ons) or a video-service key.")
        RUNNER.submit(pid, "change_still", i=body.i, instruction=ins, scope=scope)
    elif body.target == "clip":
        if not 0 <= body.i < len(p.get("clips") or []):
            raise HTTPException(404, "No such shot")
        if not RUNNER.eng.can_edit():
            raise HTTPException(400, "Editing shots needs the Pictures add-on (⚙ → Add-ons) or a video-service key.")
        RUNNER.submit(pid, "change_clip", i=body.i, instruction=ins, scope=scope)
    elif body.target == "song":
        if not any(int(t.get("n", 0)) == body.i for t in p.get("tracks") or []):
            raise HTTPException(404, "No such take")
        RUNNER.submit(pid, "change_song", n=body.i, instruction=ins, strength=max(0.1, min(1.0, body.strength)))
    else:
        raise HTTPException(400, "Unknown target")
    return {"ok": True, "remember": ins if body.target != "song" and knowledge.seen(ins) else None}


class Learn(BaseModel):
    kind: str = "always"  # always | means
    text: str = ""
    means: str = ""


@app.get("/api/learned")
def learned_list():
    return knowledge.learned()


@app.post("/api/learned")
def learned_add(body: Learn):
    try:
        return knowledge.add("means" if body.kind == "means" else "always", body.text, body.means)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/learned/remove")
def learned_remove(body: Learn):
    return knowledge.remove("means" if body.kind == "means" else "always", body.text)


class Undo(BaseModel):
    target: str  # image | clip | all
    i: int = 0


@app.post("/api/projects/{pid}/undo")
def undo(pid: str, body: Undo):
    p = _get(pid)
    if RUNNER.current == pid or p.get("status") in ("queued", "running"):
        raise HTTPException(409, "Wait until Ghost finishes this project, then press Undo.")
    if body.target == "image":
        if not 0 <= body.i < len(p.get("stills") or []):
            raise HTTPException(404, "No such image")
        RUNNER.undo_still(pid, body.i)
    elif body.target == "clip":
        if not 0 <= body.i < len(p.get("clips") or []):
            raise HTTPException(404, "No such shot")
        RUNNER.submit(pid, "undo_clip", i=body.i)
    elif body.target == "all":
        if not p.get("undo"):
            raise HTTPException(400, "Nothing to undo")
        if "stills" in p["undo"][-1]:
            RUNNER._undo_all(pid)  # pictures: instant
        else:
            RUNNER.submit(pid, "undo_all")  # shots: re-joins the video (a few seconds)
    return {"ok": True}


class Rewrite(BaseModel):
    text: str = ""
    instruction: str = ""
    kind: str = "text"
    language: str | None = None


@app.post("/api/rewrite")
def rewrite(body: Rewrite):
    """Script, lines, lyrics, prompts: the script AI changes the same text; you check it before saving."""
    try:
        return {"text": planner.rewrite(CFG, body.text[:6000], body.instruction[:300], body.kind, body.language)}
    except Exception as e:
        raise HTTPException(400, str(e)[:300])


@app.post("/api/projects/{pid}/images/{i}/main")
def set_main_look(pid: str, i: int):
    """Use this picture as the main look: later retakes keep its person and place."""
    p = _get(pid)
    st = p.get("stills") or []
    if not 0 <= i < len(st) or not st[i].get("image"):
        raise HTTPException(404, "No such image")
    p["anchor"], p["anchor_i"], p["same_look"] = st[i]["image"], i, True
    STORE.save(p)
    return {"ok": True}


@app.post("/api/projects/{pid}/music/more")
def more_music(pid: str):
    p = _get(pid)
    if p.get("kind") != "music":
        raise HTTPException(400, "Not a music project")
    RUNNER.submit(pid, "more_music")
    return {"ok": True}


class Part(BaseModel):
    start: float
    end: float
    instruction: str = ""
    lyrics: str = ""


@app.post("/api/projects/{pid}/music/{n}/replace")
def replace_part(pid: str, n: int, body: Part):
    """Re-make only start..end seconds of a take (ACE-Step repaint); the rest stays exactly as it was."""
    p = _get(pid)
    tr = next((t for t in p.get("tracks") or [] if int(t.get("n", 0)) == n), None)
    if not tr:
        raise HTTPException(404, "No such take")
    from . import addons
    if not addons.music_installed():
        raise HTTPException(400, "Replacing a part needs the Music & songs add-on (⚙ → Add-ons).")
    length = float(tr.get("seconds") or 600)
    start, end = max(0.0, body.start), min(length, body.end)
    if end - start < 2:
        raise HTTPException(400, f"Choose at least 2 seconds inside the take (it is about {length:.0f} s long)")
    RUNNER.submit(pid, "replace_part", n=n, start=start, end=end, instruction=body.instruction[:300], lyrics=body.lyrics[:3000])
    return {"ok": True}


@app.post("/api/projects/{pid}/music/{n}/stems")
def stems(pid: str, n: int):
    from . import addons
    p = _get(pid)
    if not any(int(t.get("n", 0)) == n for t in p.get("tracks") or []):
        raise HTTPException(404, "No such take")
    if not addons.stems_installed():
        raise HTTPException(400, "Install the Song stems add-on first (⚙ → Add-ons).")
    RUNNER.submit(pid, "stems", n=n)
    return {"ok": True}


@app.get("/api/addons")
def addons_list():
    from . import addons
    return {"addons": addons.listing(CFG), "memory_gb": addons.memory_gb(), "disk_gb": addons.free_disk_gb()}


@app.post("/api/addons/{aid}/install")
def addons_install(aid: str):
    from . import addons
    if aid not in addons.ADDONS:
        raise HTTPException(404, "Unknown add-on")
    return addons.install(aid, CFG)


@app.get("/api/projects/{pid}/zip")
def zip_images(pid: str):
    """All images of an images-only project in one zip."""
    import io
    import zipfile
    from fastapi.responses import Response
    p = _get(pid)
    if any(s.get("dna") for s in p.get("stills") or []):
        raise HTTPException(409, "This project is shrunk (🧬). Press Regrow first to get the full pictures.")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, s in enumerate(p.get("stills") or [], 1):
            if s.get("image") and Path(s["image"]).exists():
                z.write(s["image"], f"{n:02d} {s.get('caption') or 'image'}{Path(s['image']).suffix}".replace("/", "-"))
    safe = "".join(ch for ch in (p.get("title") or "images") if ch.isalnum() or ch in " -_").strip() or "images"
    return Response(b.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{safe} - images.zip"'})


@app.post("/api/projects/{pid}/stop")
def stop_project(pid: str):
    _get(pid)
    RUNNER.stop(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/resume")
def resume_project(pid: str):
    _get(pid)
    RUNNER.submit(pid, "resume")
    return {"ok": True}


@app.patch("/api/projects/{pid}/plan")
def edit_plan(pid: str, body: dict):
    """Edit what Ghost wrote (prompts, lyrics, lines) before or after making."""
    p = _get(pid)
    for i, e in enumerate(body.get("stills") or []):
        if i < len(p.get("stills") or []) and isinstance(e, dict):
            for k in ("caption", "prompt"):
                if isinstance(e.get(k), str) and e[k].strip():
                    p["stills"][i][k] = e[k].strip()
    if isinstance(body.get("song"), dict) and p.get("song"):
        for k in ("title", "caption", "lyrics"):
            v = body["song"].get(k)
            if isinstance(v, str):
                p["song"][k] = v.strip()
                if k == "lyrics":
                    p["song"]["describe"] = ""
        if body["song"].get("title"):
            p["title"] = body["song"]["title"].strip()
    for i, e in enumerate(body.get("clips") or []):
        if i < len(p.get("clips") or []) and isinstance(e, dict):
            for k in ("line", "video_prompt", "image_prompt"):
                if isinstance(e.get(k), str):
                    p["clips"][i][k] = e[k].strip()
    for i, e in enumerate(body.get("dub") or []):
        segs = (p.get("dub") or {}).get("segments") or []
        if i < len(segs) and isinstance(e, dict) and isinstance(e.get("dub"), str) and e["dub"].strip():
            segs[i]["dub"] = e["dub"].strip()
    if isinstance(body.get("image_prompt"), str) and p.get("plan"):
        p["plan"]["image_prompt"] = body["image_prompt"].strip()
    STORE.save(p)
    return {"ok": True}


class Rename(BaseModel):
    title: str


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: Rename):
    p = _get(pid)
    t = " ".join(body.title.split())[:80]
    if not t:
        raise HTTPException(400, "Write a name")
    p["title"] = t
    STORE.save(p)
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    """Deletes the project and all its files from this computer."""
    import time as _t
    _get(pid)
    RUNNER.stop(pid)
    for _ in range(40):  # wait for a running job to let go
        if RUNNER.current != pid:
            break
        _t.sleep(0.25)
    d = STORE.dir(pid).resolve()
    if d.parent != STORE.root.resolve():
        raise HTTPException(400, "Not a project folder")
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@app.get("/api/storage")
def storage():
    """Space used by each project, and how much the disk has left."""
    from . import addons, dna
    items = dna.usage(STORE)
    return {"projects": items, "total": sum(i["bytes"] for i in items), "free_gb": round(addons.free_disk_gb(), 1)}


@app.post("/api/projects/{pid}/shrink")
def shrink_project(pid: str):
    """🧬 Keep the recipe, not the files. The finished video, songs and your uploads stay."""
    from . import dna
    p = _get(pid)
    if RUNNER.current == pid or p.get("status") in ("running", "queued"):
        raise HTTPException(409, "This project is being made. Shrink it when it has finished.")
    r = dna.shrink(p, STORE.dir(pid))
    STORE.save(p)
    return {"ok": True, **r}


@app.post("/api/storage/shrink-all")
def shrink_all(days: float = 14):
    """Shrink every finished project not opened or changed for `days` days."""
    from . import dna
    freed, n = 0, 0
    for it in dna.usage(STORE):
        if it["shrunk"] or it["status"] in ("running", "queued") or RUNNER.current == it["id"]:
            continue
        if time.time() - (it.get("updated") or 0) < days * 86400:
            continue
        p = STORE.load(it["id"])
        freed += dna.shrink(p, STORE.dir(it["id"]))["freed"]
        STORE.save(p)
        n += 1
    return {"ok": True, "projects": n, "freed": freed}


@app.post("/api/projects/{pid}/regrow")
def regrow_project(pid: str):
    p = _get(pid)
    if not p.get("shrunk"):
        return {"ok": True, "note": "Not shrunk"}
    RUNNER.submit(pid, "regrow")
    return {"ok": True}


@app.post("/api/projects/{pid}/rerun")
def rerun(pid: str):
    _get(pid)
    RUNNER.submit(pid, "all")
    return {"ok": True}


@app.get("/files/{pid}/{name:path}")
def files(pid: str, name: str):
    base = STORE.dir(pid).resolve()
    f = (base / name).resolve()
    if base not in f.parents or not f.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(f)


# ---------------------------------------------------------------- Talk to Ghost (assistant)
from fastapi.responses import StreamingResponse  # noqa: E402

from .agent import Agent, context as agent_context  # noqa: E402
from .agent.tools import ToolError  # noqa: E402


class GhostBackend:
    """The real app, as the assistant's tools see it. Each method reuses the same code the buttons use,
    so the assistant can never do something a button could not."""

    def project(self, pid):
        try:
            return STORE.load(pid)
        except Exception:
            return None

    def projects(self):
        out = []
        for row in STORE.list()[:60]:
            p = self.project(row["id"])
            if p:
                out.append(p)
        return out

    def _p(self, pid):
        p = self.project(pid)
        if not p:
            raise ToolError(f"No project with id {pid}")
        return p

    def _call(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except HTTPException as e:
            raise ToolError(str(e.detail))

    @staticmethod
    def _ix(p, key, index, what):
        n = len(p.get(key) or [])
        if not n:
            raise ToolError(f"This project has no {what}s")
        if not 1 <= index <= n:
            raise ToolError(f"There are only {n} {what}s (1–{n})")
        return index - 1

    # read
    def list_projects(self, query="", limit=8):
        ps = self.projects()
        ps = agent_context.search(ps, query, k=limit) if query else ps[:limit]
        return {"text": "; ".join(f"{p['id']}: {p.get('kind', 'video')} “{p.get('title', '')}” ({p.get('status')})" for p in ps) or "No projects yet."}

    def get_project(self, project_id):
        return {"text": agent_context.describe(self._p(project_id))}

    # act
    def open_project(self, project_id):
        p = self._p(project_id)
        return {"text": f"Opened “{p.get('title')}”", "open": project_id}

    def change_picture(self, project_id, index, instruction, all=False):
        p = self._p(project_id)
        i = self._ix(p, "stills", index, "picture")
        self._call(change, project_id, Change(target="image", i=i, instruction=instruction, scope="all" if all else "one"))
        return {"text": f"Changing {'every picture' if all else f'picture {index}'}: {instruction}", "refresh": project_id}

    def retake_picture(self, project_id, index, fresh=False):
        p = self._p(project_id)
        i = self._ix(p, "stills", index, "picture")
        self._call(redo_image, project_id, i, "new" if fresh else "same")
        return {"text": f"{'New take' if fresh else 'Retake'} of picture {index} started", "refresh": project_id}

    def set_main_look(self, project_id, index):
        p = self._p(project_id)
        self._call(set_main_look, project_id, self._ix(p, "stills", index, "picture"))
        return {"text": f"Picture {index} is now the main look", "refresh": project_id}

    def change_shot(self, project_id, index, instruction, all=False):
        p = self._p(project_id)
        i = self._ix(p, "clips", index, "shot")
        self._call(change, project_id, Change(target="clip", i=i, instruction=instruction, scope="all" if all else "one"))
        return {"text": f"Changing {'every shot' if all else f'shot {index}'}: {instruction}", "refresh": project_id}

    def retake_shot(self, project_id, index):
        p = self._p(project_id)
        self._call(redo, project_id, self._ix(p, "clips", index, "shot"))
        return {"text": f"New take of shot {index} started", "refresh": project_id}

    def change_song(self, project_id, take, instruction, amount="medium"):
        p = self._p(project_id)
        if not any(int(t.get("n", 0)) == take for t in p.get("tracks") or []):
            raise ToolError(f"There is no take {take}")
        self._call(change, project_id, Change(target="song", i=take, instruction=instruction,
                                              strength={"little": 0.7, "medium": 0.5, "lot": 0.3}.get(amount, 0.5)))
        return {"text": f"Changing take {take}: {instruction} (it will be a new take)", "refresh": project_id}

    def replace_song_part(self, project_id, take, start, end, instruction=""):
        self._call(replace_part, project_id, take, Part(start=start, end=end, instruction=instruction))
        return {"text": f"Re-making {start:.0f}–{end:.0f} s of take {take} (it will be a new take)", "refresh": project_id}

    def split_song_stems(self, project_id, take):
        self._call(stems, project_id, take)
        return {"text": f"Splitting take {take} into vocals, drums, bass and the rest", "refresh": project_id}

    def more_song_takes(self, project_id):
        self._call(more_music, project_id)
        return {"text": "Making another take", "refresh": project_id}

    def rewrite_text(self, project_id, field, instruction, index=None):
        p = self._p(project_id)
        lang = p.get("language")
        if field == "picture_prompt":
            i = self._ix(p, "stills", index, "picture")
            new = planner.rewrite(CFG, p["stills"][i]["prompt"], instruction, "image_prompt")
            p["stills"][i]["prompt"] = new
        elif field in ("line", "shot_prompt"):
            i = self._ix(p, "clips", index, "shot")
            k = "line" if field == "line" else "video_prompt"
            new = planner.rewrite(CFG, p["clips"][i].get(k, ""), instruction, "line" if k == "line" else "image_prompt", lang)
            self._call(edit_clip, project_id, i, ClipEdit(**{k: new}))
            p = self._p(project_id)
        elif field in ("lyrics", "style"):
            if not p.get("song"):
                raise ToolError("This project has no song")
            k = "lyrics" if field == "lyrics" else "caption"
            new = planner.rewrite(CFG, p["song"].get(k, ""), instruction, "lyrics" if k == "lyrics" else "caption", lang)
            p["song"][k] = new
        else:
            if not p.get("plan"):
                raise ToolError("This project has no main picture prompt")
            new = planner.rewrite(CFG, p["plan"].get("image_prompt", ""), instruction, "image_prompt")
            p["plan"]["image_prompt"] = new
        STORE.save(p)
        return {"text": f"Saved the new text: {new[:300]}", "refresh": project_id}

    def undo(self, project_id, target, index=None):
        p = self._p(project_id)
        if target == "all":
            self._call(undo, project_id, Undo(target="all"))
        elif target == "picture":
            i = self._ix(p, "stills", index, "picture")
            if not p["stills"][i].get("history"):
                raise ToolError(f"Picture {index} has no change to undo")
            self._call(undo, project_id, Undo(target="image", i=i))
        else:
            i = self._ix(p, "clips", index, "shot")
            if not p["clips"][i].get("history"):
                raise ToolError(f"Shot {index} has no change to undo")
            self._call(undo, project_id, Undo(target="clip", i=i))
        return {"text": "Undone", "refresh": project_id}

    def pause(self, project_id):
        self._call(stop_project, project_id)
        return {"text": "Pausing", "refresh": project_id}

    def resume(self, project_id):
        self._call(resume_project, project_id)
        return {"text": "Continuing", "refresh": project_id}

    def remember(self, kind, text, means=""):
        try:
            knowledge.add(kind, text, means)
        except ValueError as e:
            raise ToolError(str(e))
        return {"text": f"I'll remember: {text}" + (f" means {means}" if means else "")}

    # gated (run only after the person's OK)
    def create_project(self, kind, idea, count=None, seconds=None, quality=None, aspect=None, language=None, same_as=None):
        base = self.project(same_as) if same_as else None
        base = base or {}
        q = {"fast": "draft", "quality": "final"}.get(quality or "", base.get("quality") or "draft")
        p = STORE.new(idea=idea, dialogue="", seconds=max(8, min(180, int(seconds or base.get("seconds") or 30))),
                      voice_mode=base.get("voice_mode", "oncamera"), language=language or base.get("language") or CFG.get("language", "Hindi"),
                      title="New ad", mode="idea", aspect=aspect or base.get("aspect") or ("16:9" if kind == "music" else "9:16"),
                      quality=q, kind=kind, count=max(1, min(8, int(count or base.get("count") or 4))),
                      music_style=base.get("music_style", "bollywood"), vocals=base.get("vocals", "ai"),
                      music_seconds=max(10, min(600, int(seconds or base.get("music_seconds") or 60))), ai_music=bool(base.get("ai_music")),
                      review=False, same_look=True, ai_motion=False, style=base.get("style", "auto"), refs=list(base.get("refs") or []))
        RUNNER.submit(p["id"], "all")
        return {"text": f"Started a new {kind} project ({'Fast' if q == 'draft' else 'Quality'})", "open": p["id"]}

    def upgrade_quality(self, project_id):
        self._call(upgrade, project_id)
        return {"text": "Re-making it in Quality", "refresh": project_id}

    def start_again(self, project_id):
        self._call(rerun, project_id)
        return {"text": "Starting again", "refresh": project_id}

    def delete_project(self, project_id):
        p = self._p(project_id)
        self._call(delete_project, project_id)
        return {"text": f"Deleted “{p.get('title')}”"}


AGENT = Agent(CFG, GhostBackend())


class Talk(BaseModel):
    session: str = "default"
    text: str = ""
    project: str | None = None
    confirm: bool | None = None
    deep: bool | None = None
    redo: str | None = None  # "regenerate" | "edit"


@app.post("/api/agent/chat")
def agent_chat(body: Talk):
    """Streams what the assistant does as it happens (one JSON object per line)."""
    import queue as _q
    q: _q.Queue = _q.Queue()

    def work():
        try:
            AGENT.turn(body.session, body.text[:4000], body.project, q.put, body.confirm, body.deep,
                       body.redo if body.redo in ("regenerate", "edit") else None)
        finally:
            q.put(None)
    _th.Thread(target=work, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            if ev is None:
                return
            yield json.dumps(ev, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@app.post("/api/agent/warm")
def agent_warm():
    """The Talk drawer was opened: load the chat model now, so the first answer starts in about a second."""
    from .agent import router
    fast, careful = router.pick_models(CFG)
    if careful and not RUNNER.busy:  # never compete with a picture being made for memory
        _th.Thread(target=llm.warm, args=(careful,), daemon=True).start()
    return {"ok": True, "model": careful}


@app.get("/api/health")
def health():
    """Check Ghost: a quick canary of every part (like the per-platform checks big services run), with timings."""
    import time as _t
    from . import addons
    from .agent import router
    out = []

    def check(name, fn):
        st = _t.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)[:160]
        out.append({"name": name, "ok": ok, "detail": detail, "ms": int((_t.time() - st) * 1000)})

    check("Video editor (ffmpeg)", lambda: (bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "missing: run the installer again"))
    check("Script AI app (Ollama)", lambda: (llm._ollama_up() or llm.cloud_on(), "running" if llm._ollama_up() else
                                            "using your smarter brain" if llm.cloud_on() else "not running: open the Ollama app"))
    fast, careful = router.pick_models(CFG)
    check("Script AI models", lambda: (bool(careful), f"fast: {fast or '-'} · careful: {careful or '-'}" if careful else "none: install Script AI"))

    def think():
        d = llm.ask_json(fast or careful, 'Answer as JSON: {"ok": true, "word": "<the Hindi word for water>"}', temperature=0)
        return bool(d.get("ok")), f"answered: {d.get('word', '?')}"
    if careful:
        check("Script AI answers", think)
    check("Pictures", lambda: (RUNNER.eng.plan()["image"] != "photos", {"local": "on this computer" + (" (fast Apple engine)" if E._mflux_ok() else ""),
                                                                       "service": "video service", "nvidia": "NVIDIA",
                                                                       "photos": "no picture AI: install AI pictures"}[RUNNER.eng.plan()["image"]]))
    g = RUNNER.eng.goog
    check("Google Cloud (Veo + Gemini image)", lambda: (True, (f"on · project {g.project or '(key only)'} · {g.model}") if g.on else "off (optional)"))
    check("Voice", lambda: (True, RUNNER.eng.plan()["voice"]))
    check("Music", lambda: (addons.music_installed() or RUNNER.eng.fal.on, "installed" if addons.music_installed() else "video service" if RUNNER.eng.fal.on else "not installed"))
    from . import live
    check("Live lookups (internet)", lambda: (bool(live.news("Kerala", 1)), "news reachable"))
    check("Free disk", lambda: (addons.free_disk_gb() > 15, f"{addons.free_disk_gb():.0f} GB free"))
    return {"version": VERSION, "checks": out}


@app.get("/api/agent/history")
def agent_history(session: str = "default"):
    s = agent_context.load(session)
    msgs = [{"role": m["role"], "text": m["content"]} for m in s["messages"]
            if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")]
    return {"messages": msgs[-40:], "pending": [str(c[0]) for c in (s.get("pending") or {}).get("calls", [])]}


@app.post("/api/agent/reset")
def agent_reset(body: Talk):
    s = agent_context.load(body.session)
    s.update(messages=[], summary=[], pending=None)
    agent_context.save(s)
    return {"ok": True}
