"""End-to-end check of Performance ad mode in test mode (colour-card pictures, photo motion, a test tone for the
voice; no script AI, so the simple writer is used). Checks the stages, the claims rule, the 16 files and the final check.
Run:  python3 -m tests.test_perf"""
from __future__ import annotations

import queue
import tempfile
from pathlib import Path

import yaml

from app import addons, llm, perf, pipeline, qc
from app import engines as E

ROOT = Path(__file__).resolve().parent.parent


def runner(root):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    r = object.__new__(pipeline.Runner)
    r.cfg, r.store, r.cancel, r.q, r.current = cfg, pipeline.Store(root), set(), queue.Queue(), None
    r.eng = E.Engines(cfg, force_mock=True)
    return r


def test_perf_end_to_end():
    llm.available = lambda: False
    qc.available = lambda: False
    tmp = Path(tempfile.mkdtemp())
    addons.GHOST = tmp / ".ghost"
    (addons.GHOST / "brand").mkdir(parents=True)
    from PIL import Image, ImageDraw
    lg = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    ImageDraw.Draw(lg).rounded_rectangle((0, 0, 299, 99), 20, fill=(255, 210, 0, 255))
    lg.save(addons.GHOST / "brand" / "logo.png")
    (addons.GHOST / "brand" / "brand.json").write_text('{"name": "Chaiwala", "color": "#7a1f1f", "accent": "#ffd54a", "logo": "%s"}'
                                                       % (addons.GHOST / "brand" / "logo.png"))
    r = runner(tmp / "projects")
    p = r.store.new(kind="perf", idea="Ad for Chaiwala Masala Tea. Fresh masala chai in 2 minutes. Made with real ginger. Free delivery in Kochi.",
                    language="English", quality="draft", aspect="9:16", offer="₹199 for 50 cups", cta="Order now", product="Chaiwala Masala Tea",
                    disclaimer="T&C apply.", platform="both", ai_music=False, voice_mode="narrator", refs=[])
    r._all(p["id"])
    p = r.store.load(p["id"])
    pf = p["perf"]
    b = pf["brief"]
    assert b["product"]["offer"] == "₹199 for 50 cups" and b["cta"] == "Order now"
    assert len(pf["angles"]) == 4 and all("total" in a for a in pf["angles"])
    assert len(pf["variants"]) == 2 and all(len(v["script"]["shots"]) == 5 for v in pf["variants"])
    approved = {a.lower() for a in perf.approved_copy(b)}
    for v in pf["variants"]:
        for sh in v["script"]["shots"]:
            assert not sh["on_screen"] or sh["on_screen"].lower() in approved or any(sh["on_screen"].lower() in a for a in approved)
    outs = pf["outputs"]
    vids = [o for o in outs if o["type"] == "video"]
    imgs = [o for o in outs if o["type"] == "image"]
    assert len(vids) == 8 and len(imgs) == 16, (len(vids), len(imgs))
    bad = [(Path(o["file"]).name, o["problems"]) for o in outs if o["problems"]]
    assert not bad, bad
    assert p["status"] == "done" and p["final"].endswith("_9x16.mp4") and Path(p["final"]).exists()
    # the project list doesn't show the two inner ads
    assert [x["id"] for x in r.store.list()] == [p["id"]]
    return p


def test_claims():
    src = "Chaiwala. Fresh masala chai in 2 minutes. ₹199 for 50 cups."
    assert perf.claim_ok("Fresh masala chai in 2 minutes", src)
    assert not perf.claim_ok("Fresh chai in 1 minute", src)
    assert not perf.claim_ok("Loved by 10 lakh families", src)
    assert perf.banned_in("Guaranteed results, doctor recommended") == ["guaranteed", "doctor recommended"]
    b = perf.finish_brief({"product": {"name": "X"}, "claims": ["Cures colds in 2 minutes"]}, src, {})
    assert b["claims"][0]["status"] == "blocked"
    ranked = perf.rank([{"kind": "offer_first", "hook": "Miracle offer", "scores": {r: 5 for r in perf.RULES}},
                        {"kind": "lifestyle", "hook": "Your morning", "scores": {r: 3 for r in perf.RULES}}])
    assert ranked[0]["kind"] == "lifestyle" and ranked[1]["scores"]["policy_safe"] == 0


if __name__ == "__main__":
    test_claims()
    p = test_perf_end_to_end()
    print("ok perf", p["perf"]["checked"])
