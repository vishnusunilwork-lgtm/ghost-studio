"""Offline check: the same person in every shot, and no added camera shake on real video.
Cloud video is stood in by a fake that records what it was given. Run:  python3 -m tests.test_consistency"""
from __future__ import annotations

import queue
import tempfile
from pathlib import Path

import yaml

from app import editor, llm, pipeline, planner, qc
from app import engines as E

ROOT = Path(__file__).resolve().parent.parent
LOOK = "Young Indian woman, early 20s, warm brown skin, long dark hair in a loose bun, pastel green cotton kurti, simple gold earrings"


def runner(root):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    r = object.__new__(pipeline.Runner)
    r.cfg, r.store, r.cancel, r.q, r.current = cfg, pipeline.Store(root), set(), queue.Queue(), None
    r.eng = E.Engines(cfg, force_mock=True)
    return r


def test_planner_locks_look():
    d = {"character": {"look": LOOK}, "people": [{"key": "A", "look": "Same as character description"}],
         "clips": [{"speaker": "A", "image_prompt": "She lights a diya, camera pushes in slightly, shake 0.4. Warm light.",
                    "video_prompt": "The woman smiles and says hello."}]}
    d["clips"][0]["image_prompt"] = planner.still_prompt(d["clips"][0]["image_prompt"])
    planner.lock_looks(d)
    planner.lock_looks(d)  # twice: still only once
    c = d["clips"][0]
    assert d["people"][0]["look"] == LOOK
    assert "shake" not in c["image_prompt"] and "camera" not in c["image_prompt"] and "Warm light" in c["image_prompt"]
    assert c["video_prompt"].count("pastel green cotton kurti") == 1 and c["image_prompt"].count(LOOK[:40]) == 1


def test_service_same_person_no_shake():
    llm.available = lambda: False
    qc.available = lambda: False
    r = runner(Path(tempfile.mkdtemp()))
    p = r.store.new(kind="video", idea="Diwali greeting on a balcony", language="Hindi", quality="draft", aspect="9:16",
                    seconds=24, voice_mode="oncamera", ai_music=False, refs=[])
    p["plan"] = {"character": {"look": LOOK}, "people": [], "image_prompt": "balcony at night"}
    p["clips"] = [{"speaker": "A", "line": f"line {i}", "seconds": 4, "seed": 10 + i, "camera_move": "push_in", "shake": 0.5,
                   "image_prompt": "lights a diya", "video_prompt": "The woman smiles."} for i in range(3)]
    d = r.store.dir(p["id"])
    main = d / "frame_main.png"
    from PIL import Image
    Image.new("RGB", (360, 640), "green").save(main)
    p["image"] = str(main)
    r.store.save(p)
    seen = {"img": [], "vid": []}
    r.eng.plan = lambda: {"image": "service", "video": "service", "voice": "mac"}

    def image(prompt, out, w, h, seed=0, ref_images=None, **kw):
        seen["img"].append((prompt, [str(x) for x in ref_images or []]))
        Image.new("RGB", (w, h), "green").save(out)
        return out

    def video(image, prompt, seconds, out, seed=0, width=None, height=None, steps=None, aspect="9:16", keep_look=False):
        seen["vid"].append((str(image), prompt, keep_look))
        return E.Engines.photo_clip(r.eng, image, seconds, out, 360, 640)
    r.eng.image, r.eng.video = image, video
    r._videos(p["id"])
    p = r.store.load(p["id"])
    assert len(seen["img"]) == 3 and all(str(main) in refs for _, refs in seen["img"])
    assert all(pr.startswith(r.LOCK[:30]) and LOOK[:40] in pr for pr, _ in seen["img"])
    assert all(Path(img).name.startswith("shot") and keep and LOOK[:40] in pr for img, pr, keep in seen["vid"])
    assert all(c.get("match", 0) >= 70 for c in p["clips"])

    # editing: real video gets no added zoom or shake, unless you set it on that shot yourself
    got = {}
    pipeline.render = lambda clips, out, **kw: got.setdefault("clips", clips)
    r._review_stop = lambda p: True
    p["engine_plan"] = {"video": "service"}
    p["clips"][2]["by_hand"] = True
    r.store.save(p)
    r._edit(p["id"])
    cl = got["clips"]
    assert [c["move"] for c in cl] == ["none", "none", "push_in"] and [c["shake"] for c in cl] == [0, 0, 0.5], cl
    assert "sin(" not in editor.camera_chain("none", 4, 24, 1, 0)


if __name__ == "__main__":
    test_planner_locks_look()
    test_service_same_person_no_shake()
    print("ok consistency")
