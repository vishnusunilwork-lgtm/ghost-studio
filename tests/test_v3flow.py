"""Offline check of the Ghost V3 flow: Gemini script brain on the Google sign-in, best of two main pictures, character
sheet from the picture, mood / place in the brief, and the ✨ Agent ideas. Run:  python3 -m tests.test_v3flow"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app import llm, pipeline
from app import engines as E
from tests.test_handoff import runner

LOOK = "Young Indian woman, early 20s, long dark hair in a loose bun, pastel green cotton kurti, simple gold earrings"
SEEN = "Indian woman about 25, oval face, warm brown skin, long black hair in a low bun, mint green cotton kurti with white embroidery, small gold jhumkas"


class FakeGoogle:
    on, configured, project, broken = True, True, "demo-project", ""

    def __init__(self):
        self.asked = []

    def imagen(self, prompt, out, w, h, seed=0):
        from PIL import Image
        Image.new("RGB", (w, h), "blue").save(out)
        return out

    def ask(self, text, images=None, json_out=False):
        self.asked.append((text[:40], len(images or [])))
        return '{"best": 2, "why": "more natural face"}' if json_out else SEEN

    def _headers(self):
        return {"Authorization": "Bearer t"}


def test_brief_and_pictures():
    assert pipeline.brief({"idea": "Diwali ad", "mood": "angry", "place": "outdoor"}) == \
        "Diwali ad Mood: angry, intense, frustrated. Place: outdoors (street, garden, terrace or nature)."
    assert pipeline.brief({"idea": "x", "mood": "nope"}) == "x"
    r = runner(Path(tempfile.mkdtemp()))
    r.eng.mock = False
    r.eng.goog = FakeGoogle()
    r.eng.plan = lambda: {"image": "service", "video": "service", "voice": "mac"}
    from PIL import Image

    def image(prompt, out, w, h, seed=0, ref_images=None, **kw):
        Image.new("RGB", (w, h), "green").save(out)
        return out
    r.eng.image = image
    p = r.store.new(kind="video", idea="Diwali", quality="draft", aspect="9:16", seconds=15, refs=[], voice_mode="oncamera")
    p["plan"] = {"character": {"look": LOOK}, "people": [{"key": "A", "look": LOOK}], "image_prompt": "a woman on a balcony"}
    p["clips"] = [{"speaker": "A", "line": "Happy Diwali", "seconds": 4, "seed": 1,
                   "image_prompt": f"Same character in every shot: {LOOK}. lights a diya", "video_prompt": f"Same character in every shot: {LOOK}. smiles"}]
    r.store.save(p)
    r._images(p["id"])
    p = r.store.load(p["id"])
    main = Image.open(p["image"]).convert("RGB").getpixel((5, 5))
    assert main == (0, 0, 255), main  # Gemini judged picture 2 (Imagen) better: it became the main picture
    assert not (Path(p["image"]).parent / "frame_main_imagen.png").exists()
    assert p["plan"]["character"]["look"] == SEEN and p["plan"]["people"][0]["look"] == SEEN
    c = p["clips"][0]
    assert SEEN in c["video_prompt"] and LOOK not in c["video_prompt"] and c["video_prompt"].count("Same character") == 1
    assert any("Imagen" in line for line in p["log"]) and any("Character sheet" in line for line in p["log"])


def test_google_brain_and_ideas():
    from app import server as S
    llm.CHAIN[:] = []
    S.RUNNER.eng.goog = FakeGoogle()
    S.CFG.setdefault("api", {}).pop("google_brain_off", None)
    saved = S._save_chain
    S._save_chain = lambda: None
    try:
        S._google_brain_sync()
        S._google_brain_sync()  # only once
        g = [e for e in llm.CHAIN if e.get("auth") == "google"]
        assert len(g) == 1 and llm.CHAIN[0] is g[0] and "demo-project" in g[0]["base"] and g[0]["model"].startswith("google/gemini")
        assert llm.ready() == g  # signed in = ready, no key needed
        S.RUNNER.eng.goog.on = False
        assert llm.ready() == []  # signed out: skipped
    finally:
        S._save_chain = saved
        llm.CHAIN[:] = []
    llm.available = lambda: False
    d = S.agent_suggest(S.Suggest(text="15 sec Malayalam Diwali video with music", language="Malayalam"))
    assert len(d["ideas"]) == 3 and all(i["idea"] and i["kind"] in ("video", "images", "music") for i in d["ideas"])
    assert d["ideas"][0]["title"].startswith("Diwali") and d["ideas"][0]["language"] == "Malayalam"


if __name__ == "__main__":
    test_brief_and_pictures()
    test_google_brain_and_ideas()
    print("ok v3 flow")
