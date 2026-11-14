"""In-app updates from the public GitHub project: no account or sign-in needed.
Compares the VERSION file, downloads the newest copy and restarts the app.
Never touches projects/, config.local.yaml, .venv/ or .ghost/."""
from __future__ import annotations

import os
import re
import shutil
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP = {".venv", "projects", "config.local.yaml", ".ghost", ".git"}
_cache: dict = {"t": 0.0, "data": None}


def repo() -> str:
    f = ROOT / ".ghost" / "repo"
    if f.exists() and "/" in f.read_text():
        return f.read_text().strip()
    for name, pat in (("Start Ghost Studio.command", r'^REPO="([^"]+/[^"]+)"'), ("ghost.ps1", r'^\$REPO = "([^"]+/[^"]+)"')):
        try:
            m = re.search(pat, (ROOT / name).read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1)
        except Exception:
            pass
    return ""


def local_version() -> str:
    f = ROOT / "VERSION"
    return f.read_text().strip() if f.exists() else ""


RAW = os.environ.get("GHOST_RAW", "https://raw.githubusercontent.com")
ARCHIVE = os.environ.get("GHOST_ARCHIVE", "https://github.com")


def _get(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "GhostStudio", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _v(s: str) -> tuple:
    """'2.10.1' -> (2, 10, 1) so versions compare correctly."""
    out = []
    for part in (s or "0").strip().split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def check(force: bool = False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["t"] < 120:
        return _cache["data"]
    r = repo()
    if not r:
        data = {"available": False, "reason": "updates not set up"}
    else:
        try:
            remote = _get(f"{RAW}/{r}/main/VERSION?{int(time.time())}").decode().strip()
            notes = ""
            try:
                for line in _get(f"{RAW}/{r}/main/WHATSNEW?{int(time.time())}").decode().splitlines():
                    if line.startswith(remote + ":"):
                        notes = line.split(":", 1)[1].strip()
                        break
            except Exception:
                pass
            data = {"available": bool(remote) and _v(remote) > _v(local_version()), "remote": remote, "local": local_version(), "notes": notes}
        except Exception as e:
            data = {"available": False, "reason": "offline: " + str(e)[:120]}
    _cache.update(t=time.time(), data=data)
    return data


def apply() -> str:
    r = repo()
    tmp = ROOT / ".ghost" / "new"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    tgz = ROOT / ".ghost" / "new.tgz"
    tgz.write_bytes(_get(f"{ARCHIVE}/{r}/archive/refs/heads/main.tar.gz", timeout=300))
    with tarfile.open(tgz) as t:
        t.extractall(tmp, filter="data") if sys.version_info >= (3, 12) else t.extractall(tmp)
    src = next(p for p in tmp.iterdir() if p.is_dir())
    if not (src / "app" / "server.py").exists():
        raise RuntimeError("Downloaded update looks incomplete")
    for item in src.iterdir():
        if item.name in KEEP:
            continue
        dst = ROOT / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
    try:
        os.chmod(ROOT / "Start Ghost Studio.command", 0o755)
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    tgz.unlink(missing_ok=True)
    _cache.update(t=0.0, data=None)
    return local_version()


def restart_soon(delay: float = 1.0):
    """Replace this process with a fresh copy of the app (same port, same window)."""
    def go():
        time.sleep(delay)
        if os.environ.get("GHOST_SERVICE"):
            os._exit(0)  # the background engine starts the new version straight away
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "app.server:app",
                                  "--host", "127.0.0.1", "--port", os.environ.get("GHOST_PORT", "7860")])
    threading.Thread(target=go, daemon=True).start()
