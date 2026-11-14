"""In-app updates: checks the team's GitHub project and updates + restarts the app with one click.
Uses the GitHub helper that "Start Ghost Studio" installed and signed in. Never touches
projects/, config.local.yaml, .venv/ or .ghost/."""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP = {".venv", "projects", "config.local.yaml", ".ghost", ".git"}
_cache: dict = {"t": 0.0, "data": None}


def repo() -> str:
    try:
        m = re.search(r'^REPO="([^"]+/[^"]+)"', (ROOT / "Start Ghost Studio.command").read_text(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _gh() -> str:
    local = ROOT / ".ghost" / "bin" / "gh"
    return str(local) if local.exists() else (shutil.which("gh") or "")


def _api(path: str, timeout=30) -> bytes:
    r = subprocess.run([_gh(), "api", path], capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-300:] or "GitHub not reachable")
    return r.stdout


def local_sha() -> str:
    f = ROOT / ".ghost" / "version"
    return f.read_text().strip() if f.exists() else ""


def check(force: bool = False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["t"] < 600:
        return _cache["data"]
    r = repo()
    if not r or not _gh():
        data = {"available": False, "reason": "updates not set up"}
    else:
        try:
            import json
            sha = json.loads(_api(f"repos/{r}/commits/main"))["sha"]
            ver = ""
            try:
                ver = base64.b64decode(json.loads(_api(f"repos/{r}/contents/VERSION"))["content"]).decode().strip()
            except Exception:
                pass
            data = {"available": bool(sha) and sha != local_sha(), "remote": ver, "sha": sha[:7]}
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
    import json
    sha = json.loads(_api(f"repos/{r}/commits/main"))["sha"]
    tgz.write_bytes(_api(f"repos/{r}/tarball/{sha}", timeout=300))
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
    (ROOT / ".ghost" / "version").write_text(sha)
    shutil.rmtree(tmp, ignore_errors=True)
    tgz.unlink(missing_ok=True)
    _cache.update(t=0.0, data=None)
    return sha


def restart_soon(delay: float = 1.0):
    """Replace this process with a fresh copy of the app (same port, same window)."""
    def go():
        time.sleep(delay)
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "app.server:app",
                                  "--host", "127.0.0.1", "--port", os.environ.get("GHOST_PORT", "7860")])
    threading.Thread(target=go, daemon=True).start()
