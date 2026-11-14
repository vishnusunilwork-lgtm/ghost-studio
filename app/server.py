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


def _background_boost():
    """After start: give Apple chips the fast picture engine, then pre-load the picture model when idle."""
    import time as _t
    from . import addons
    _t.sleep(5)
    try:
        addons.ensure_fast_pictures(CFG)
        if addons.memory_gb() >= 24 and not RUNNER.busy:
            RUNNER.eng.warm()
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
            "makers": RUNNER.eng.plan(), "addons": _addon_summary(), "image_ai": E.image_ai_installed() and _model_cached() and INSTALL["state"] != "running", "image_ai_pkgs": E.image_ai_installed(), "image_ai_install": INSTALL["state"],
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
                 aspect: str = Form("9:16"), quality: str = Form("final"), kind: str = Form("video"), count: int = Form(4),
                 same_look: str = Form("1"), music_style: str = Form("bollywood"), vocals: str = Form("ai"), music_seconds: int = Form(60), ai_music: str = Form(""),
                 reference: UploadFile | None = File(None), music: UploadFile | None = File(None),
                 refs: list[UploadFile] | None = File(None)):
    if kind == "music" and not idea.strip() and not dialogue.strip():
        raise HTTPException(400, "Describe the song (mood, story, occasion), or paste your lyrics")
    if not idea.strip() and not dialogue.strip() and not reference:
        raise HTTPException(400, "Write an idea, paste a dialogue or add a reference video")
    p = STORE.new(idea=idea.strip(), dialogue=dialogue.strip(), seconds=max(8, min(180, seconds)),
                  voice_mode=voice_mode, language=language or CFG.get("language", "Hindi"), title=title.strip() or "New ad",
                  mode="reference" if reference else "idea",
                  aspect=aspect if aspect in ASPECTS else "9:16", quality="draft" if quality == "draft" else "final",
                  kind=kind if kind in ("images", "music") else "video", count=max(1, min(8, count)),
                  music_style=music_style, vocals=vocals if vocals in ("ai", "mine", "instrumental") else "ai",
                  music_seconds=max(10, min(600, music_seconds)), ai_music=ai_music == "1", same_look=same_look != "0")
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


INSTALL = {"state": "idle", "pct": 0, "step": "", "detail": "", "started": 0, "log": ""}
PKGS = [("torch", 55), ("transformers", 15), ("diffusers>=0.43.0", 10), ("accelerate", 6), ("sentencepiece", 4),
        ("safetensors", 4), ("huggingface_hub", 6)]


def _image_model() -> str:
    import platform
    return CFG["models"]["image_mac" if platform.system() == "Darwin" else "image"]


@app.post("/api/settings/image-ai")
def install_image_ai(body: ImageAIBody):
    """One-time: installs the free image AI and downloads its model, with real progress."""
    import importlib
    import importlib.util
    import subprocess
    import sys
    import threading
    import time as _t
    if INSTALL["state"] == "running":
        return INSTALL

    def work():
        INSTALL.update(state="running", pct=1, step="Starting", detail="", started=_t.time(), log="")
        try:
            total = sum(w for _, w in PKGS)
            done = 0
            for i, (pkg, w) in enumerate(PKGS, 1):
                name = pkg.split(">")[0]
                INSTALL.update(step=f"Installing {name}", detail=f"Part {i} of {len(PKGS)}")
                if importlib.util.find_spec(name.replace("-", "_")) is not None:
                    done += w
                    INSTALL["pct"] = round(30 * done / total, 1)
                    continue
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"Could not install {name}: " + r.stderr[-400:])
                done += w
                INSTALL["pct"] = round(30 * done / total, 1)
            importlib.invalidate_caches()
            from huggingface_hub import HfApi, constants, snapshot_download
            mid = _image_model()
            INSTALL.update(step="Downloading the picture model", detail="Checking size…")
            info = HfApi().model_info(mid, files_metadata=True)
            size = sum((f.size or 0) for f in info.siblings) or 1
            folder = Path(constants.HF_HUB_CACHE) / ("models--" + mid.replace("/", "--"))
            err: list = []
            th = threading.Thread(target=lambda: _dl(snapshot_download, mid, err), daemon=True)
            th.start()
            t0, last = _t.time(), 0
            while th.is_alive():
                got = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) if folder.exists() else 0
                rate = got / max(1, _t.time() - t0)
                left = (size - got) / rate if rate > 1e5 else None
                INSTALL.update(pct=round(30 + 70 * min(1, got / size), 1),
                               detail=f"{got / 1e9:.1f} of {size / 1e9:.1f} GB" + (f" · about {int(left // 60) + 1} min left" if left else ""))
                last = got
                _t.sleep(1.5)
            if err:
                raise RuntimeError(err[0])
            INSTALL.update(state="done", pct=100, step="Ready", detail="Free AI pictures are on. New ads use them automatically.")
        except Exception as e:
            INSTALL.update(state="error", step="Something went wrong", detail=str(e)[:400])

    threading.Thread(target=work, daemon=True).start()
    _t.sleep(0.2)
    return INSTALL


def _model_cached() -> bool:
    """True when the picture model's files are on this computer."""
    import os
    base = Path(os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
                or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    snap = base / ("models--" + _image_model().replace("/", "--")) / "snapshots"
    return snap.exists() and any(f.suffix == ".safetensors" for f in snap.rglob("*"))


def _dl(fn, mid, err):
    try:
        fn(mid)
    except Exception as e:  # e.g. no internet
        err.append(str(e)[:300])


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


@app.post("/api/projects/{pid}/images/{i}/redo")
def redo_image(pid: str, i: int, mode: str = "same"):
    p = _get(pid)
    if not 0 <= i < len(p.get("stills") or []):
        raise HTTPException(404, "No such image")
    RUNNER.submit(pid, "redo_still", i=i, mode="new" if mode == "new" else "same")
    return {"ok": True}


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
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, s in enumerate(p.get("stills") or [], 1):
            if s.get("image") and Path(s["image"]).exists():
                z.write(s["image"], f"{n:02d} {s.get('caption') or 'image'}.png".replace("/", "-"))
    safe = "".join(ch for ch in (p.get("title") or "images") if ch.isalnum() or ch in " -_").strip() or "images"
    return Response(b.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{safe} - images.zip"'})


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
