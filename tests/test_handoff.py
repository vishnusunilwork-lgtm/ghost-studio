"""Offline check of the hand-off: prompts for your own Flow / ChatGPT / Gemini plan, Downloads pickup in order,
your own file for one shot, Ghost making a skipped shot, and the normal finishing. Run:  python3 -m tests.test_handoff"""
from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from app import llm, pipeline, qc
from app import engines as E
from app.editor import ffmpeg_bin, probe

ROOT = Path(__file__).resolve().parent.parent


def runner(root):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    r = object.__new__(pipeline.Runner)
    r.cfg, r.store, r.cancel, r.q, r.current = cfg, pipeline.Store(root), set(), queue.Queue(), None
    r.eng = E.Engines(cfg, force_mock=True)
    return r


def clip(path, seconds, color):
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=720x1280:r=24", "-f", "lavfi",
                    "-i", "sine=f=440:sample_rate=48000", "-t", str(seconds), "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)


def old(path):
    t = time.time() - 3  # "finished downloading" (not still being written)
    os.utime(path, (t, t))


def test_flow_video():
    llm.available = lambda: False
    qc.available = lambda: False
    tmp = Path(tempfile.mkdtemp())
    dl = tmp / "Downloads"
    dl.mkdir()
    pipeline.downloads_dir = lambda: dl
    r = runner(tmp / "projects")
    p = r.store.new(kind="video", idea="a desi dog walking through a Delhi market", language="Hindi", quality="draft", aspect="9:16",
                    seconds=24, voice_mode="oncamera", make_with="flow", ai_music=False, refs=[])
    (dl / "old_download.mp4").write_bytes(b"x" * 5000)  # downloaded before: must be ignored
    os.utime(dl / "old_download.mp4", (time.time() - 3600, time.time() - 3600))
    r._all(p["id"])
    p = r.store.load(p["id"])
    h = p["handoff"]
    assert p["status"] == "handoff" and len(h["prompts"]) == len(p["clips"]) >= 2
    pr = h["prompts"][0]["prompt"]
    assert "vertical 9:16" in pr and "lip-sync" in pr and "pariah" in pr  # Indian words spelled out for the app too
    clip(dl / "flow_a.mp4", 8, "red")
    old(dl / "flow_a.mp4")
    time.sleep(0.05)
    clip(dl / "flow_b.mp4", 6, "blue")
    old(dl / "flow_b.mp4")
    os.utime(dl / "flow_b.mp4", (time.time() - 2.5, time.time() - 2.5))
    got = r.handoff_scan(p["id"])
    assert [g["slot"] for g in got] == [0, 1], got
    assert r.handoff_scan(p["id"]) == []  # nothing picked twice
    r._handoff_finish(p["id"])
    p = r.store.load(p["id"])
    assert p["status"] == "done" and Path(p["final"]).exists()
    assert p["clip_talks"] and (p["voice_mode"] == "both") == (len(p["clips"]) > 2)  # Ghost-made shots get a voice
    assert probe(p["final"])["has_audio"]
    return p


def test_chatgpt_images():
    tmp = Path(tempfile.mkdtemp())
    dl = tmp / "Downloads"
    dl.mkdir()
    pipeline.downloads_dir = lambda: dl
    r = runner(tmp / "projects")
    p = r.store.new(kind="images", idea="an elephant and an ant in a Kerala forest", count=2, quality="draft", aspect="1:1",
                    make_with="chatgpt", refs=[])
    r._all(p["id"])
    p = r.store.load(p["id"])
    pr = p["handoff"]["prompts"]
    assert len(pr) == 2 and "square 1:1" in pr[0]["prompt"] and "smaller" in pr[0]["prompt"]  # scene logic travels too
    assert "Keep exactly the same" in pr[1]["prompt"]
    from PIL import Image
    f = tmp / "mine.png"
    Image.new("RGB", (1024, 1024), "green").save(f)
    r.handoff_take(p["id"], 1, f)  # "Use a file…" for picture 2
    r._handoff_finish(p["id"])  # picture 1 not handed in: Ghost makes it
    p = r.store.load(p["id"])
    assert all(s.get("image") and Path(s["image"]).exists() for s in p["stills"])
    assert p["stills"][1]["look"] == "handoff"


if __name__ == "__main__":
    test_flow_video()
    test_chatgpt_images()
    print("ok handoff")
