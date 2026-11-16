"""Offline check of the exact remake: a reference ad is watched cut by cut, people get new faces, app screens and the end
card are reused, timing and words stay, what you typed are notes (not dialogue). Run:  python3 -m tests.test_remake"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from app import links, llm, pipeline, planner, qc
from app import engines as E
from app.editor import ffmpeg_bin, probe
from tests.test_handoff import runner

SCENES = [
    {"start": 0, "end": 5, "kind": "talking", "has_face": True, "who": "A", "speaker": "A", "place": "court steps, morning",
     "framing": "medium shot", "action": "talks to an interviewer's mic", "line": "पहले मुझे यकीन नहीं था"},
    {"start": 5, "end": 8, "kind": "screen", "has_face": False, "who": None, "speaker": "A", "line": "ये देखो मैजिक शेयर ऐप"},
    {"start": 8, "end": 10, "kind": "endcard", "has_face": False, "who": None, "speaker": "none", "line": ""}]


def ref_video(path: Path):
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=540x960:r=30:d=10", "-f", "lavfi",
                    "-i", "sine=f=300:d=10", "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)


def test_exact_remake():
    llm.available = lambda: False
    qc.available = lambda: False
    tmp = Path(tempfile.mkdtemp())
    r = runner(tmp / "projects")
    asked = {}

    class G:
        on, project, location, TEXT, broken = True, "p", "us-central1", ["gemini-2.5-flash"], ""

        def _call(self, kind, models, locs, method, body, timeout=0):
            parts = body["contents"][0]["parts"]
            asked["mime"] = parts[0]["inlineData"]["mimeType"]
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "lawyer testimonial", "people": [
                {"key": "A", "role": "lawyer", "look": "man in his 40s, black lawyer's robe, white bands", "voice": "male, 40s, confident"}],
                "music": "soft devotional", "scenes": SCENES})}]}}]}, ""
    r.eng.goog = G()
    r.eng.mock = False
    r.eng.plan = lambda: {"image": "service", "video": "service", "voice": "tone"}

    def fake_best(model, prompt, what="", temperature=0.6):
        asked["plan_prompt"] = prompt
        return {"title": "नई शुरुआत", "people": [{"key": "A", "look": "Indian man in his early 40s, round face, wheatish skin, short grey-black hair, "
                                                                 "black lawyer's robe over a white shirt with white bands", "voice": "male"}],
                "changed": "removed the money claim", "scenes": [{"i": 0, "shots": [
                    {"seconds": 3, "speaker": "A", "line": "पहले मुझे यकीन नहीं था", "image_prompt": "lawyer on court steps, medium shot",
                     "video_prompt": "He says in Hindi: \"पहले मुझे यकीन नहीं था\""},
                    {"seconds": 3, "speaker": "narrator", "line": "फिर मैंने आज़माया", "image_prompt": "close-up of his hands", "video_prompt": "ambient"}]}],
                "lines_over_reused": [{"i": 1, "line": "ये देखो मैजिक शेयर ऐप"}]}, "Compared remake plans: Gemini's was better"
    llm.ask_best = fake_best
    from PIL import Image

    def image(prompt, out, w, h, seed=0, ref_images=None, **kw):
        Image.new("RGB", (w, h), "gray").save(out)
        return out

    def video(image_, prompt, seconds, out, seed=0, width=None, height=None, steps=None, aspect="9:16", keep_look=False):
        asked.setdefault("veo", []).append(prompt)
        return E.Engines.photo_clip(r.eng, image_, 8, out, 360, 640)
    r.eng.image, r.eng.video = image, video
    r.eng.transcribe = lambda m: "पहले मुझे यकीन नहीं था ये देखो मैजिक शेयर ऐप"
    p = r.store.new(kind="video", idea="", dialogue="", notes="i need exactly this, more natural and realistic", remake=True,
                    language="Hindi", quality="draft", aspect="9:16", seconds=15, voice_mode="oncamera", refs=[], ai_music=False, mode="reference")
    ref = r.store.dir(p["id"]) / "input_reference.mp4"
    ref_video(ref)
    p["reference"] = str(ref)
    r.store.save(p)
    r._all(p["id"])
    p = r.store.load(p["id"])
    assert asked["mime"] == "video/mp4" and "more natural and realistic" in asked["plan_prompt"]  # notes reach the plan, not the dialogue
    assert [c.get("reused", False) for c in p["clips"]] == [False, False, True, True]
    assert [round(c["seconds"], 1) for c in p["clips"]] == [2.5, 2.5, 3.0, 2.0] and abs(p["seconds"] - 10) < 0.2  # the original's timing
    assert p["clips"][1]["narration"] == "फिर मैंने आज़माया" and p["clips"][2]["narration"] == "ये देखो मैजिक शेयर ऐप"
    assert p["voice_gender"] == "male" and p["voice_mode"] == "both" and p["clip_talks"]
    shots = [v for v in asked["veo"] if "Close-up, he or she talks" not in v]
    voices = [v for v in asked["veo"] if "Close-up, he or she talks" in v]
    assert len(shots) == 2 and all("lawyer's robe" in v for v in asked["veo"])  # only the shots with a face are re-made
    # the lawyer's off-screen words are spoken in HIS voice (same picture, same seed), not by a separate narrator
    assert len(voices) == 2 and "फिर मैंने आज़माया" in voices[0] and "मैजिक शेयर" in voices[1]
    assert all(p["clips"][k].get("voice_shot") for k in (1, 2))
    assert p["clips"][0]["seed"] == p["voice_seeds"]["A"]
    assert any("removed the money claim" in line for line in p["log"]) and any("2 new shots" in line for line in p["log"])
    assert p["status"] == "done" and abs(probe(p["final"])["duration"] - 10) < 0.6


if __name__ == "__main__":
    test_exact_remake()
    print("ok remake")
