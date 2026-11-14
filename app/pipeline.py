"""One-click pipeline: (reference) -> plan -> images -> videos -> voice -> edit -> final MP4.
Every project lives in its own folder, so nothing is ever mixed between projects."""
from __future__ import annotations

import json
import queue
import random
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import engines as E
from . import planner
from .editor import render
from .reference import key_frames

ASPECTS = {  # generation size (multiples of 32) and export size
    "9:16": ((576, 1024), (1080, 1920)),
    "4:5": ((832, 1024), (1080, 1350)),
    "1:1": ((768, 768), (1080, 1080)),
    "16:9": ((1024, 576), (1920, 1080)),
}


def sizes(p: dict) -> tuple[tuple[int, int], tuple[int, int], bool]:
    gen, out = ASPECTS.get(p.get("aspect") or "9:16", ASPECTS["9:16"])
    draft = p.get("quality") == "draft"
    if draft:
        gen = tuple(max(256, int(v * 0.625) // 32 * 32) for v in gen)
    return gen, out, draft


STEPS = ["analyse", "plan", "images", "videos", "voice", "music", "edit"]
LABEL = {"analyse": "Reading the reference", "plan": "Writing script and prompts", "images": "Making the first frames",
         "videos": "Making the video clips", "voice": "Making the narration", "music": "Making the music", "edit": "Editing and exporting"}


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def dir(self, pid: str) -> Path:
        return self.root / pid

    def load(self, pid: str) -> dict:
        return json.loads((self.dir(pid) / "project.json").read_text(encoding="utf-8"))

    def save(self, p: dict):
        with self.lock:
            d = self.dir(p["id"])
            d.mkdir(parents=True, exist_ok=True)
            p["updated"] = time.time()
            tmp = d / "project.json.tmp"
            tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(d / "project.json")

    def list(self) -> list[dict]:
        out = []
        for f in self.root.glob("*/project.json"):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
                out.append({k: p.get(k) for k in ("id", "title", "status", "updated", "final", "mode", "step", "quality", "aspect", "progress", "kind")})
            except Exception:
                pass
        return sorted(out, key=lambda x: x.get("updated") or 0, reverse=True)

    def new(self, **kw) -> dict:
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        p = {"id": pid, "created": time.time(), "title": kw.get("title") or "New ad", "status": "queued", "step": "",
             "progress": 0, "log": [], "steps": {s: "waiting" for s in STEPS}, "clips": [], **kw}
        self.save(p)
        return p


class Runner:
    """One job at a time: the GPU can only hold one big model."""

    def __init__(self, cfg: dict, store: Store):
        self.cfg, self.store = cfg, store
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.eng = E.get_engines(cfg)
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, pid: str, task: str = "all", **kw):
        p = self.store.load(pid)
        p["status"] = "queued"
        self.store.save(p)
        self.q.put((pid, task, kw))

    def _loop(self):
        while True:
            pid, task, kw = self.q.get()
            self.busy = True
            try:
                getattr(self, "_" + task)(pid, **kw)
            except Exception as e:
                p = self.store.load(pid)
                p["status"] = "error"
                p["error"] = str(e)
                p["log"].append("ERROR: " + str(e))
                p["log"].append(traceback.format_exc()[-1200:])
                self.store.save(p)
            finally:
                self.busy = not self.q.empty()

    # ------------------------------------------------------------ helpers
    def _say(self, p, step, msg=None, progress=None):
        p["step"] = step
        p["status"] = "running"
        if step in p["steps"] and p["steps"][step] == "waiting":
            p["steps"][step] = "running"
        if msg:
            p["log"].append(time.strftime("%H:%M:%S ") + msg)
        if progress is not None:
            p["progress"] = progress
        self.store.save(p)

    def _done(self, p, step):
        p["steps"][step] = "done"
        self.store.save(p)

    # ------------------------------------------------------------ the one click
    def _all(self, pid):
        p = self.store.load(pid)
        d = self.store.dir(pid)
        q = self.cfg["quality"]
        # 1 reference
        ref = None
        if p.get("reference"):
            self._say(p, "analyse", "Reading cuts and key frames", 3)
            frames = key_frames(Path(p["reference"]), d / "ref")
            text = p.get("dialogue_ref") or self.eng.transcribe(Path(p["reference"]))
            p["ref_frames"], p["ref_text"] = frames, text
            if not text:
                p["log"].append(time.strftime("%H:%M:%S ") + "Could not hear the reference's speech (install Script AI to copy its words). Writing a fresh script.")
            try:
                from . import llm
                ref = planner.analyse_reference(self.cfg, frames, text) if llm.available() else {"summary": "reference ad", "scenes": frames}
            except Exception as e:
                ref = {"summary": "reference ad (vision model unavailable: %s)" % e}
            if isinstance(ref, dict):
                ref["transcript"] = text
            p["ref_analysis"] = ref
            self._done(p, "analyse")
        else:
            p["steps"]["analyse"] = "skipped"
        if p.get("kind") == "images":
            self._images_only(pid, ref)
            return
        if p.get("kind") == "music":
            self._music_only(pid)
            return
        # 2 plan
        self._say(p, "plan", "Writing the script and every prompt", 10)
        pl = planner.plan(self.cfg, idea=p.get("idea", ""), dialogue=p.get("dialogue", ""), reference=ref,
                          seconds=int(p.get("seconds", 30)), voice_mode=p.get("voice_mode", "oncamera"),
                          language=p.get("language"), aspect=p.get("aspect") or "9:16", style=self._style(p, ref))
        p["plan"] = {k: v for k, v in pl.items() if k != "clips"}
        p["title"] = p.get("title") if p.get("title") not in (None, "", "New ad") else pl.get("title") or "New ad"
        p["clips"] = [{**c, "status": "waiting", "seed": random.randint(1, 10**6)} for c in pl.get("clips", [])]
        if pl.get("note"):
            p["log"].append(pl["note"])
        self._done(p, "plan")
        # 3 images (one per person so the same face appears in all their clips)
        self._images(pid)
        # 4 videos
        self._videos(pid)
        # 5 narration
        self._voice(pid)
        # 6 AI background music (if chosen and the music add-on is installed)
        self._music_for_ad(pid)
        # 6 edit
        self._edit(pid)

    def _style(self, p, ref=None) -> str:
        st = p.get("style") or "auto"
        if st == "auto" and ref:
            st = planner.style_from_reference(ref) or "auto"
            if st != "auto":
                p["log"].append(time.strftime("%H:%M:%S ") + f"Direction copied from the reference: {planner.STYLES_DIR[st]['name']}")
        p["style_used"] = st
        return st

    def _images(self, pid):
        from . import addons
        addons.MusicServer.stop()  # pictures need the memory
        p = self.store.load(pid)
        d = self.store.dir(pid)
        q = self.cfg["quality"]
        (gw, gh), _, draft = sizes(p)
        k = 1 if draft else 2
        steps = int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"])
        self._say(p, "images", "Making the first-frame image" + (" (draft)" if draft else ""), 20)
        main = d / "frame_main.png"
        refs = [Path(r) for r in p.get("refs") or []]
        self.eng.image(p["plan"]["image_prompt"], main, gw * k, gh * k, seed=int(p.get("image_seed", 7)),
                       ref_images=refs or None, steps=steps, fallback=refs[0] if refs else None, best=p.get("quality") == "best")
        p["image"] = str(main)
        p["engine_plan"] = self.eng.plan()
        style = p.get("style_used") or "auto"
        # One picture per person ("camera A", "camera B"), all in the same place as the main frame
        people = {pp.get("key"): pp for pp in (p["plan"].get("people") or []) if pp.get("key")}
        p["person_images"] = {}
        if len(people) >= 2 or style == "interview2":
            for n_, (key, pp) in enumerate(people.items()):
                f = d / f"frame_{key}.png"
                self._say(p, "images", f"Camera {key}: {pp.get('look', '')[:50]}", 22 + n_)
                prompt = pp.get("single_prompt") or f"{p['plan']['image_prompt']} Only this person in frame, medium close-up: {pp.get('look', '')}"
                self.eng.image(prompt, f, gw * k, gh * k, seed=11 + n_, ref_images=[main, *refs][:3], steps=steps)
                p["person_images"][key] = str(f)
                self.store.save(p)
        if p["engine_plan"]["video"] == "photo_motion":
            n = len(p["clips"]) or 1
            if style == "interview2" and p["person_images"]:
                # A real 2-camera shoot: each camera keeps its framing, so every line reuses that camera's picture (fast)
                for i, c in enumerate(p["clips"]):
                    c["image"] = p["person_images"].get(c.get("speaker")) or str(main)
                self.store.save(p)
            else:
                # every shot gets its own picture, following its speaker's look, so the ad keeps moving
                for i, c in enumerate(p["clips"]):
                    self._say(p, "images", f"Picture {i + 1} of {n}", 20 + int(8 * i / n))
                    anchor = Path(p["person_images"].get(c.get("speaker")) or main)
                    self._shot_image(p, i, gw * k, gh * k, steps, refs, anchor)
                    self.store.save(p)
        self._done(p, "images")

    def _shot_image(self, p, i, w, h, steps, refs, main):
        c = p["clips"][i]
        f = self.store.dir(p["id"]) / f"shot{i + 1:02d}_{c['seed']}.png"
        prompt = c.get("image_prompt") or f"{p['plan']['image_prompt']} Moment: {c.get('line', '')}"
        self.eng.image(prompt, f, w, h, seed=int(c["seed"]), ref_images=[main, *refs][:4], steps=steps,
                       fallback=refs[i % len(refs)] if refs else None)
        c["image"] = str(f)

    def _videos(self, pid, only: int | None = None):
        p = self.store.load(pid)
        d = self.store.dir(pid)
        idx = [only] if only is not None else range(len(p["clips"]))
        (gw, gh), _, draft = sizes(p)
        steps = int(self.cfg["quality"].get("video_steps_draft", 12)) if draft else None
        n = len(p["clips"]) or 1
        for i in idx:
            c = p["clips"][i]
            self._say(p, "videos", f"Clip {i + 1} of {n}: {c.get('line','')[:50]}", 25 + int(55 * i / n))
            start = c.get("image") or p.get("person_images", {}).get(c.get("speaker")) or p["image"]
            out = d / f"clip{i + 1:02d}_{c['seed']}{'_draft' if draft else ''}.mp4"
            self.eng.video(Path(start), c["video_prompt"], float(c["seconds"]), out, seed=c["seed"],
                           width=gw, height=gh, steps=steps, aspect=p.get("aspect") or "9:16")
            c["video"], c["status"] = str(out), "done"
            self.store.save(p)
        if only is None:
            self._done(p, "videos")

    def _voice(self, pid):
        p = self.store.load(pid)
        d = self.store.dir(pid)
        mode = p.get("voice_mode", "oncamera")
        if not self.eng.talks and mode != "narrator":
            p["log"].append(time.strftime("%H:%M:%S ") + "This computer makes photo-motion ads, so a narrator speaks the lines.")
            mode = "narrator"
        if mode == "oncamera":
            p["steps"]["voice"] = "skipped"
            self.store.save(p)
            return
        self._say(p, "voice", "Recording the narrator", 85)
        for i, c in enumerate(p["clips"]):
            text = c.get("narration") or (c.get("line") if mode == "narrator" else "")
            if not text:
                continue
            f = d / f"narr{i + 1:02d}.wav"
            self.eng.speech(text, f, p.get("language") or self.cfg.get("language", "Hindi"))
            c["narration_file"] = str(f)
        self._done(p, "voice")

    def _edit(self, pid):
        p = self.store.load(pid)
        d = self.store.dir(pid)
        q = self.cfg["quality"]
        self._say(p, "edit", "Editing with camera moves and exporting", 92)
        clips, narr, t = [], [], 0.0
        mode = p.get("voice_mode", "oncamera")
        photo = (p.get("engine_plan") or {}).get("video") == "photo_motion"
        if photo:
            mode = "narrator"
        for c in p["clips"]:
            if not c.get("video"):
                continue
            mv = c.get("camera_move", "none")
            if photo and mv == "none":
                mv = "push_in"  # a still photo always needs a little life
            clips.append({"video": c["video"], "duration": c["seconds"], "move": mv,
                          "amount": c.get("amount", 1), "shake": c.get("shake", 0), "mute": mode == "narrator"})
            if c.get("narration_file"):
                narr.append({"file": c["narration_file"], "at": t + 0.2})
            t += float(c["seconds"])
        if not clips:
            raise RuntimeError("No clips to edit")
        _, (ow, oh), draft = sizes(p)
        safe = "".join(ch for ch in p["title"] if ch.isalnum() or ch in " -_").strip() or "ad"
        out = d / f"{safe} - {ow}x{oh}{' - draft' if draft else ''}.mp4"
        style = p.get("style_used") or p.get("style") or "auto"
        grade = (planner.STYLES_DIR.get(style) or {}).get("grade", "natural") if style in planner.STYLES_DIR else "natural"
        render(clips, out, fps=int(q["fps"]), crf=int(q["export_crf"]) + (4 if draft else 0), narration=narr,
               music=p.get("music"), log=lambda m: None, size=(ow, oh), grade=grade,
               letterbox=(style == "cinematic" and p.get("aspect") == "16:9" and p.get("letterbox", True)),
               preset="veryfast" if draft else "medium")
        p["final"] = str(out)
        p["status"], p["progress"] = "done", 100
        self._done(p, "edit")
        p["log"].append(time.strftime("%H:%M:%S ") + "Finished: " + out.name)
        self.store.save(p)

    # ------------------------------------------------------------ small tasks (from the UI)
    def _redo_clip(self, pid, i: int):
        p = self.store.load(pid)
        p["clips"][i]["seed"] = random.randint(1, 10**6)
        p["clips"][i]["status"] = "waiting"
        if (p.get("engine_plan") or {}).get("video") == "photo_motion" and p.get("image") and p.get("style_used") != "interview2":
            (gw, gh), _, draft = sizes(p)
            k = 1 if draft else 2
            q = self.cfg["quality"]
            self._say(p, "images", f"New picture for shot {i + 1}")
            self._shot_image(p, i, gw * k, gh * k, int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"]),
                             [Path(r) for r in p.get("refs") or []], Path(p["image"]))
        self.store.save(p)
        self._videos(pid, only=i)
        self._edit(pid)

    def _reedit(self, pid):
        self._edit(pid)

    # ------------------------------------------------------------ music
    def _music_for_ad(self, pid):
        from . import addons
        p = self.store.load(pid)
        if not p.get("ai_music") or p.get("music") or not addons.music_installed():
            p["steps"]["music"] = "skipped"
            self.store.save(p)
            return
        E.free()
        total = sum(float(c.get("seconds", 0)) for c in p["clips"]) + 1
        style = (p.get("plan") or {}).get("music_style") or "warm festive Indian background music"
        self._say(p, "music", "Composing background music to fit the ad", 88)
        try:
            tr = addons.MusicServer.generate(caption=style + ". Instrumental background, leaves room for voice.", lyrics="[Instrumental]",
                                             seconds=max(10, total), language="unknown", out_dir=self.store.dir(pid), takes=1,
                                             log=lambda m: self._say(self.store.load(pid), "music", m))
            p = self.store.load(pid)
            p["music"] = tr[0]["file"]
            p["music_ai"] = True
            self._done(p, "music")
        except Exception as e:
            p = self.store.load(pid)
            p["log"].append(time.strftime("%H:%M:%S ") + "Music skipped: " + str(e)[:200])
            p["steps"]["music"] = "skipped"
            self.store.save(p)
        finally:
            addons.MusicServer.stop_later()

    def _music_only(self, pid):
        from . import addons
        p = self.store.load(pid)
        for st in ("analyse", "images", "videos", "voice", "edit"):
            p["steps"][st] = "skipped"
        self._say(p, "plan", "Writing the song" if p.get("vocals") != "instrumental" else "Planning the music", 8)
        pl = planner.plan_music(self.cfg, idea=p.get("idea", ""), style=p.get("music_style", "bollywood"), vocals=p.get("vocals", "ai"),
                                lyrics=p.get("dialogue", ""), seconds=int(p.get("music_seconds", 60)), language=p.get("language"))
        if p.get("title") in (None, "", "New ad"):
            p["title"] = pl.get("title") or "Song"
        p["song"] = pl
        if pl.get("note"):
            p["log"].append(pl["note"])
        p["tracks"] = p.get("tracks") or []
        self._done(p, "plan")
        self._compose(pid, takes=2)

    def _compose(self, pid, takes=1):
        from . import addons
        p = self.store.load(pid)
        if not addons.music_installed():
            raise RuntimeError("Install the Music & songs add-on first (⚙ → Add-ons).")
        pl = p["song"]
        E.free()
        self._say(p, "music", "Starting the music engine", 20)
        try:
            out = addons.MusicServer.generate(caption=pl["caption"], lyrics=pl.get("lyrics", ""), seconds=pl.get("seconds", 60),
                                              language=pl.get("language", "unknown"), out_dir=self.store.dir(pid), takes=takes,
                                              bpm=pl.get("bpm"), describe=pl.get("describe", ""), fast=p.get("quality") == "draft",
                                              log=lambda m: self._say(self.store.load(pid), "music", m))
        finally:
            addons.MusicServer.stop_later()
        p = self.store.load(pid)
        for t in out:
            p["tracks"].append({**t, "n": len(p["tracks"]) + 1})
        if not pl.get("lyrics") and out and out[0].get("lyrics"):
            p["song"]["lyrics"] = out[0]["lyrics"]
        p["steps"]["music"] = "done"
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Finished {len(out)} take{'s' if len(out) > 1 else ''}")
        self.store.save(p)

    def _more_music(self, pid):
        p = self.store.load(pid)
        p["steps"]["music"] = "waiting"
        self.store.save(p)
        self._compose(pid, takes=1)

    # ------------------------------------------------------------ images only
    def _images_only(self, pid, ref=None):
        p = self.store.load(pid)
        self._say(p, "plan", "Writing the image prompts", 8)
        pl = planner.plan_images(self.cfg, idea=p.get("idea", "") or p.get("dialogue", ""), reference=ref,
                                 count=int(p.get("count", 4)), aspect=p.get("aspect") or "9:16", language=p.get("language"),
                                 style=self._style(p, ref))
        if p.get("title") in (None, "", "New ad"):
            p["title"] = pl.get("title") or "Images"
        p.pop("anchor", None); p.pop("anchor_i", None)  # a new set starts a new main look
        p["stills"] = [{"caption": i.get("caption", ""), "prompt": i["prompt"], "seed": random.randint(1, 10**6), "status": "waiting"}
                       for i in pl["images"]]
        if pl.get("note"):
            p["log"].append(pl["note"])
        self._done(p, "plan")
        self._stills(pid)

    LOCK = ("Keep EXACTLY the same person as in the reference image: same face, skin tone, hair, outfit, colours and jewellery, "
            "and the same place, light and colour mood. Only the framing, pose and action change: ")

    def _stills(self, pid, only: int | None = None, mode: str = "same"):
        from . import addons
        addons.MusicServer.stop()  # pictures need the memory
        p = self.store.load(pid)
        d = self.store.dir(pid)
        q = self.cfg["quality"]
        _, (ow, oh), draft = sizes(p)
        k = 0.5 if draft else 1.0
        w, h = int(ow * k) // 16 * 16, int(oh * k) // 16 * 16
        steps = int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"])
        refs = [Path(r) for r in p.get("refs") or []]
        idx = [only] if only is not None else range(len(p["stills"]))
        n = len(p["stills"]) or 1
        same = p.get("same_look", True) and mode != "new"
        anchor = Path(p["anchor"]) if p.get("anchor") and Path(p["anchor"]).exists() else None
        took: list[float] = []
        for i in idx:
            s = p["stills"][i]
            eta = f" · about {int(sum(took) / len(took))} s each" if took else (" · first one also loads the model" if i == 0 else "")
            self._say(p, "images", f"Image {i + 1} of {n}: {s.get('caption', '')}{eta}", 15 + int(80 * i / n))
            t0 = time.time()
            f = d / f"image{i + 1:02d}_{s['seed']}{'_draft' if draft else ''}.png"
            # Same look: every picture after the main one follows it (same person + place), via the reference model
            use_anchor = same and anchor is not None and self.eng.plan()["image"] in ("local", "nvidia", "service")
            ref_imgs = ([anchor] if use_anchor else []) + refs
            prompt = (self.LOCK + s["prompt"]) if use_anchor else s["prompt"]
            self.eng.image(prompt, f, w, h, seed=int(s["seed"]), ref_images=ref_imgs[:3] or None, steps=steps,
                           fallback=refs[i % len(refs)] if refs else None, best=p.get("quality") == "best")
            f = self._checked(p, i, f, lambda seed, out: self.eng.image(prompt, out, w, h, seed=seed, ref_images=ref_imgs[:3] or None,
                                                                        steps=steps, fallback=None, best=p.get("quality") == "best"), draft)
            s["image"], s["status"] = str(f), "done"
            s["look"] = "same" if use_anchor else "fresh"
            if anchor is None or (mode == "new" and p.get("anchor_i") == i):
                if p.get("same_look", True):  # the first picture becomes the main look
                    anchor = f
                    p["anchor"], p["anchor_i"] = str(f), i
            took.append(time.time() - t0)
            self.store.save(p)
        for st in ("videos", "voice", "edit"):
            p["steps"][st] = "skipped"
        p["steps"]["images"] = "done"
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Finished {n} image{'s' if n > 1 else ''}")
        self.store.save(p)

    def _checked(self, p, i, f: Path, remake, draft: bool) -> Path:
        """Final pictures: if the vision check finds bad hands/eyes/face, make one more take and keep the better one."""
        from . import qc
        if draft or not qc.available() or self.eng.plan()["image"] == "photos":
            return f
        try:
            r1 = qc.check(f)
            if r1["ok"]:
                return f
            self._say(p, "images", f"Image {i + 1}: {r1.get('problem') or 'small defects'} — making a better take")
            f2 = f.with_name(f.stem + "_retake.png")
            remake(random.randint(1, 10**6), f2)
            r2 = qc.check(f2)
            return f2 if r2["score"] >= r1["score"] else f
        except Exception as e:
            p["log"].append(time.strftime("%H:%M:%S ") + f"Quality check skipped: {str(e)[:120]}")
            return f

    def _redo_still(self, pid, i: int, mode: str = "same"):
        """mode same = keep the main look (person + place); mode new = a completely fresh take."""
        p = self.store.load(pid)
        p["stills"][i]["seed"] = random.randint(1, 10**6)
        p["stills"][i]["status"] = "waiting"
        if mode == "same" and p.get("anchor_i") == i and p["stills"][i].get("image"):
            p["anchor"] = p["stills"][i]["image"]  # retaking the main look itself: keep its own person + place
        self.store.save(p)
        self._stills(pid, only=i, mode=mode)

    def _upgrade(self, pid):
        """Draft -> final: the same script and the same takes (seeds), re-made at full quality."""
        p = self.store.load(pid)
        p["quality"] = "final"
        if p.get("kind") == "images":
            p["steps"]["images"] = "waiting"
            self.store.save(p)
            self._stills(pid)
            return
        for s in ("images", "videos", "voice", "edit"):
            p["steps"][s] = "waiting"
        p["log"].append(time.strftime("%H:%M:%S ") + "Making the final version at full quality")
        self.store.save(p)
        self._images(pid)
        self._videos(pid)
        self._voice(pid)
        self._edit(pid)
