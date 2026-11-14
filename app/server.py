"""AI Film Studio Local - web app on http://localhost:7860 (only this computer)."""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engines as E
from . import llm
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
app = FastAPI(title="AI Film Studio Local")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


LOCAL = ROOT / "config.local.yaml"


def _local() -> dict:
    return (yaml.safe_load(LOCAL.read_text(encoding="utf-8")) or {}) if LOCAL.exists() else {}


@app.get("/api/status")
def status():
    f = RUNNER.eng.fal
    m = E.Spend.month()
    return {"engine": RUNNER.eng.name, "gpu": E.gpu_info(), "llm": llm.available(), "models": CFG["models"],
            "makers": RUNNER.eng.plan(), "image_ai": E.image_ai_installed(),
            "api": {"on": f.on, "key_end": f.key[-4:] if f.on else "", "limit": f.limit, "spent": m["usd"],
                    "price": f.price, "model": f.video_model},
            "moves": MOVES, "steps": STEPS, "labels": LABEL, "language": CFG.get("language", "Hindi"),
            "brand": CFG["brand"]["name"], "aspects": list(ASPECTS),
            "version": VERSION + (" (" + BUILD + ")" if BUILD else "")}


@app.get("/api/projects")
def projects():
    return STORE.list()


@app.post("/api/projects")
async def create(idea: str = Form(""), dialogue: str = Form(""), seconds: int = Form(30),
                 voice_mode: str = Form("oncamera"), language: str = Form(""), title: str = Form(""),
                 aspect: str = Form("9:16"), quality: str = Form("final"),
                 reference: UploadFile | None = File(None), music: UploadFile | None = File(None),
                 refs: list[UploadFile] | None = File(None)):
    if not idea.strip() and not dialogue.strip() and not reference:
        raise HTTPException(400, "Write an idea, paste a dialogue or add a reference video")
    p = STORE.new(idea=idea.strip(), dialogue=dialogue.strip(), seconds=max(8, min(180, seconds)),
                  voice_mode=voice_mode, language=language or CFG.get("language", "Hindi"), title=title.strip() or "New ad",
                  mode="reference" if reference else "idea",
                  aspect=aspect if aspect in ASPECTS else "9:16", quality="draft" if quality == "draft" else "final")
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
def update_apply():
    if RUNNER.busy:
        raise HTTPException(409, "An ad is being made right now. Update when it is finished.")
    try:
        sha = updater.apply()
    except Exception as e:
        raise HTTPException(500, "Update failed: " + str(e)[:300])
    updater.restart_soon()
    return {"ok": True, "sha": sha[:7]}


class ApiSettings(BaseModel):
    fal_key: str | None = None
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
    RUNNER.eng.fal = E.Fal(CFG)
    return {"ok": True}


class ImageAIBody(BaseModel):
    go: bool = True


INSTALL = {"state": "idle", "log": ""}


@app.post("/api/settings/image-ai")
def install_image_ai(body: ImageAIBody):
    """One-time: installs the free image AI for Macs / PCs (several GB)."""
    import subprocess
    import sys
    import threading
    if INSTALL["state"] == "running":
        return INSTALL

    def work():
        INSTALL.update(state="running", log="Installing image AI (one time, 5-15 min)…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "torch", "diffusers>=0.43.0", "transformers",
                            "accelerate", "sentencepiece", "safetensors"], capture_output=True, text=True)
        INSTALL.update(state="done" if r.returncode == 0 else "error",
                       log="Installed. The first picture also downloads the model (~8 GB)." if r.returncode == 0 else r.stderr[-600:])
    threading.Thread(target=work, daemon=True).start()
    return INSTALL


@app.get("/api/settings/image-ai")
def image_ai_state():
    return {**INSTALL, "installed": E.image_ai_installed()}


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
