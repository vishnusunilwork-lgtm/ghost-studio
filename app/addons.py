"""Ghost add-ons: free generators that install once and then run on this computer.

  script   : Qwen 3.5 / 3.6 in Ollama (Apache 2.0)  understands you, writes scripts, lyrics and prompts
  pictures : FLUX.2 klein 4B (Apache 2.0)          photo-real images
  realism  : Z-Image-Turbo (Apache 2.0)            most realistic pictures
  music    : ACE-Step 1.5 (MIT)                    songs with vocals in 50+ languages, covers, replace a part
  voices   : Indic Parler-TTS (Apache 2.0)         natural Malayalam, Hindi, Tamil… narration, voice described in words
  stems    : Demucs (MIT)                          split a song into vocals, drums, bass and the rest
  motion   : FastMetal-5B (Wan2.2, Apache 2.0)     real moving video on an Apple-chip Mac, no cloud

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
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true",
                                     headers={"User-Agent": "GhostStudio", **({"Authorization": "Bearer " + hf_token()} if hf_token() else {})})
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


# ---------------------------------------------------------------- add-on: best pictures (Z-Image-Turbo)
ZIMAGE = "Tongyi-MAI/Z-Image-Turbo"


def realism_installed(cfg=None) -> bool:
    import importlib.util
    if IS_MAC and platform.machine() == "arm64":
        ok = importlib.util.find_spec("mflux") is not None
    else:
        ok = importlib.util.find_spec("diffusers") is not None
    return ok and _hf_cached(ZIMAGE)


def install_realism(cfg):
    if IS_MAC and platform.machine() == "arm64":
        _pip("realism", ["mflux>=0.18.0", "huggingface_hub"], 0, 10)
    else:
        _pip("realism", ["torch", "transformers", "diffusers>=0.36.0", "accelerate", "safetensors", "huggingface_hub"], 0, 10)
    _hf_download("realism", ZIMAGE, 10, 100)


# ---------------------------------------------------------------- add-on: script AI (Ollama model)
def _script_models() -> tuple[list[str], list[str], list[str]]:
    """(main choices, fast choices, vision choices), newest first. Older Ollama apps can't run the newest
    families, so each list falls back to a model they can run."""
    m = memory_gb()
    if m >= 32:
        main = ["qwen3.6:35b-a3b", "qwen3.5:35b-a3b", "qwen3.5:9b", "qwen2.5:14b"]
    elif m >= 15:
        main = ["qwen3.5:9b", "qwen2.5:7b"]
    else:
        main = ["qwen3.5:4b", "qwen2.5:3b"]
    fast = ["qwen3.5:4b", "qwen2.5:3b"] if m >= 15 else []
    vision = ["qwen3-vl:8b", "qwen2.5vl:7b"] if m >= 24 else ["qwen3-vl:4b", "qwen2.5vl:3b"]
    return main, fast, vision


def ollama_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3):
            return True
    except Exception:
        return False


def script_installed(cfg=None) -> bool:
    from . import llm
    import importlib.util
    if importlib.util.find_spec("faster_whisper") is None:
        return False
    have = llm.installed()
    return any(h.split(":")[0] in ("qwen2.5", "qwen3", "qwen3.5", "qwen3.6", "llama3.1", "llama3.2", "gemma2", "gemma3", "mistral") for h in have)


def script_upgrade() -> str:
    """Text shown when an older script AI (Qwen 2.5) is installed and the newer one would understand people better."""
    from . import llm
    have = llm.installed()
    if have and script_installed() and not any(h.startswith(("qwen3.5", "qwen3.6")) for h in have):
        return "Upgrade to Qwen 3.5: understands you better in Talk to Ghost. Replaces Qwen 2.5 and frees its space."
    return ""


def _ollama_pull(aid, model, lo, hi):
    _set(aid, step=f"Downloading {model}", detail="")
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
                _set(aid, pct=round(lo + (hi - lo) * done / tot, 1), detail=f"{done / 1e9:.1f} of {tot / 1e9:.1f} GB")


def _pull_first(aid, choices, lo, hi) -> str:
    """Pull the first model this Ollama can run (newest first)."""
    last = ""
    for m in choices:
        try:
            _ollama_pull(aid, m, lo, hi)
            return m
        except Exception as e:  # e.g. "requires a newer version of Ollama" or not in the library
            last = str(e)[:200]
    raise RuntimeError("Could not download a script model: " + last + ". Update the Ollama app, then try again.")


def _ollama_delete(model):
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/delete", data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"}, method="DELETE")
        urllib.request.urlopen(req, timeout=60).read()
    except Exception:
        pass


def install_script(cfg):
    aid = "script"
    if not ollama_up():
        raise RuntimeError("Ollama is not running. Install it free from ollama.com, open it once, then press Install again.")
    from . import llm
    before = llm.installed()
    main, fast, vision = _script_models()
    model = _pull_first(aid, main, 0, 55)
    if fast and model not in fast:
        try:
            _pull_first(aid, fast, 55, 70)  # small and quick: Talk to Ghost answers simple requests with it
        except Exception:
            pass
    _pull_first(aid, vision, 70, 90)  # eyes: quality check of pictures, reading reference ads
    # ears for reference ads and dubbing: speech-to-text (faster-whisper, MIT)
    _pip(aid, ["faster-whisper"], 90, 93)
    _hf_download(aid, "Systran/faster-whisper-small", 93, 100)
    cfg["models"]["llm"] = model
    if model.startswith("qwen3"):  # the newer family is in: remove the old one Ghost installed (frees 5-15 GB)
        for old in before:
            if old.startswith(("qwen2.5:", "qwen2.5vl:")):
                _ollama_delete(old)


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

    _timer = None

    @classmethod
    def stop_later(cls, seconds: int = 600):
        """Keep the music engine ready for 10 minutes, so the next song starts straight away."""
        if cls._timer:
            cls._timer.cancel()
        cls._timer = threading.Timer(seconds, cls.stop)
        cls._timer.daemon = True
        cls._timer.start()

    @classmethod
    def stop(cls):
        if cls._timer:
            cls._timer.cancel()
            cls._timer = None
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
                 seed: int = -1, bpm: int | None = None, describe: str = "", fast: bool = False, log=lambda m: None,
                 src_audio: str | None = None, cover_strength: float = 0.5, task: str = "cover",
                 repaint: tuple[float, float] | None = None) -> list[dict]:
        cls.start(log)
        body = {"prompt": caption, "lyrics": lyrics or "[Instrumental]", "audio_duration": max(10, min(600, float(seconds))),
                "vocal_language": language or "unknown", "audio_format": "mp3", "batch_size": max(1, min(4, takes)),
                "inference_steps": 8, "thinking": (not fast) or (lyrics or "[Instrumental]") != "[Instrumental]" or bool(describe), "use_random_seed": seed < 0, "seed": seed}
        if bpm:
            body["bpm"] = int(bpm)
        if describe:  # the music engine writes caption + lyrics itself
            body.update({"sample_mode": True, "sample_query": describe})
            body.pop("lyrics", None)
        if src_audio and task == "repaint" and repaint:  # "Replace a part": re-make only start..end, keep the rest
            body.update({"task_type": "repaint", "src_audio_path": str(src_audio), "repainting_start": float(repaint[0]),
                         "repainting_end": float(repaint[1]), "thinking": False})
        elif src_audio:  # "Change" / "Cover my song": keep the melody (and words), change the sound (ACE-Step cover mode)
            body.update({"task_type": "cover", "src_audio_path": str(src_audio),
                         "audio_cover_strength": max(0.1, min(1.0, float(cover_strength))), "thinking": False})
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


# ---------------------------------------------------------------- isolated engines
# Voices and stems each get their own small Python (like the music engine), so their library versions can never
# break the picture models. Ghost talks to them through a tiny worker script.
WORKERS = Path(__file__).resolve().parent / "workers"


def _engine_py(name: str) -> Path:
    d = ENGINES / name / ".venv"
    return d / ("Scripts/python.exe" if IS_WIN else "bin/python")


def _make_engine(aid: str, name: str, pkgs: list[str], lo: float, hi: float):
    uv = _get_uv(aid)
    root = ENGINES / name
    root.mkdir(parents=True, exist_ok=True)
    if not _engine_py(name).exists():
        _run_streamed(aid, [uv, "venv", "--python", "3.11", str(root / ".venv")], lo=lo, hi=lo + 3, label="Preparing")
    _run_streamed(aid, [uv, "pip", "install", "--python", str(_engine_py(name)), *pkgs], lo=lo + 3, hi=hi, label="Installing")


def run_worker(name: str, script: str, job: dict, timeout: int = 3600) -> dict:
    """Run one job in an engine's own Python. job/answer are JSON; errors come back as text."""
    env = {**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1", "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    if hf_token():
        env["HF_TOKEN"] = hf_token()
    r = subprocess.run([str(_engine_py(name)), str(WORKERS / script)], input=json.dumps(job), capture_output=True, text=True,
                       timeout=timeout, env=env)
    try:
        out = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
    except Exception:
        out = {}
    if r.returncode != 0 or out.get("error"):
        raise RuntimeError(out.get("error") or (r.stderr or "worker failed")[-400:])
    return out


# ---------------------------------------------------------------- add-on: natural voices (Indic Parler-TTS)
VOICES = "ai4bharat/indic-parler-tts"
HF_TOKEN = {"v": ""}  # free Hugging Face read token: AI4Bharat asks people to accept its terms before downloading


def hf_token() -> str:
    return HF_TOKEN["v"] or os.environ.get("HF_TOKEN", "")


def voices_access() -> str:
    """'' if the voice model can be downloaded with the saved token, else a plain-words reason."""
    try:
        req = urllib.request.Request(f"https://huggingface.co/api/models/{VOICES}/tree/main",
                                     headers={"User-Agent": "GhostStudio", **({"Authorization": "Bearer " + hf_token()} if hf_token() else {})})
        with urllib.request.urlopen(req, timeout=20):
            pass
        req = urllib.request.Request(f"https://huggingface.co/{VOICES}/resolve/main/config.json",
                                     headers={"User-Agent": "GhostStudio", **({"Authorization": "Bearer " + hf_token()} if hf_token() else {})})
        with urllib.request.urlopen(req, timeout=20):
            return ""
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "needs_token" if not hf_token() else "needs_agree"
        return f"Hugging Face answered {e.code}. Try again later."
    except Exception as e:
        return "No internet: " + str(e)[:80]


def voices_installed(cfg=None) -> bool:
    return _engine_py("voices").exists() and _hf_cached(VOICES)


def install_voices(cfg):
    why = voices_access()
    if why == "needs_token":
        raise RuntimeError("The voice model's makers (AI4Bharat) ask for a free Hugging Face account. Paste your free token below, then press Install again.")
    if why == "needs_agree":
        raise RuntimeError("Your token works, but this account hasn't been given access yet. Press “Open the voice model page”, sign in, "
                           "press “Agree and access” (AI4Bharat may take a little while to approve), then press Try again.")
    if why:
        raise RuntimeError(why)
    base = ["torch", "soundfile", "numpy", "huggingface_hub"]
    try:  # the released package first (PyPI); the newest source if that fails
        _make_engine("voices", "voices", base + ["parler-tts==0.2.3"], 0, 30)
    except Exception:
        _make_engine("voices", "voices", base + ["parler-tts @ https://github.com/huggingface/parler-tts/archive/refs/heads/main.tar.gz"], 0, 30)
    _set("voices", step="Downloading the voice model", detail="Checking size…")
    total = _hf_size(VOICES)
    err: list = []
    th = threading.Thread(target=lambda: _safe(lambda: run_worker("voices", "voice_worker.py", {"download": True, "repo": VOICES}, 7200), err), daemon=True)
    th.start()
    _watch_download("voices", _hf_cache(VOICES), total, 30, 100, th.is_alive)
    if err:
        raise RuntimeError(err[0])


def speak_natural(lines: list[dict], device: str) -> dict:
    """lines: [{text, lang, voice, style, out}] — the model loads once for all of them."""
    return run_worker("voices", "voice_worker.py", {"lines": lines, "device": device, "repo": VOICES})


# ---------------------------------------------------------------- add-on: stems (Demucs)
def stems_installed(cfg=None) -> bool:
    return _engine_py("stems").exists() and (ENGINES / "stems" / "ready").exists()


def install_stems(cfg):
    _make_engine("stems", "stems", ["torch", "torchaudio", "soundfile", "demucs"], 0, 80)
    _set("stems", step="Downloading the separation model", detail="about 80 MB")
    r = subprocess.run([str(_engine_py("stems")), "-c", "from demucs.pretrained import get_model; get_model('htdemucs')"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Could not download the stems model: " + r.stderr[-300:])
    (ENGINES / "stems" / "ready").write_text("htdemucs")


def split_stems(src: Path, out_dir: Path, two: bool = False) -> dict:
    """Vocals, drums, bass and the rest as MP3s (htdemucs, MIT). two=True: just vocals + everything else (for dubbing)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dev = "cuda" if cuda_vram_gb() else "cpu"  # Demucs on CPU works on every Mac
    cmd = [str(_engine_py("stems")), "-m", "demucs", "-n", "htdemucs", "-d", dev, "--mp3", "-o", str(out_dir)]
    if two:
        cmd += ["--two-stems", "vocals"]
    r = subprocess.run(cmd + [str(src)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Stem split failed: " + r.stderr[-300:])
    found = {f.stem: str(f) for f in (out_dir / "htdemucs" / Path(src).stem).glob("*.mp3")}
    if not found:
        raise RuntimeError("Stem split made no files")
    return found


# ---------------------------------------------------------------- add-on: AI motion (FastMetal-5B on Apple chips)
# Real moving video on the Mac itself: Wan2.2-TI2V-5B (Apache 2.0) distilled by Hao AI Lab to 3 steps + INT8 for MLX
# (FastVideo, Apache 2.0). Measured by its makers on an M4 Max 36 GB: about 47 s (fast) to 151 s (720p) per 5 s shot.
MOTION_SRC_COMMIT = "1a41551d529c1298d0600a2e394f9537219748ea"  # FastVideo main, 14 Nov 2026 (pinned: tested layout)
MOTION_REPO = "FastVideo/FastMetal-5B-QAD"
MOTION_DIR = ENGINES / "motion"


def motion_fits() -> tuple[bool, str]:
    if not (IS_MAC and platform.machine() == "arm64"):
        return False, "Needs a Mac with an Apple chip (M1 or newer). With an NVIDIA computer or no Mac, use Google Cloud or the video service."
    if memory_gb() and memory_gb() < 16:
        return False, "Needs 16 GB memory or more."
    return True, ""


def motion_installed(cfg=None) -> bool:
    return _engine_py("motion").exists() and (MOTION_DIR / "ready").exists()


def install_motion(cfg):
    ok, why = motion_fits()
    if not ok:
        raise RuntimeError(why)
    uv = _get_uv("motion")
    MOTION_DIR.mkdir(parents=True, exist_ok=True)
    src = MOTION_DIR / "src"
    if not (src / "pyproject.toml").exists():
        _set("motion", step="Downloading the motion engine code", detail="FastVideo (Apache 2.0)")
        tgz = MOTION_DIR / "src.tgz"
        urllib.request.urlretrieve(f"https://codeload.github.com/hao-ai-lab/FastVideo/tar.gz/{MOTION_SRC_COMMIT}", tgz)
        tmp = MOTION_DIR / "src_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with tarfile.open(tgz) as t:
            t.extractall(tmp, filter="data")
        top = next(tmp.iterdir())
        shutil.rmtree(src, ignore_errors=True)
        top.rename(src)
        shutil.rmtree(tmp, ignore_errors=True)
        tgz.unlink(missing_ok=True)
    _set("motion", pct=4)
    if not _engine_py("motion").exists():
        _run_streamed("motion", [uv, "venv", "--python", "3.12", str(MOTION_DIR / ".venv")], lo=4, hi=6, label="Preparing")
    _run_streamed("motion", [uv, "pip", "install", "--python", str(_engine_py("motion")), "-e", f"{src}[mlx]"],
                  lo=6, hi=30, label="Installing (about 4 GB)")
    model = MOTION_DIR / "FastMetal-5B-QAD"
    _set("motion", step="Downloading the motion model", detail="Checking size…")
    total = _hf_size(MOTION_REPO)
    err: list = []
    th = threading.Thread(target=lambda: _safe(lambda: run_worker("motion", "motion_worker.py", {"download": True, "model": str(model)}, 14400), err),
                          daemon=True)
    th.start()
    _watch_download("motion", model, total, 30, 100, th.is_alive)
    if err:
        raise RuntimeError(err[0])
    (MOTION_DIR / "ready").write_text(MOTION_REPO)


def make_motion(shots: list[dict], width: int, height: int, fast: bool, on_progress=None, cancelled=None) -> list[str]:
    """All shots of an ad in one run (the models load once). shots: [{prompt, out, seconds, seed}]."""
    job = {"src": str(MOTION_DIR / "src"), "model": str(MOTION_DIR / "FastMetal-5B-QAD"), "shots": shots,
           "width": width, "height": height, "fast": fast}
    env = {**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1", "PYTORCH_ENABLE_MPS_FALLBACK": "1", "TOKENIZERS_PARALLELISM": "false",
           "FASTVIDEO_ATTENTION_BACKEND": "TORCH_SDPA", "PYTHONPATH": str(MOTION_DIR / "src")}
    p = subprocess.Popen([str(_engine_py("motion")), str(WORKERS / "motion_worker.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env, cwd=str(MOTION_DIR / "src"))
    p.stdin.write(json.dumps(job))
    p.stdin.close()
    err_tail: list[str] = []

    def drain():
        for x in p.stderr:
            err_tail.append(x)
            del err_tail[:-40]
    threading.Thread(target=drain, daemon=True).start()

    def watch():  # Pause / Update stop the model run straight away, not at the end of a shot
        while p.poll() is None:
            if cancelled and cancelled():
                p.kill()
            time.sleep(1)
    threading.Thread(target=watch, daemon=True).start()
    last: dict = {}
    try:
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            last = ev
            if on_progress and ev.get("progress"):
                on_progress(ev)  # may raise Pause/Cancel: the model run is stopped below
            if cancelled and cancelled():
                break
    except BaseException:
        p.kill()
        raise
    if not last.get("ok"):
        p.kill()
    p.wait()
    if last.get("ok"):
        return last["files"]
    if cancelled and cancelled():
        raise RuntimeError("AI motion stopped (paused)")
    raise RuntimeError("AI motion: " + (last.get("error") or "".join(err_tail)[-400:] or "stopped"))


ADDONS = {
    "script": {"name": "Script AI", "desc": "Understands you in Talk to Ghost, writes scripts, lyrics and every prompt, translates for dubbing, and checks each picture for bad hands and eyes. Qwen 3.5 in Ollama (Apache 2.0).",
               "size": "about 8 GB", "time": "a few seconds per script", "install": install_script, "installed": script_installed,
               "fits": lambda: (ollama_up(), "" if ollama_up() else "Needs the free Ollama app: get it at ollama.com, open it, then come back.")},
    "realism": {"name": "Best pictures", "desc": "Most realistic people, hands and eyes for Final pictures. Z-Image-Turbo (Apache 2.0). Draft keeps the fast model.",
                "size": "about 20 GB", "time": "about 40–70 s per picture on a Mac",
                "install": install_realism, "installed": realism_installed,
                "fits": lambda: ((memory_gb() >= 16 or cuda_vram_gb() >= 12), "" if (memory_gb() >= 16 or cuda_vram_gb() >= 12) else "Needs 16 GB memory or more.")},
    "pictures": {"name": "AI pictures", "desc": "Photo-real images for shots and Images only. FLUX.2 klein (Apache 2.0).",
                 "size": "about 10 GB", "time": "about 10–35 s per picture on a Mac", "install": install_pictures, "installed": pictures_installed,
                 "fits": lambda: ((memory_gb() >= 16 or cuda_vram_gb() >= 8), "" if (memory_gb() >= 16 or cuda_vram_gb() >= 8) else "Needs 16 GB memory or more.")},
    "music": {"name": "Music & songs", "desc": "Songs with vocals in Hindi and 50+ languages or instrumentals, covers of your own tune or humming, and replacing any part of a song. ACE-Step 1.5 (MIT).",
              "size": "about 15 GB", "time": "about 5–10 min per song on a Mac, under a minute on NVIDIA", "install": install_music, "installed": music_installed,
              "fits": lambda: ((memory_gb() >= 16 or cuda_vram_gb() >= 6), "" if (memory_gb() >= 16 or cuda_vram_gb() >= 6) else "Needs 16 GB memory or more.")},
    "voices": {"name": "Natural voices", "desc": "Human-sounding narration in Malayalam, Hindi, Tamil, English and more Indian languages, "
               "female or male, warm, excited, calm or announcer. Used for ads and dubbing. Indic Parler-TTS (Apache 2.0).",
               "size": "about 6 GB", "time": "a few seconds per sentence", "install": install_voices, "installed": voices_installed,
               "fits": lambda: ((memory_gb() >= 8 or cuda_vram_gb() >= 4), "" if (memory_gb() >= 8 or cuda_vram_gb() >= 4) else "Needs 8 GB memory or more.")},
    "stems": {"name": "Song stems", "desc": "Split any song into vocals, drums, bass and the rest. Also lets dubbing keep the background music. Demucs (MIT).",
              "size": "about 2 GB", "time": "about 1–2 min per song", "install": install_stems, "installed": stems_installed,
              "fits": lambda: (True, "")},
    "motion": {"name": "AI motion (on this Mac)", "desc": "Real moving video made on your Mac, no cloud and no cost per video: a dog really walks through the market, "
               "people move, the camera travels. FastMetal-5B (Wan2.2, Apache 2.0). No sound in the clip: Ghost adds voice and music.",
               "size": "about 24 GB", "time": "about 1–3 min per shot on M4 Max, slower on older Macs", "install": install_motion,
               "installed": motion_installed, "fits": motion_fits},
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
        up = ""
        if aid == "script" and installed:
            try:
                up = script_upgrade()
            except Exception:
                pass
        token = aid == "voices" and not installed and not hf_token()
        out.append({"id": aid, "name": a["name"], "desc": a["desc"], "size": a["size"], "time": a["time"], "fits": ok, "why": why,
                    "installed": installed and not up, "upgrade": up, "needs_token": token, **st})
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
