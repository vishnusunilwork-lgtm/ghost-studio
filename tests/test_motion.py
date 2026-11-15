"""Offline check of AI motion plumbing: the worker protocol, prompts, pause, and the pipeline step.
(The real model needs an Apple-chip Mac; here a stand-in worker writes tiny videos.)
Run:  python3 -m tests.test_motion"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app import addons, pipeline
from app import engines as E
from app.editor import ffmpeg_bin

FAKE = r'''
import json, subprocess, sys, time
job = json.loads(sys.stdin.read())
print(json.dumps({"progress": "Loading the motion model", "i": 0}), flush=True)
files = []
for i, s in enumerate(job["shots"]):
    if job.get("slow"):
        time.sleep(30)
    subprocess.run([job["ffmpeg"], "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={job['width']}x{job['height']}:rate=24",
                    "-t", str(s["seconds"]), "-pix_fmt", "yuv420p", s["out"]], check=True)
    files.append(s["out"])
    print(json.dumps({"progress": f"Shot {i + 1} ready", "i": i + 1, "seconds": 1.0}), flush=True)
print(json.dumps({"ok": True, "files": files, "prompts": [s["prompt"] for s in job["shots"]]}), flush=True)
'''


def setup():
    d = Path(tempfile.mkdtemp())
    (d / "motion_worker.py").write_text(FAKE)
    (d / "src").mkdir()
    addons.WORKERS = d
    addons._engine_py = lambda name: Path(sys.executable)
    addons.MOTION_DIR = d
    real = addons.make_motion

    def make(shots, w, h, fast, on_progress=None, cancelled=None, slow=False):
        orig = json.dumps
        addons.json.dumps = lambda o, *a, **k: orig({**o, "ffmpeg": ffmpeg_bin(), "slow": slow} if isinstance(o, dict) and "shots" in o else o, *a, **k)
        try:
            return real(shots, w, h, fast, on_progress, cancelled)
        finally:
            addons.json.dumps = orig
    return d, make


class Eng:
    def plan(self):
        return {"video": "local_motion", "image": "local", "voice": "mac"}


def runner(root):
    r = object.__new__(pipeline.Runner)
    r.cfg, r.store, r.cancel, r.eng = {"quality": {"fps": 24}}, pipeline.Store(root), set(), Eng()
    return r


def project(r):
    p = {"id": "m1", "aspect": "9:16", "quality": "draft", "log": [], "steps": {s: "waiting" for s in pipeline.STEPS}, "status": "running",
         "plan": {"image_prompt": "desi dog"},
         "clips": [{"seed": 5, "seconds": 4, "image_prompt": "An Indian street dog walks through a busy Delhi market",
                    "video_prompt": "The dog trots forward, sniffing. Says in Hindi: \"namaste\". Soft street sound."},
                   {"seed": 6, "seconds": 3, "image_prompt": "The same dog sits by a chai stall", "video_prompt": "", "edits": ["make it a black dog"]}]}
    r.store.dir("m1").mkdir(parents=True, exist_ok=True)
    r.store.save(p)
    return p


def test_motion():
    _, make = setup()
    addons_make = addons.make_motion
    addons.make_motion = make
    r = runner(Path(tempfile.mkdtemp()))
    p = project(r)
    seen = []
    E.free_all = lambda: seen.append("freed")
    r._motion(p, [0, 1], False, True, None)
    p = r.store.load("m1")
    assert all(Path(c["video"]).exists() and Path(c["image"]).exists() for c in p["clips"])
    assert p["steps"]["videos"] == "done" and seen == ["freed"]
    w = subprocess.run([ffmpeg_bin(), "-i", p["clips"][0]["video"]], capture_output=True, text=True).stderr
    assert "448x832" in w  # portrait draft size of the model
    addons.make_motion = addons_make


def test_prompts_and_pause():
    _, make = setup()
    out = Path(tempfile.mkdtemp())
    shots = [{"prompt": "x", "out": str(out / "a.mp4"), "seconds": 1, "seed": 1}]
    assert make(shots, 64, 64, False) == [str(out / "a.mp4")]
    t0 = time.time()
    flag = {"stop": False}
    import threading
    threading.Timer(1.0, lambda: flag.update(stop=True)).start()
    try:
        make(shots, 64, 64, False, cancelled=lambda: flag["stop"], slow=True)
        raise AssertionError("should stop")
    except RuntimeError as e:
        assert "paused" in str(e)
    assert time.time() - t0 < 10  # stopped at once, not after the 30 s shot
    # the pipeline turns the stop into a Pause, and builds clean prompts
    captured = {}

    def fake(shots, w, h, fast, on_progress=None, cancelled=None):
        captured["shots"] = shots
        raise RuntimeError("AI motion stopped (paused)")
    addons.make_motion = fake
    r = runner(Path(tempfile.mkdtemp()))
    p = project(r)
    r.cancel.add("m1")
    E.free_all = lambda: None
    try:
        r._motion(p, [0, 1], False, False, None)
    except E.Cancelled:
        pass
    r.cancel.clear()
    try:
        r._motion(p, [0, 1], False, False, None)
    except RuntimeError:
        pass
    a, b = captured["shots"][0]["prompt"], captured["shots"][1]["prompt"]
    assert "Delhi market" in a and "trots forward" in a and "namaste" not in a and "Says" not in a
    assert "black dog" in b and "Tight close-up" in b


if __name__ == "__main__":
    test_motion()
    test_prompts_and_pause()
    print("ok motion")
