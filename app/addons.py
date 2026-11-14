"""Ghost add-ons: free generators that install once and then run on this computer.

  pictures : FLUX.2 klein 4B (Apache 2.0)          photo-real images
  music    : ACE-Step 1.5 (MIT)                    songs with vocals in 50+ languages, or instrumentals
  motion   : Wan 2.2 TI2V 5B (Apache 2.0)          real AI movement from a picture (big computers only)

Every add-on shows real progress while it installs, and can be re-run safely (it skips what is done).
Ghost talks to each one directly; nothing leaves this computer except the one-time downloads.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GHOST = ROOT / ".ghost"
ENGINES = GHOST / "engines"
IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"


# ---------------------------------------------------------------- this computer
def memory_gb() -> float:
    try:
        if IS_WIN:
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("p", ctypes.c_ulong), ("t", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong),
                            ("av", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            m = M(); m.l = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.t / 1024**3, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except Exception:
        return 0.0


def free_disk_gb() -> float:
    try:
        return round(shutil.disk_usage(ROOT).free / 1024**3, 1)
    except Exception:
        return 0.0


def cuda_vram_gb() -> float:
    try:
        import torch
        if torch.cuda.is_available():
            return round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)
    except Exception:
        pass
    return 0.0


def _folder_bytes(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def _hf_size(repo: str) -> int:
    try:
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true", headers={"User-Agent": "GhostStudio"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return sum((s.get("size") or 0) for s in d.get("siblings", []))
    except Exception:
        return 0


def _hf_cache(repo: str) -> Path:
    base = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or \
        str(Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    return Path(base) / ("models--" + repo.replace("/", "--"))


def _hf_cached(repo: str) -> bool:
    snap = _hf_cache(repo) / "snapshots"
    return snap.exists() and any(f.suffix == ".safetensors" for f in snap.rglob("*"))


def _uv() -> str:
    for name in ("uv.exe", "uv"):
        p = GHOST / "bin" / name
        if p.exists():
            return str(p)
    return shutil.which("uv") or ""


# ---------------------------------------------------------------- progress state
STATE: dict[str, dict] = {}


def _st(aid: str) -> dict:
    return STATE.setdefault(aid, {"state": "idle", "pct": 0, "step": "", "detail": "", "started": 0})


def _set(aid: str, **kw):
    _st(aid).update(kw)


def _run_streamed(aid: str, cmd: list, cwd: Path | None = None, env: dict | None = None, lo: float = 0, hi: float = 0, label: str = ""):
    """Runs a command, shows its last line as detail and creeps the bar from lo to hi."""
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env={**os.environ, **(env or {})},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in p.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if line:
            tail = (tail + [line])[-30:]
            el = time.time() - t0
            _set(aid, pct=round(lo + (hi - lo) * min(0.95, el / (el + 240)), 1), detail=(label + " · " if label else "") + line[-110:])
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd[:3]))} failed: " + " | ".join(tail[-4:])[-500:])
    _set(aid, pct=hi)


def _watch_download(aid: str, folder: Path, total: int, lo: float, hi: float, alive):
    t0 = time.time()
    start = _folder_bytes(folder)
    while alive():
        got = max(0, _folder_bytes(folder) - start) if total else _folder_bytes(folder)
        rate = got / max(1, time.time() - t0)
        if total:
            left = (total - got) / rate if rate > 1e5 else None
            _set(aid, pct=round(lo + (hi - lo) * min(0.99, got / total), 1),
                 detail=f"{got / 1e9:.1f} of {total / 1e9:.1f} GB" + (f" · about {int(left // 60) + 1} min left" if left else ""))
        else:
            _set(aid, detail=f"{got / 1e9:.1f} GB downloaded")
        time.sleep(1.5)


# ---------------------------------------------------------------- add-on: pictures
def _pictures_model(cfg) -> str:
    return cfg["models"]["image_mac" if IS_MAC else "image"]


def pictures_installed(cfg) -> bool:
    try:
        import diffusers  # noqa: F401
        import torch  # noqa: F401
    except Exception:
        return False
    return _hf_cached(_pictures_model(cfg))


def _pip(aid, pkgs, lo, hi):
    import importlib.util
    n = len(pkgs)
    for i, pkg in enumerate(pkgs):
        name = pkg.split(">")[0].split("=")[0]
        mod = {"huggingface_hub": "huggingface_hub"}.get(name, name.replace("-", "_"))
        _set(aid, step=f"Installing {name}", detail=f"Part {i + 1} of {n}")
        if importlib.util.find_spec(mod) is None or ">" in pkg:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"Could not install {name}: " + r.stderr[-300:])
        _set(aid, pct=round(lo + (hi - lo) * (i + 1) / n, 1))


def _hf_download(aid, repo, lo, hi):
    import importlib
    importlib.invalidate_caches()
    from huggingface_hub import snapshot_download
    _set(aid, step="Downloading the model", detail="Checking size…")
    total = _hf_size(repo)
    err: list = []
    th = threading.Thread(target=lambda: _safe(lambda: snapshot_download(repo), err), daemon=True)
    th.start()
    _watch_download(aid, _hf_cache(repo), total, lo, hi, th.is_alive)
    if err:
        raise RuntimeError(err[0])


def _safe(fn, err):
    try:
        fn()
    except Exception as e:
        err.append(str(e)[:400])


def install_pictures(cfg):
    _pip("pictures", ["torch", "transformers", "diffusers>=0.43.0", "accelerate", "sentencepiece", "safetensors", "huggingface_hub"], 0, 25)
    if IS_MAC and platform.machine() == "arm64":
        try:
            _pip("pictures", ["mflux>=0.18.0"], 25, 30)  # Apple-chip native speed-up (optional)
        except Exception:
            pass
    _hf_download("pictures", _pictures_model(cfg), 30, 100)


def ensure_fast_pictures(cfg):
    """Existing installs on Apple chips quietly get the MLX speed-up."""
    import importlib.util
    if not (IS_MAC and platform.machine() == "arm64") or importlib.util.find_spec("mflux"):
        return
    try:
        import diffusers  # noqa: F401  (only if pictures are installed)
    except Exception:
        return
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "mflux>=0.18.0"], capture_output=True, text=True)


# ---------------------------------------------------------------- add-on: script AI (Ollama model)
def _script_model() -> str:
    m = memory_gb()
    return "qwen2.5:14b" if m >= 32 else "qwen2.5:7b" if m >= 12 else "qwen2.5:3b"


def ollama_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3):
            return True
    except Exception:
        return False


def script_installed(cfg=None) -> bool:
    from . import llm
    have = llm.installed()
    return any(h.split(":")[0] in ("qwen2.5", "qwen3", "llama3.1", "llama3.2", "gemma2", "gemma3", "mistral") for h in have)


def install_script(cfg):
    aid = "script"
    if not ollama_up():
        raise RuntimeError("Ollama is not running. Install it free from ollama.com, open it once, then press Install again.")
    model = _script_model()
    _set(aid, step=f"Downloading the script AI ({model})", detail="")
    req = urllib.request.Request("http://127.0.0.1:11434/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=7200) as r:
        for line in r:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("error"):
                raise RuntimeError(d["error"])
            if d.get("total"):
                done, tot = d.get("completed", 0), d["total"]
                _set(aid, pct=round(99 * done / tot, 1), detail=f"{done / 1e9:.1f} of {tot / 1e9:.1f} GB")
            elif d.get("status"):
                _set(aid, detail=d["status"])
    cfg["models"]["llm"] = model


# ---------------------------------------------------------------- add-on: music (ACE-Step 1.5)
ACE_DIR = ENGINES / "acestep"
ACE_PORT = 8011


def music_installed(cfg=None) -> bool:
    ck = ACE_DIR / "checkpoints"
    return (ACE_DIR / ".venv").exists() and (ck / "acestep-v15-turbo").exists() and (ck / "vae").exists()


def _get_uv(aid):
    if _uv():
        return _uv()
    _set(aid, step="Getting the installer tool (uv)", detail="")
    (GHOST / "bin").mkdir(parents=True, exist_ok=True)
    env = {"UV_INSTALL_DIR": str(GHOST / "bin"), "INSTALLER_NO_MODIFY_PATH": "1"}
    if IS_WIN:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"]
    else:
        cmd = ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]
    subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True)
    if not _uv():
        raise RuntimeError("Could not get the installer tool (uv). Check your internet and try again.")
    return _uv()


def install_music(cfg):
    aid = "music"
    uv = _get_uv(aid)
    _set(aid, pct=3)
    if not (ACE_DIR / "pyproject.toml").exists():
        _set(aid, step="Downloading the music engine", detail="ACE-Step 1.5")
        ENGINES.mkdir(parents=True, exist_ok=True)
        tgz = ENGINES / "acestep.tgz"
        req = urllib.request.Request("https://github.com/ACE-Step/ACE-Step-1.5/archive/refs/heads/main.tar.gz", headers={"User-Agent": "GhostStudio"})
        with urllib.request.urlopen(req, timeout=600) as r, open(tgz, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp = ENGINES / "acestep_new"
        shutil.rmtree(tmp, ignore_errors=True)
        with tarfile.open(tgz) as t:
            t.extractall(tmp)
        src = next(p for p in tmp.iterdir() if p.is_dir())
        shutil.rmtree(ACE_DIR, ignore_errors=True)
        src.rename(ACE_DIR)
        shutil.rmtree(tmp, ignore_errors=True)
        tgz.unlink(missing_ok=True)
    _set(aid, pct=6, step="Installing the music engine (one time)", detail="This part can take 5–15 minutes")
    _run_streamed(aid, [uv, "sync"], cwd=ACE_DIR, lo=6, hi=40, label="Installing")
    _set(aid, step="Downloading the music model", detail="Checking size…")
    total = _hf_size("ACE-Step/Ace-Step1.5")
    ck = ACE_DIR / "checkpoints"
    ck.mkdir(exist_ok=True)
    done = {"x": False}
    err: list = []

    def dl():
        try:
            r = subprocess.run([uv, "run", "--no-sync", "acestep-download"], cwd=str(ACE_DIR), capture_output=True, text=True,
                               env={**os.environ, "TOKENIZERS_PARALLELISM": "false"})
            if r.returncode != 0 and not music_installed():
                err.append((r.stderr or r.stdout)[-400:])
        finally:
            done["x"] = True
    threading.Thread(target=dl, daemon=True).start()
    _watch_download(aid, ck, total, 40, 100, lambda: not done["x"])
    if err:
        raise RuntimeError("Model download failed: " + err[0])


class MusicServer:
    """Starts ACE-Step's local API when music is needed, and stops it afterwards to free memory."""
    proc: subprocess.Popen | None = None
    lock = threading.Lock()

    @classmethod
    def _url(cls, path: str) -> str:
        return f"http://127.0.0.1:{ACE_PORT}{path}"

    @classmethod
    def up(cls) -> bool:
        try:
            with urllib.request.urlopen(cls._url("/health"), timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    @classmethod
    def start(cls, log=lambda m: None):
        with cls.lock:
            if cls.up():
                return
            env = {**os.environ, "TOKENIZERS_PARALLELISM": "false", "ACESTEP_API_PORT": str(ACE_PORT)}
            if IS_MAC:
                env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"
                if platform.machine() == "arm64":
                    env["ACESTEP_LM_BACKEND"] = "mlx"
            if memory_gb() and memory_gb() < 20:
                env["ACESTEP_OFFLOAD_TO_CPU"] = "true"
            logf = open(GHOST / "music.log", "ab")
            cls.proc = subprocess.Popen([_uv(), "run", "--no-sync", "acestep-api", "--host", "127.0.0.1", "--port", str(ACE_PORT)],
                                        cwd=str(ACE_DIR), env=env, stdout=logf, stderr=subprocess.STDOUT)
            t0 = time.time()
            while not cls.up():
                if cls.proc.poll() is not None:
                    raise RuntimeError("The music engine stopped while starting. See .ghost/music.log")
                if time.time() - t0 > 1200:
                    raise RuntimeError("The music engine took too long to start")
                log(f"Starting the music engine ({int(time.time() - t0)} s)…")
                time.sleep(3)

    @classmethod
    def stop(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(20)
            except Exception:
                cls.proc.kill()
        cls.proc = None

    @classmethod
    def _post(cls, path: str, body: dict, timeout=60) -> dict:
        req = urllib.request.Request(cls._url(path), data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        if d.get("code") not in (None, 200) or d.get("error"):
            raise RuntimeError("Music engine: " + str(d.get("error")))
        return d.get("data")

    @classmethod
    def generate(cls, *, caption: str, lyrics: str, seconds: float, language: str, out_dir: Path, takes: int = 2,
                 seed: int = -1, bpm: int | None = None, describe: str = "", log=lambda m: None) -> list[dict]:
        cls.start(log)
        body = {"prompt": caption, "lyrics": lyrics or "[Instrumental]", "audio_duration": max(10, min(600, float(seconds))),
                "vocal_language": language or "unknown", "audio_format": "mp3", "batch_size": max(1, min(4, takes)),
                "inference_steps": 8, "thinking": True, "use_random_seed": seed < 0, "seed": seed}
        if bpm:
            body["bpm"] = int(bpm)
        if describe:  # the music engine writes caption + lyrics itself
            body.update({"sample_mode": True, "sample_query": describe})
            body.pop("lyrics", None)
        task = cls._post("/release_task", body)
        tid = task.get("task_id") if isinstance(task, dict) else task
        t0 = time.time()
        while True:
            res = cls._post("/query_result", {"task_id_list": [tid]})
            item = res[0] if isinstance(res, list) and res else {}
            if item.get("status") == 1:
                break
            if item.get("status") == 2:
                raise RuntimeError("Music engine could not make this track: " + str(item.get("result"))[:300])
            if time.time() - t0 > 3600:
                raise RuntimeError("Music took too long")
            log(f"Composing… {int(time.time() - t0)} s")
            time.sleep(4)
        results = item.get("result")
        results = json.loads(results) if isinstance(results, str) else results
        out = []
        out_dir.mkdir(parents=True, exist_ok=True)
        for n, r in enumerate(results or [], 1):
            url = r.get("file", "")
            if not url:
                continue
            if url.startswith("/"):
                url = cls._url(url)
            f = out_dir / f"track{int(time.time())}_{n}.mp3"
            with urllib.request.urlopen(url, timeout=300) as resp, open(f, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            m = r.get("metas") or {}
            out.append({"file": str(f), "bpm": m.get("bpm"), "key": m.get("keyscale"), "seconds": m.get("duration"),
                        "lyrics": r.get("lyrics", lyrics), "caption": r.get("prompt", caption)})
        if not out:
            raise RuntimeError("The music engine returned no audio")
        return out


# ---------------------------------------------------------------- add-on: motion (Wan 2.2 TI2V 5B)
WAN = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"


def motion_fits() -> tuple[bool, str]:
    v = cuda_vram_gb()
    if v:
        return (v >= 12, "" if v >= 12 else f"Needs an NVIDIA card with 12 GB or more (this one has {v} GB).")
    m = memory_gb()
    if IS_MAC and platform.machine() == "arm64":
        return (m >= 32, "" if m >= 32 else f"Needs a Mac with 32 GB memory or more (this one has {m:.0f} GB).")
    return (False, "Needs an Apple M-chip Mac with 32 GB+ or an NVIDIA graphics card.")


def motion_installed(cfg=None) -> bool:
    try:
        import diffusers  # noqa: F401
    except Exception:
        return False
    return _hf_cached(WAN)


def install_motion(cfg):
    _pip("motion", ["torch", "transformers", "diffusers>=0.43.0", "accelerate", "sentencepiece", "safetensors", "huggingface_hub",
                    "imageio", "imageio-ffmpeg", "ftfy"], 0, 15)
    _hf_download("motion", WAN, 15, 100)


# ---------------------------------------------------------------- registry
ADDONS = {
    "script": {"name": "Script AI", "desc": "Writes scripts, lyrics and every prompt (much better than the built-in templates). Qwen 2.5 in Ollama (Apache 2.0).",
               "size": "about 5 GB", "time": "a few seconds per script", "install": install_script, "installed": script_installed,
               "fits": lambda: (ollama_up(), "" if ollama_up() else "Needs the free Ollama app: get it at ollama.com, open it, then come back.")},
    "pictures": {"name": "AI pictures", "desc": "Photo-real images for shots and Images only. FLUX.2 klein (Apache 2.0).",
                 "size": "about 10 GB", "time": "1–3 min per picture on a Mac", "install": install_pictures, "installed": pictures_installed,
                 "fits": lambda: ((memory_gb() >= 16 or cuda_vram_gb() >= 8), "" if (memory_gb() >= 16 or cuda_vram_gb() >= 8) else "Needs 16 GB memory or more.")},
    "music": {"name": "Music & songs", "desc": "Songs with vocals in Hindi and 50+ languages, or instrumentals, in any style. ACE-Step 1.5 (MIT).",
              "size": "about 15 GB", "time": "about 5–10 min per song on a Mac, under a minute on NVIDIA", "install": install_music, "installed": music_installed,
              "fits": lambda: ((memory_gb() >= 16 or cuda_vram_gb() >= 6), "" if (memory_gb() >= 16 or cuda_vram_gb() >= 6) else "Needs 16 GB memory or more.")},
    "motion": {"name": "AI motion video", "desc": "Real AI movement from each picture (no speech; a narrator speaks). Wan 2.2 (Apache 2.0). Experimental.",
               "size": "about 25 GB", "time": "about 10–20 min per shot on a 32 GB Mac", "install": install_motion, "installed": motion_installed,
               "fits": motion_fits},
}


def listing(cfg) -> list[dict]:
    out = []
    for aid, a in ADDONS.items():
        ok, why = a["fits"]()
        st = _st(aid)
        installed = False
        try:
            installed = a["installed"](cfg) and st["state"] != "running"
        except Exception:
            pass
        out.append({"id": aid, "name": a["name"], "desc": a["desc"], "size": a["size"], "time": a["time"], "fits": ok, "why": why,
                    "installed": installed, **st})
    return out


def install(aid: str, cfg) -> dict:
    a = ADDONS[aid]
    st = _st(aid)
    if st["state"] == "running":
        return st
    need = float(a["size"].split()[1]) if a["size"].split()[1].replace(".", "").isdigit() else 0
    if need and free_disk_gb() and free_disk_gb() < need + 3:
        _set(aid, state="error", step="Not enough free disk space", detail=f"Needs about {need:.0f} GB free; this computer has {free_disk_gb():.0f} GB.")
        return _st(aid)

    def work():
        _set(aid, state="running", pct=1, step="Starting", detail="", started=time.time())
        try:
            a["install"](cfg)
            _set(aid, state="done", pct=100, step="Ready", detail="Installed. Ghost uses it automatically.")
        except Exception as e:
            _set(aid, state="error", step="Something went wrong", detail=str(e)[:500])
    threading.Thread(target=work, daemon=True).start()
    time.sleep(0.2)
    return _st(aid)
