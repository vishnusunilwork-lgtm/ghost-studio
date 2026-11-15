"""Offline check of 🧬 Shrink / Regrow with a stand-in picture model that is exactly repeatable (like a real one
with the same seed and settings). Run:  python3 -m tests.test_dna"""
from __future__ import annotations

import hashlib
import random
import tempfile
from pathlib import Path

from app import dna, pipeline


class Eng:
    calls = []

    def image(self, prompt, out, w, h, seed=0, ref_images=None, **kw):
        """Busy noise pictures (PNG-heavy, like photos), fully decided by prompt + seed + reference."""
        from PIL import Image, ImageFilter
        key = prompt + str(seed) + "".join(Path(r).name for r in ref_images or [])
        rnd = random.Random(hashlib.sha256(key.encode()).hexdigest())
        im = Image.frombytes("L", (w, h), rnd.randbytes(w * h)).convert("RGB")
        im = Image.blend(im, Image.new("RGB", (w, h), tuple(rnd.randint(0, 255) for _ in range(3))), 0.6).filter(ImageFilter.SMOOTH)
        im.save(out)
        Eng.calls.append(Path(out).name)
        return out

    def plan(self):
        return {"image": "local", "video": "photo_motion", "voice": "mac"}


def runner(root):
    r = object.__new__(pipeline.Runner)
    r.cfg = {"quality": {"image_steps": 4, "image_steps_draft": 2, "fps": 24}}
    r.store, r.cancel, r.eng = pipeline.Store(root), set(), Eng()
    import queue
    r.q = queue.Queue()
    return r


def test_images_shrink_and_regrow():
    root = Path(tempfile.mkdtemp())
    r = runner(root)
    p = {"id": "i1", "kind": "images", "aspect": "1:1", "quality": "final", "log": [], "steps": {s: "done" for s in pipeline.STEPS},
         "status": "done", "same_look": True, "anchor_i": 0, "refs": [],
         "stills": [{"prompt": "a desi dog", "seed": 11, "caption": "dog"}, {"prompt": "the dog sleeps", "seed": 22, "caption": "sleep"},
                    {"prompt": "the dog runs", "seed": 33, "caption": "run"}]}
    d = r.store.dir("i1")
    d.mkdir(parents=True)
    _, (ow, oh), _ = pipeline.sizes(p)
    w, h = ow // 16 * 16, oh // 16 * 16
    for i, s in enumerate(p["stills"]):
        f = d / f"image{i + 1:02d}_{s['seed']}.png"
        anchor = d / "image01_11.png" if i else None
        Eng().image((r.LOCK + s["prompt"]) if anchor else s["prompt"], f, w, h, seed=s["seed"], ref_images=[anchor] if anchor else None)
        s.update(image=str(f), status="done", look="same" if i else "fresh")
    p["anchor"] = p["stills"][0]["image"]
    # picture 3 was ✨ Changed: its recipe isn't pure any more -> kept as JPEG
    edited = d / "shot03_edit123.png"
    Path(p["stills"][2]["image"]).rename(edited)
    p["stills"][2].update(image=str(edited), edits=["make it a black dog"], history=[{"image": "x"}])
    (d / "image02_22_retake.png").write_bytes(b"0" * 1000)  # an unused leftover
    originals = {i: Path(s["image"]).read_bytes() for i, s in enumerate(p["stills"])}
    r.store.save(p)
    before = dna.folder_bytes(d)

    res = dna.shrink(p, d)
    r.store.save(p)
    assert res["dna"] == 2 and res["jpeg"] == 1, res
    assert dna.folder_bytes(d) < before / 4, (before, dna.folder_bytes(d))
    assert not (d / "image01_11.png").exists() and not (d / "image02_22_retake.png").exists()
    assert p["stills"][2]["image"].endswith(".jpg") and Path(p["stills"][2]["image"]).exists()
    assert "history" not in p["stills"][2] and p["shrunk"]["freed"] > 0

    # any other job on a shrunk project regrows first
    r.submit("i1", "redo_still", i=1)
    assert [r.q.get()[1], r.q.get()[1]] == ["regrow", "redo_still"]

    Eng.calls.clear()
    r._regrow("i1")
    p = r.store.load("i1")
    assert Eng.calls == ["image01_11.png", "image02_22.png"]  # the main look first: picture 2 follows it
    for i in (0, 1):
        assert Path(p["stills"][i]["image"]).read_bytes() == originals[i], f"picture {i + 1} not regrown exactly"
        assert p["stills"][i]["match"] >= 99 and "dna" not in p["stills"][i]
    assert not p.get("shrunk") and p["anchor"].endswith("image01_11.png")


def test_video_keeps_final():
    root = Path(tempfile.mkdtemp())
    r = runner(root)
    d = r.store.dir("v1")
    d.mkdir(parents=True)
    for n in ("final.mp4", "clip01_5.mp4", "shot01_5.png", "frame_main.png", "narr01.wav", "talk01_5.mp4"):
        if n.endswith(".png"):
            Eng().image(n, d / n, 64, 64)
        else:
            (d / n).write_bytes(b"x" * 5000)
    p = {"id": "v1", "kind": "video", "status": "done", "log": [], "final": str(d / "final.mp4"), "image": str(d / "frame_main.png"),
         "clips": [{"seed": 5, "image": str(d / "shot01_5.png"), "video": str(d / "talk01_5.mp4"), "narration_file": str(d / "narr01.wav")}]}
    dna.shrink(p, d)
    left = sorted(f.name for f in d.iterdir())
    assert "final.mp4" in left and "narr01.wav" in left and "clip01_5.mp4" not in left and "talk01_5.mp4" not in left
    assert p["clips"][0]["video"] is None and p["clips"][0]["dna"] and Path(p["clips"][0]["image"]).exists()


if __name__ == "__main__":
    test_images_shrink_and_regrow()
    test_video_keeps_final()
    print("ok dna")
