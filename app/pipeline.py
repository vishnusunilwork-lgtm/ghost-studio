"""One-click pipeline: (reference) -> plan -> images -> videos -> voice -> edit -> final MP4.
Every project lives in its own folder, so nothing is ever mixed between projects."""
from __future__ import annotations

import json
import queue
import random
import shutil
import re
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import engines as E
from . import planner
from .planner import still_prompt
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


HANDOFF_APPS = {
    "flow": {"name": "Google Flow", "url": "https://labs.google/fx/tools/flow", "makes": "video",
             "how": "Paste the prompt, choose Veo and 9:16 (or the size you need), press Generate, then Download."},
    "gemini": {"name": "Gemini app", "url": "https://gemini.google.com/app", "makes": "image",
               "how": "Paste the prompt and send. When the picture (or video) appears, press Download."},
    "chatgpt": {"name": "ChatGPT", "url": "https://chatgpt.com/", "makes": "image", "prefill": "https://chatgpt.com/?q=",
                "how": "The prompt is filled in for you: send it. When the picture appears, press Download. Keep all shots in the same chat so the person stays the same."},
}
MEDIA_IMG = {".png", ".jpg", ".jpeg", ".webp"}
MEDIA_VID = {".mp4", ".mov", ".webm", ".m4v"}


def downloads_dir() -> Path:
    return Path.home() / "Downloads"


MOODS = {"warm": "warm and heartfelt", "excited": "excited, high energy", "emotional": "emotional, touching",
         "funny": "funny, light and playful", "calm": "calm and soothing", "angry": "angry, intense, frustrated",
         "serious": "serious and trustworthy", "devotional": "devotional"}
PLACES = {"indoor": "indoors (a real home, shop or office)", "outdoor": "outdoors (street, garden, terrace or nature)"}


def brief(p: dict) -> str:
    """The idea plus the few settings that shape the story: mood and indoor / outdoor."""
    idea = p.get("idea", "") or ""
    extra = []
    if p.get("mood") in MOODS:
        extra.append("Mood: " + MOODS[p["mood"]] + ".")
    if p.get("place") in PLACES:
        extra.append("Place: " + PLACES[p["place"]] + ".")
    return (idea + (" " if idea and extra else "") + " ".join(extra)).strip()


def knowledge_apply(t: str) -> str:
    from . import knowledge
    return knowledge.apply(t)


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.gone: set[str] = set()  # deleted projects: a job still finishing may never bring their folder back

    def dir(self, pid: str) -> Path:
        return self.root / pid

    def load(self, pid: str) -> dict:
        return json.loads((self.dir(pid) / "project.json").read_text(encoding="utf-8"))

    def save(self, p: dict):
        with self.lock:
            if p.get("id") in self.gone:
                return
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
        self.current: str | None = None
        self.cancel: set[str] = set()
        self.eng = E.get_engines(cfg)
        self.recover()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, pid: str, task: str = "all", **kw):
        self.cancel.discard(pid)
        p = self.store.load(pid)
        p["status"] = "queued"
        p.pop("error", None)
        if task == "all":
            p.pop("shrunk", None)  # a fresh start makes every file anew
        self.store.save(p)
        if p.get("shrunk") and task != "regrow":
            self.q.put((pid, "regrow", {}))  # 🧬 a shrunk project first grows its files back, then does the job
        self.q.put((pid, task, kw))

    def stop(self, pid: str):
        """Pause: the running job stops at the next safe point; a waiting job leaves the queue."""
        self.cancel.add(pid)
        if self.current == pid:
            E.STOP["flag"] = True
            from . import addons
            addons.MusicServer.stop()
        else:
            try:
                p = self.store.load(pid)
                if p.get("status") == "queued":
                    p["status"] = "stopped"
                    p["log"].append(time.strftime("%H:%M:%S ") + "Removed from the queue")
                    self.store.save(p)
            except FileNotFoundError:
                pass

    def forget(self, pid: str):
        """Delete: stop everything for this project (running or waiting) and never write to it again."""
        self.store.gone.add(pid)
        self.stop(pid)

    @property
    def busy(self) -> bool:
        return self.current is not None

    @busy.setter
    def busy(self, _v):  # kept for old callers; "busy" now always means "a job is running right now"
        pass

    def _note(self, pid, status, line, error=None):
        try:
            p = self.store.load(pid)
            p["status"] = status
            if error is not None:
                p["error"] = error
            p["log"].append(line)
            self.store.save(p)
        except Exception:
            pass  # the project was deleted while it was being made: nothing to update

    def _loop(self):
        while True:  # this loop must never die, or Ghost would look busy for ever
            pid = None
            try:
                pid, task, kw = self.q.get()
                if pid in self.cancel or pid in self.store.gone or not (self.store.dir(pid) / "project.json").exists():
                    continue  # paused or deleted while waiting
                self.current = pid
                E.STOP["flag"] = False
                try:
                    getattr(self, "_" + task)(pid, **kw)
                except E.Cancelled:
                    self._note(pid, "stopped", time.strftime("%H:%M:%S ") + "Paused. Press Continue to carry on from here.")
                except FileNotFoundError:
                    if (self.store.dir(pid) / "project.json").exists():
                        self._note(pid, "error", "ERROR: a file is missing\n" + traceback.format_exc()[-1200:], "A file is missing. Press Continue or Start again.")
                except Exception as e:
                    self._note(pid, "error", "ERROR: " + str(e) + "\n" + traceback.format_exc()[-1200:], str(e))
            except BaseException as e:  # anything else: log it and keep the worker alive
                print("worker error:", e)
            finally:
                if pid and pid in self.store.gone:  # deleted while it was being made: remove anything written meanwhile
                    shutil.rmtree(self.store.dir(pid), ignore_errors=True)
                if pid:
                    self.cancel.discard(pid)
                self.current = None
                E.STOP["flag"] = False

    def recover(self):
        """After Ghost restarts, jobs that were running or waiting are shown as Paused (press Continue)."""
        for pp in self.store.list():
            if pp.get("status") in ("running", "queued"):
                self._note(pp["id"], "stopped", time.strftime("%H:%M:%S ") + "Ghost restarted. Press Continue to carry on from here.")

    # ------------------------------------------------------------ helpers
    def _say(self, p, step, msg=None, progress=None):
        if p.get("id") in self.cancel or E.STOP["flag"]:
            raise E.Cancelled("Paused")
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

    # ------------------------------------------------------------ 🧬 Ghost DNA: grow a shrunk project's files back
    def _regrow(self, pid):
        from . import dna
        p = self.store.load(pid)
        if not p.get("shrunk"):
            return
        d = self.store.dir(pid)
        stills = p.get("stills") or []
        if stills:
            _, (ow, oh), draft = sizes(p)
            k = 0.5 if draft else 1.0
            w, h = int(ow * k) // 16 * 16, int(oh * k) // 16 * 16
            q = self.cfg["quality"]
            steps = int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"])
            refs = [Path(r) for r in p.get("refs") or []]
            ai = p.get("anchor_i")
            order = sorted(range(len(stills)), key=lambda i: 0 if i == ai else 1)  # the main look first: the others follow it
            todo = [i for i in order if stills[i].get("dna")]
            scores = []
            for n, i in enumerate(todo):
                s = stills[i]
                self._say(p, "images", f"🧬 Regrowing picture {i + 1} from its recipe ({n + 1} of {len(todo)})", 10 + int(85 * n / max(1, len(todo))))
                info = s["dna"]
                anchor = Path(stills[info["anchor"]]["image"]) if info.get("anchor") is not None else None
                ref_imgs = ([anchor] if anchor else []) + refs
                prompt = (self.LOCK + s["prompt"]) if anchor else s["prompt"]
                out = d / info["name"]
                self.eng.image(prompt, out, w, h, seed=int(s["seed"]), ref_images=ref_imgs[:3] or None, steps=steps,
                               fallback=refs[i % len(refs)] if refs else None, best=not draft)
                prev = Path(info["preview"])
                s["match"] = dna.similarity(prev, out) if prev.exists() else None
                scores.append(s["match"] or 0)
                s["image"] = str(out)
                s.pop("dna")
                if i == ai:
                    p["anchor"] = str(out)
                self.store.save(p)
            if scores:
                p["log"].append(time.strftime("%H:%M:%S ") + f"🧬 Regrew {len(scores)} picture(s); closest match {max(scores)}%, "
                                f"lowest {min(scores)}% (100% = the same picture; below about 90% = a close variation, "
                                "usually because a model was updated since)")
        clips = p.get("clips") or []
        if clips and any(c.get("dna") for c in clips):
            self._say(p, "images", "🧬 Regrowing the shots from their recipes", 10)
            self._images(pid)
            self._videos(pid, resume=True)
            self._talk(pid)
            p = self.store.load(pid)
            for c in p.get("clips") or []:
                c.pop("dna", None)
        p.pop("shrunk", None)
        p["status"], p["progress"] = "done", 100
        self.store.save(p)

    # ------------------------------------------------------------ the one click
    def _all(self, pid):
        p = self.store.load(pid)
        d = self.store.dir(pid)
        q = self.cfg["quality"]
        if p.get("kind") == "dub":
            return self._dub(pid)
        # 1 reference
        ref = None
        if p.get("reference"):
            self._say(p, "analyse", "Reading cuts and key frames", 3)
            frames = key_frames(Path(p["reference"]), d / "ref")
            text = p.get("dialogue_ref") or self.eng.transcribe(Path(p["reference"]))
            p["ref_frames"], p["ref_text"] = frames, text
            if not text:
                p["log"].append(time.strftime("%H:%M:%S ") + "Could not hear the reference's speech (install Script AI to copy its words). Writing a fresh script.")
            watched = self._watch_reference(p) if p.get("remake") else None
            try:
                from . import llm
                if watched:
                    ref = watched
                else:
                    ref = planner.analyse_reference(self.cfg, frames, text) if llm.available() else {"summary": "reference ad", "scenes": frames}
            except Exception as e:
                ref = {"summary": "reference ad (vision model unavailable: %s)" % e}
            if isinstance(ref, dict):
                ref["transcript"] = text
            p["ref_analysis"] = ref
            self._done(p, "analyse")
        else:
            p["steps"]["analyse"] = "skipped"
        if p.get("kind") == "video" and p.get("remake") and isinstance(ref, dict) and ref.get("watched"):
            return self._remake(pid)
        if p.get("kind") == "images":
            self._images_only(pid, ref)
            return
        if p.get("kind") == "music":
            self._music_only(pid)
            return
        # 2 plan
        self._say(p, "plan", "Writing the script and every prompt", 10)
        pl = planner.plan(self.cfg, idea=brief(p), dialogue=p.get("dialogue", ""), reference=ref,
                          seconds=int(p.get("seconds", 30)), voice_mode=p.get("voice_mode", "oncamera"),
                          language=p.get("language"), aspect=p.get("aspect") or "9:16", style=self._style(p, ref),
                          fast=p.get("quality") == "draft", cast=p.get("cast", "auto"),
                          photo=self.eng.plan()["video"] != "service" and p.get("make_with") != "flow")
        p["plan"] = {k: v for k, v in pl.items() if k != "clips"}
        p["title"] = p.get("title") if p.get("title") not in (None, "", "New ad") else pl.get("title") or "New ad"
        p["clips"] = [{**c, "status": "waiting", "seed": random.randint(1, 10**6)} for c in pl.get("clips", [])]
        if pl.get("note"):
            p["log"].append(pl["note"])
        self._done(p, "plan")
        if self._review_stop(p):
            return
        if p.get("make_with") in HANDOFF_APPS:
            return self._handoff_start(pid)
        # 3 images (one per person so the same face appears in all their clips)
        self._images(pid)
        # 4 videos
        self._videos(pid)
        # 5 narration
        self._voice(pid)
        self._talk(pid)
        # 6 AI background music (if chosen and the music add-on is installed)
        self._music_for_ad(pid)
        # 6 edit
        self._edit(pid)

    # ------------------------------------------------------------ hand-off: your own membership apps make the shots
    def handoff_prompts(self, p: dict) -> list[dict]:
        """One ready-to-paste prompt per shot / picture, written for the chosen app."""
        from . import knowledge
        app = p.get("make_with")
        lang = p.get("language") or self.cfg.get("language", "Hindi")
        aspect = p.get("aspect") or "9:16"
        orient = {"9:16": "vertical 9:16", "16:9": "horizontal 16:9", "1:1": "square 1:1", "4:5": "vertical 4:5"}.get(aspect, aspect)
        out = []
        if p.get("kind") == "images":
            for i, st in enumerate(p.get("stills") or []):
                body = knowledge.apply(st["prompt"])
                same = " Keep exactly the same person/subject and place as the first picture in this chat." if i and p.get("same_look", True) else ""
                out.append({"n": i + 1, "caption": st.get("caption", ""), "prompt": f"Create a photorealistic {orient} image: {body}{same} No text, no logos, no watermark."})
            return out
        for i, c in enumerate(p.get("clips") or []):
            look = knowledge.apply(c.get("image_prompt") or (p.get("plan") or {}).get("image_prompt", ""))
            if app == "flow":
                say = ""
                if c.get("line") and p.get("voice_mode") != "narrator":
                    say = f' The person speaks in {lang}, naturally, with lip-sync: "{c["line"]}".'
                elif c.get("line"):
                    say = " No one speaks; natural background sound only."
                txt = (f"{orient} cinematic, photorealistic video, about {max(4, min(8, round(float(c.get('seconds') or 6))))} seconds. "
                       f"{look} Action and camera: {c.get('video_prompt') or c.get('camera_move') or 'natural movement'}.{say} No subtitles, no text, no logos.")
            else:
                same = " Same person and place as the first picture in this chat." if i else ""
                txt = f"Create a photorealistic {orient} image: {look}{same} The moment: {c.get('line', '')} No text, no logos, no watermark."
            out.append({"n": i + 1, "caption": c.get("line", "")[:80], "prompt": txt})
        return out

    def _handoff_start(self, pid):
        p = self.store.load(pid)
        app = HANDOFF_APPS[p["make_with"]]
        h = p.setdefault("handoff", {})
        h.setdefault("since", time.time() - 5)
        h.setdefault("files", {})
        h["prompts"] = self.handoff_prompts(p)
        h["app"] = p["make_with"]
        p["status"], p["step"] = "handoff", "images"
        p["steps"]["images"] = "running"
        p["log"].append(time.strftime("%H:%M:%S ") + f"Prompts ready for {app['name']}. Make each one there with your plan; "
                        "Ghost picks up the downloads by itself.")
        self.store.save(p)

    def handoff_take(self, pid: str, i: int, src: Path) -> dict:
        """Put one made file into slot i (a shot or a picture)."""
        p = self.store.load(pid)
        d = self.store.dir(pid)
        ext = src.suffix.lower()
        if ext not in MEDIA_IMG | MEDIA_VID:
            raise ValueError("Use a picture (PNG/JPG/WebP) or a video (MP4/MOV)")
        dst = d / f"handoff{i + 1:02d}_{int(time.time()) % 100000}{ext}"
        shutil.copy2(src, dst)
        h = p.setdefault("handoff", {})
        h.setdefault("files", {})[str(i)] = str(dst)
        h.setdefault("used", []).append(src.name)
        self.store.save(p)
        return {"slot": i, "file": str(dst)}

    def handoff_scan(self, pid: str) -> list[dict]:
        """New downloads since the hand-off started go into the empty slots, in the order they were downloaded."""
        p = self.store.load(pid)
        h = p.get("handoff") or {}
        n = len(h.get("prompts") or [])
        empty = [i for i in range(n) if str(i) not in (h.get("files") or {})]
        if not empty:
            return []
        dl = downloads_dir()
        try:
            cands = [f for f in dl.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_IMG | MEDIA_VID]
        except Exception:
            return []
        now = time.time()
        used = set(h.get("used") or [])
        fresh = sorted([f for f in cands if f.stat().st_mtime > h.get("since", now) and f.name not in used
                        and now - f.stat().st_mtime > 2 and f.stat().st_size > 1000], key=lambda f: f.stat().st_mtime)
        got = []
        for f, i in zip(fresh, empty):
            got.append(self.handoff_take(pid, i, f))
        return got

    def _handoff_finish(self, pid):
        """Everything after the shots: the same finishing as any Ghost ad (or the pictures for an Images project)."""
        from .editor import probe
        p = self.store.load(pid)
        h = p.get("handoff") or {}
        files = h.get("files") or {}
        if p.get("kind") == "images":
            for k, f in files.items():
                st = p["stills"][int(k)]
                st.update(image=f, status="done", look="handoff")
            missing = [i for i, st in enumerate(p["stills"]) if not st.get("image")]
            h["finished"] = True
            p["steps"]["images"] = "done"
            for st_ in ("videos", "voice", "edit"):
                p["steps"][st_] = "skipped"
            p["status"], p["progress"] = "done", 100
            self.store.save(p)
            if missing:  # anything you didn't make there, Ghost makes itself
                for i in missing:
                    self._stills(pid, only=i)
            return
        (gw, gh), _, draft = sizes(p)
        videos = 0
        for k, f in files.items():
            c = p["clips"][int(k)]
            if Path(f).suffix.lower() in MEDIA_VID:
                c["video"], c["status"] = f, "done"
                try:
                    c["seconds"] = round(min(12.0, max(1.5, probe(f)["duration"])), 2)
                except Exception:
                    pass
                videos += 1
            else:
                c["image"] = f
        # Flow/Veo clips already speak their lines (with lip-sync): keep their sound. Shots made from pictures get a voice.
        p["clip_talks"] = videos > 0 and p.get("make_with") == "flow" and p.get("voice_mode") != "narrator"
        p["engine_plan"] = {**self.eng.plan(), "video": "handoff" if videos else "photo_motion"}
        if p["clip_talks"]:
            silent = [c for c in p["clips"] if not (c.get("video") and Path(c["video"]).suffix.lower() in MEDIA_VID)]
            for c in silent:
                if c.get("line"):
                    c["narration"] = c["line"]
            p["voice_mode"] = "both" if silent else "oncamera"
        h["finished"] = True
        p["status"] = "running"
        self._done(p, "images")
        missing = [i for i, c in enumerate(p["clips"]) if not c.get("video") and not c.get("image")]
        if missing:  # shots you skipped: Ghost makes the picture itself
            self._say(p, "images", f"Making the {len(missing)} shot(s) you didn't hand in", 50)
            if not p.get("image") or not Path(p["image"]).exists():
                self._images(pid)
                p = self.store.load(pid)
            q = self.cfg["quality"]
            for i in missing:
                if not p["clips"][i].get("image"):
                    self._shot_image(p, i, gw * (1 if draft else 2), gh * (1 if draft else 2),
                                     int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"]),
                                     [Path(r) for r in p.get("refs") or []], Path(p["image"]))
            self.store.save(p)
        for i, c in enumerate(p["clips"]):  # pictures become shots with a camera move (and talking mouths)
            if not c.get("video") and c.get("image"):
                out = self.store.dir(pid) / f"clip{i + 1:02d}_{c['seed']}_handoff.mp4"
                self.eng.photo_clip(Path(c["image"]), float(c.get("seconds") or 4), out, gw, gh)
                c["video"], c["status"] = str(out), "done"
        self.store.save(p)
        self._done(p, "videos")
        self._voice(pid)
        self._talk(pid)
        self._music_for_ad(pid)
        self._edit(pid)

    def _review_stop(self, p) -> bool:
        """"Check prompts first": stop after writing so you can edit, then Continue makes it."""
        if not p.get("review") or p.get("reviewed"):
            return False
        p["reviewed"] = True
        p["status"] = "review"
        p["log"].append(time.strftime("%H:%M:%S ") + "Ready for your check: edit anything, then press Make it.")
        self.store.save(p)
        return True

    # ------------------------------------------------------------ exact remake: same ad, new faces
    def _watch_reference(self, p) -> dict | None:
        """Gemini watches the whole reference (picture + sound) and lists every cut with its words. None without Google."""
        from . import links, llm
        g = self.eng.goog
        if not g.on or self.eng.mock:
            p["log"].append(time.strftime("%H:%M:%S ") + "Exact remake needs Sign in with Google (Gemini watches the reference). "
                            "Copying its look and words instead.")
            return None
        self._say(p, "analyse", "Gemini is watching the reference ad, cut by cut", 5)
        from .editor import ffmpeg_bin
        small = self.store.dir(p["id"]) / "ref_small.mp4"
        try:
            for h, crf in ((640, 30), (480, 34), (360, 38)):  # small enough for one request (about 18 MB)
                subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", p["reference"], "-vf", f"scale=-2:{h},fps=15", "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", str(crf), "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(small)], check=True, timeout=300)
                if small.stat().st_size <= links.INLINE:
                    break
            ans = links._gemini_media(g, small.read_bytes(), "video/mp4", planner.WATCH_REF)
            ref = llm._json_from(ans)
            scenes = [s for s in ref.get("scenes") or [] if float(s.get("end", 0)) > float(s.get("start", 0))]
            if not scenes:
                raise ValueError("no scenes")
            ref["scenes"], ref["watched"] = scenes, True
            ref["transcript"] = " ".join(str(s.get("line") or "") for s in scenes).strip() or p.get("ref_text", "")
            kinds = {}
            for s in scenes:
                kinds[s.get("kind")] = kinds.get(s.get("kind"), 0) + 1
            p["log"].append(time.strftime("%H:%M:%S ") + f"Reference: {len(scenes)} cuts, {float(scenes[-1]['end']):.0f} s — " +
                            ", ".join(f"{n} {k}" for k, n in kinds.items()))
            return ref
        except E.Cancelled:
            raise
        except Exception as e:
            p["log"].append(time.strftime("%H:%M:%S ") + f"Gemini couldn't watch the reference ({str(e)[:120]}); copying its look and words instead.")
            return None
        finally:
            small.unlink(missing_ok=True)

    def _remake(self, pid):
        """Shot for shot: scenes with people get new fictional faces (Veo); app screens, end cards and faceless footage are
        reused from the original; the words, timing and order stay; the original background music is kept when it can be
        separated (Song stems add-on)."""
        from .editor import ffmpeg_bin
        p = self.store.load(pid)
        d = self.store.dir(pid)
        ref = p["ref_analysis"]
        scenes = ref["scenes"]
        lang = p.get("language") or self.cfg.get("language", "Hindi")
        self._say(p, "plan", "Planning the remake shot by shot", 10)
        pl = planner.plan_remake(self.cfg, ref, lang, p.get("notes", ""))
        people = pl.get("people") or []
        main_look = (people[0].get("look") if people else "") or ""
        plan_scenes = {int(x.get("i", -1)): x for x in pl.get("scenes") or [] if isinstance(x, dict)}
        over = {int(x.get("i", -1)): str(x.get("line") or "") for x in pl.get("lines_over_reused") or [] if isinstance(x, dict)}
        clips, (gw, gh) = [], sizes(p)[0]
        _, (ow, oh), _ = sizes(p)
        for i, sc in enumerate(scenes):
            a, b = float(sc["start"]), float(sc["end"])
            if not planner.needs_new_face(sc):
                out = d / f"reuse{i + 1:02d}.mp4"
                subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", p["reference"],
                                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0",
                                "-vf", f"scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},setsar=1",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-shortest", str(out)], check=True, timeout=300)
                line = over.get(i, str(sc.get("line") or ""))
                clips.append({"speaker": "narrator", "line": line, "narration": line, "seconds": round(b - a, 2), "video": str(out),
                              "status": "done", "reused": True, "camera_move": "none", "shake": 0, "seed": random.randint(1, 10**6)})
                continue
            shots = (plan_scenes.get(i) or {}).get("shots") or [{"seconds": b - a, "speaker": sc.get("speaker"), "line": sc.get("line"),
                                                                   "image_prompt": f"{main_look}. {sc.get('place', '')}. {sc.get('framing', '')}",
                                                                   "video_prompt": f"{main_look}. {sc.get('action', '')}"}]
            total = sum(float(x.get("seconds") or 0) for x in shots) or (b - a)
            for x in shots:
                secs = round(float(x.get("seconds") or 0) * (b - a) / total, 2)  # exactly the original scene's length
                spk = str(x.get("speaker") or "none")
                line = str(x.get("line") or "")
                c = {"speaker": spk if spk not in ("narrator", "none") else "A", "line": line, "seconds": max(1.0, secs),
                     "image_prompt": still_prompt(x.get("image_prompt") or ""), "video_prompt": x.get("video_prompt") or "",
                     "camera_move": "none", "shake": 0, "status": "waiting", "seed": random.randint(1, 10**6), "scene": i}
                if spk in ("narrator", "none") and line:
                    c["narration"] = line  # heard over this shot, nobody on screen speaks
                clips.append(c)
        first = next((c for c in clips if not c.get("reused")), None)
        p["plan"] = {"title": pl.get("title") or p.get("title"), "character": {"look": main_look, "voice": (people[0].get("voice") if people else "")},
                     "people": people, "image_prompt": (first or {}).get("image_prompt") or main_look, "style": "remake", "cast": "people"}
        planner.lock_looks({"character": p["plan"]["character"], "people": people, "clips": [c for c in clips if not c.get("reused")]})
        p["clips"] = clips
        p["title"] = p.get("title") if p.get("title") not in (None, "", "New ad") else p["plan"]["title"]
        p["seconds"] = round(sum(float(c["seconds"]) for c in clips), 1)
        p["clip_talks"], p["voice_mode"] = True, "both"
        v = " ".join(str(x.get("voice") or "") + " " + str(x.get("look") or "") for x in (ref.get("people") or [])[:1]).lower()
        if re.search(r"\bfemale|woman|girl|lady\b", v):
            p["voice_gender"] = "female"
        elif re.search(r"\bmale|\bman\b|boy|gentleman", v):
            p["voice_gender"] = "male"  # the narrator's voice matches the original speaker
        if pl.get("changed"):
            p["log"].append(time.strftime("%H:%M:%S ") + "Changed to keep Ghost's rules: " + str(pl["changed"])[:300])
        if pl.get("note"):
            p["log"].append(pl["note"])
        n_new = sum(1 for c in clips if not c.get("reused"))
        p["log"].append(time.strftime("%H:%M:%S ") + f"Remake: {n_new} new shots with new faces, {len(clips) - n_new} reused from the original "
                        f"(screens, end card, faceless footage), {p['seconds']:.0f} s like the original.")
        self._done(p, "plan")
        self._keep_ref_music(p)
        if self._review_stop(p):
            return
        self._images(pid)
        self._videos(pid, resume=True)
        self._voice(pid)
        self._music_for_ad(pid)
        self._edit(pid)

    def _keep_ref_music(self, p):
        """The original's background music without its voice (needs the Song stems add-on)."""
        from . import addons
        if not addons.stems_installed(self.cfg):
            p["log"].append(time.strftime("%H:%M:%S ") + "Tip: install Song stems (⚙ → Add-ons) to keep the original's background music.")
            self.store.save(p)
            return
        try:
            self._say(p, "music", "Keeping the original background music (removing its voice)", 12)
            found = addons.split_stems(Path(p["reference"]), self.store.dir(p["id"]) / "ref_stems", two=True)
            if found.get("no_vocals"):
                p["music"], p["music_from_ref"] = found["no_vocals"], True
                p["log"].append(time.strftime("%H:%M:%S ") + "Original background music kept.")
        except E.Cancelled:
            raise
        except Exception as e:
            p["log"].append(time.strftime("%H:%M:%S ") + "Original music couldn't be separated: " + str(e)[:120])
        self.store.save(p)

    def _best_of_two(self, p, main: Path, w: int, h: int):
        """The main picture decides the whole ad's look: Gemini's, Imagen's and (with an OpenAI key) ChatGPT's are made,
        Gemini looks at them side by side and the best one is kept."""
        from . import llm
        g = self.eng.goog
        prompt = knowledge_apply(p["plan"]["image_prompt"])
        cands = [("Gemini", main)]
        alts = []
        self._say(p, "images", "Second opinions: the same picture by Imagen" + (" and ChatGPT" if self._openai() else "") +
                  ", then the best one is kept", 21)

        def imagen():
            f = main.with_name("frame_main_imagen.png")
            alts.append(f)
            g.imagen(prompt, f, w, h, seed=int(p.get("image_seed", 7)))
            return "Imagen", f

        def chatgpt():
            f = main.with_name("frame_main_chatgpt.png")
            alts.append(f)
            E.openai_image(self._openai(), prompt, f, w, h)
            return "ChatGPT", f
        from concurrent.futures import ThreadPoolExecutor
        jobs = [imagen] + ([chatgpt] if self._openai() else [])
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                futs = [ex.submit(j) for j in jobs]
                for fu in futs:
                    try:
                        cands.append(fu.result())
                    except E.Cancelled:
                        raise
                    except Exception as e:
                        why = "not enabled in your Google Cloud project" if "404" in str(e) or "not found" in str(e).lower() else str(e)[:90]
                        p["log"].append(time.strftime("%H:%M:%S ") + f"A second picture was skipped ({why})")
            if len(cands) < 2:
                return
            names = ", ".join(f"{i + 1} = {n}" for i, (n, _) in enumerate(cands))
            ans = g.ask(f"{len(cands)} candidate pictures ({names}, in that order) for the first frame of an ad. The brief: " +
                        p["plan"]["image_prompt"][:1200] + "\nPick the best: matches the brief, looks like a real photo, natural faces, "
                        "hands and eyes, believable place, no text or logos. Answer JSON {\"best\": <number>, \"why\": \"one short sentence\"}.",
                        [f for _, f in cands], json_out=True)
            pick = llm._json_from(ans) or {}
            k = max(1, min(len(cands), int(pick.get("best") or 1))) - 1
            who, f = cands[k]
            if k:
                shutil.copyfile(f, main)
            p["log"].append(time.strftime("%H:%M:%S ") + f"Main picture: {who}'s was the best of {len(cands)} — {str(pick.get('why') or '')[:140]}")
        except E.Cancelled:
            raise
        except Exception as e:
            p["log"].append(time.strftime("%H:%M:%S ") + f"Picture comparison skipped ({str(e)[:100]}): using Gemini's")
        finally:
            for f in alts:
                f.unlink(missing_ok=True)

    @staticmethod
    def _openai():
        """Your OpenAI (ChatGPT) brain, if one is in the chain and ready: its key also makes ChatGPT pictures."""
        from . import llm
        return next((e for e in llm.ready() if e.get("provider") == "openai" or "api.openai.com" in (e.get("base") or "")), None)

    def _cast_from_picture(self, p, main: Path):
        """Character sheet from what is really in the main picture (script + picture + reference), used in every shot."""
        pl = p.get("plan") or {}
        old = ((pl.get("character") or {}).get("look") or "").strip()
        lines = " / ".join(c.get("line", "") for c in p.get("clips") or [])[:800]
        ref = (p.get("ref_analysis") or {}).get("summary", "") if isinstance(p.get("ref_analysis"), dict) else ""
        try:
            ans = self.eng.goog.ask(
                "This is the main character of an ad, in its first picture. Script: " + lines + (" Reference ad: " + ref[:400] if ref else "") +
                "\nWrite the character sheet so a video model draws the SAME person in every shot: apparent age, gender, face shape, "
                "skin tone, hair (style, length, colour), the exact outfit with colours and patterns, jewellery and accessories. "
                "Only what is visible in the picture. One line, under 70 words, no names. If there is no person or animal, answer NONE.", [main])
        except E.Cancelled:
            raise
        except Exception as e:
            p["log"].append(time.strftime("%H:%M:%S ") + f"Character sheet skipped ({str(e)[:100]})")
            return
        new = ans.strip().strip('"').splitlines()[0].strip() if ans else ""
        if not new or new.upper().startswith("NONE") or len(new) < 25:
            return
        pl.setdefault("character", {})["look"] = new
        for pp in pl.get("people") or []:
            if not pp.get("look") or pp["look"].strip() == old:
                pp["look"] = new
        for c in p.get("clips") or []:
            for key in ("image_prompt", "video_prompt"):
                if c.get(key) and old and old in c[key]:
                    c[key] = c[key].replace(old, new)
        planner.lock_looks({"character": pl["character"], "people": pl.get("people") or [], "clips": p.get("clips") or []})
        p["log"].append(time.strftime("%H:%M:%S ") + "Character sheet from the picture: " + new[:160])
        self.store.save(p)

    def _style(self, p, ref=None) -> str:
        st = p.get("style") or "auto"
        if st == "auto" and ref:
            st = planner.style_from_reference(ref) or "auto"
            if st != "auto":
                p["log"].append(time.strftime("%H:%M:%S ") + f"Direction copied from the reference: {planner.STYLES_DIR[st]['name']}")
        p["style_used"] = st
        return st

    def _images(self, pid):
        from . import addons, llm
        addons.MusicServer.stop()  # pictures need the memory
        llm.unload()
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
                       ref_images=refs or None, steps=steps, fallback=refs[0] if refs else None, best=False)
        p["image"] = str(main)
        p["engine_plan"] = self.eng.plan()
        if self.eng.goog.on and not self.eng.mock:
            if not refs and self.cfg.get("api", {}).get("google_best_of_two", True) is not False:
                self._best_of_two(p, main, gw * k, gh * k)
            self._cast_from_picture(p, main)
        if self.eng.goog.broken and self.eng.goog.broken not in "".join(p["log"][-20:]):
            p["log"].append(time.strftime("%H:%M:%S ") + self.eng.goog.broken)
        style = p.get("style_used") or "auto"
        # One picture per person ("camera A", "camera B"), all in the same place as the main frame
        people = {pp.get("key"): pp for pp in (p["plan"].get("people") or []) if pp.get("key")}
        p["person_images"] = {}
        if len(people) >= 2 or style == "interview2":
            for n_, (key, pp) in enumerate(people.items()):
                f = d / f"frame_{key}.png"
                self._say(p, "images", f"Camera {key}: {pp.get('look', '')[:50]}", 22 + n_)
                prompt = pp.get("single_prompt") or f"{p['plan']['image_prompt']} Only this person in frame, medium close-up: {pp.get('look', '')}"
                self.eng.image(prompt, f, gw * k, gh * k, seed=11 + n_, ref_images=[main, *refs][:2], steps=steps)
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

    FRAMINGS = ["Medium shot, 50mm lens, the main character in the left third of the frame",
                "Tight close-up of the face, 85mm lens, eyes sharp, background softly blurred",
                "Wide shot, 24mm lens, the whole place visible, the character small in the frame",
                "Low-angle shot, 28mm lens, looking up at the character",
                "Three-quarter side angle, 35mm lens, the character looking off-frame",
                "Close-up from slightly above, 70mm lens, shallow depth of field"]

    def _shot_image(self, p, i, w, h, steps, refs, main):
        c = p["clips"][i]
        f = self.store.dir(p["id"]) / f"shot{i + 1:02d}_{c['seed']}.png"
        prompt = planner.still_prompt(c.get("image_prompt") or f"{p['plan']['image_prompt']} Moment: {c.get('line', '')}")
        prompt = planner.with_look(prompt, planner.look_of(p.get("plan") or {}, c.get("speaker")))
        if i > 0:  # every shot gets a clearly different framing, so the ad cuts instead of repeating one picture
            prompt = f"{self.FRAMINGS[i % len(self.FRAMINGS)]}. {prompt}"
        if main:  # the same person, outfit and place as the main picture: only framing and action change
            prompt = self.LOCK + prompt
        self.eng.image(prompt, f, w, h, seed=int(c["seed"]), ref_images=[main, *refs][:2], steps=steps,
                       fallback=refs[i % len(refs)] if refs else None)
        c["image"] = str(f)

    def _videos(self, pid, only: int | None = None, resume: bool = False):
        from . import llm
        llm.unload()
        p = self.store.load(pid)
        d = self.store.dir(pid)
        idx = [only] if only is not None else range(len(p["clips"]))
        (gw, gh), _, draft = sizes(p)
        steps = int(self.cfg["quality"].get("video_steps_draft", 12)) if draft else None
        n = len(p["clips"]) or 1
        if self.eng.plan()["video"] == "service" and only is None:  # cloud: every shot at the same time
            from concurrent.futures import ThreadPoolExecutor
            todo = [i for i in idx if not (resume and p["clips"][i].get("video") and p["clips"][i].get("status") == "done")]
            many = len(p["clips"]) > 1
            if many:  # character consistency: every shot starts from its own picture of the SAME person (made from the main one)
                self._shot_stills(p, todo, gw, gh, draft)
            self._say(p, "videos", f"Making {len(todo)} shots at the same time in the cloud (about 1–2 min)", 40)

            def mk(i):
                c = p["clips"][i]
                start = c.get("image") or p.get("person_images", {}).get(c.get("speaker")) or p["image"]
                out = d / f"clip{i + 1:02d}_{c['seed']}{'_r%d' % c['rev'] if c.get('rev') else ''}{'_draft' if draft else ''}.mp4"
                self.eng.video(Path(start), self._clip_prompt(p, c), float(c["seconds"]), out, seed=c["seed"],
                               width=gw, height=gh, steps=steps, aspect=p.get("aspect") or "9:16", keep_look=True)
                return i, out, start
            with ThreadPoolExecutor(max_workers=4) as ex:
                for i, out, start in ex.map(mk, todo):
                    p["clips"][i]["video"], p["clips"][i]["status"] = str(out), "done"
                    self._check_look(p, i, Path(start), out)
            self.store.save(p)
            self._done(p, "videos")
            return
        if self.eng.plan()["video"] == "local_motion":
            return self._motion(p, list(idx), resume, draft, only)
        for i in idx:
            c = p["clips"][i]
            if resume and c.get("video") and c.get("status") == "done" and Path(c["video"]).exists():
                continue  # already made before the pause
            self._say(p, "videos", f"Clip {i + 1} of {n}: {c.get('line','')[:50]}", 25 + int(55 * i / n))
            start = c.get("image") or p.get("person_images", {}).get(c.get("speaker")) or p["image"]
            out = d / f"clip{i + 1:02d}_{c['seed']}{'_r%d' % c['rev'] if c.get('rev') else ''}{'_draft' if draft else ''}.mp4"
            self.eng.video(Path(start), self._clip_prompt(p, c), float(c["seconds"]), out, seed=c["seed"],
                           width=gw, height=gh, steps=steps, aspect=p.get("aspect") or "9:16", keep_look=True)
            c["video"], c["status"] = str(out), "done"
            self.store.save(p)
        if only is None:
            self._done(p, "videos")

    @staticmethod
    def _clip_prompt(p, c) -> str:
        """The shot's video prompt with the full character look in it (never just "the woman")."""
        return planner.with_look(c.get("video_prompt") or "", planner.look_of(p.get("plan") or {}, c.get("speaker")))

    def _shot_stills(self, p, todo, gw, gh, draft):
        """One first-frame picture per shot, all of the same person (main picture as reference + LOCK), made in parallel."""
        from concurrent.futures import ThreadPoolExecutor
        main = Path(p["image"]) if p.get("image") and Path(p["image"]).exists() else None
        need = [i for i in todo if not (p["clips"][i].get("image") and Path(p["clips"][i]["image"]).exists())]
        if not need or not main:
            return
        q = self.cfg["quality"]
        refs = [Path(r) for r in p.get("refs") or []]
        steps = int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"])
        self._say(p, "videos", f"Same person in every shot: making {len(need)} first frames from the main picture", 35)

        def one(i):
            anchor = Path(p.get("person_images", {}).get(p["clips"][i].get("speaker")) or main)
            try:
                self._shot_image(p, i, gw * (1 if draft else 2), gh * (1 if draft else 2), steps, refs, anchor)
            except Exception as e:  # the shot then starts from the main picture itself (still the same person)
                p["log"].append(time.strftime("%H:%M:%S ") + f"Shot {i + 1} picture skipped ({str(e)[:80]}): starts from the main picture")
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, need))
        self.store.save(p)

    def _check_look(self, p, i, start: Path, clip: Path):
        """Did the video model really start from the shot's picture? (If not, the person can drift.)"""
        try:
            from . import dna
            from .editor import ffmpeg_bin
            first = clip.with_name(clip.stem + "_f0.png")
            subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(clip), "-frames:v", "1", str(first)], check=True, timeout=60)
            score = dna.similarity(start, first)
            first.unlink(missing_ok=True)
            p["clips"][i]["match"] = score
            if score < 70:
                p["log"].append(time.strftime("%H:%M:%S ") + f"Shot {i + 1}: the video looks different from its picture ({score}% alike). "
                                "Retake that shot if the person changed.")
        except Exception:
            pass

    MOTION_PROMPT = ("{shot}. {move} Realistic live-action footage shot on a phone camera, natural motion, real physics, "
                     "natural light, sharp focus, no text, no subtitles, no watermark.")

    def _motion(self, p, idx, resume, draft, only):
        """AI motion on this Mac: every shot of the ad in one run of the video model (it loads once)."""
        from . import addons, knowledge
        d = self.store.dir(p["id"])
        todo = [i for i in idx if not (resume and p["clips"][i].get("video") and p["clips"][i].get("status") == "done"
                                       and Path(p["clips"][i]["video"]).exists())]
        if not todo:
            if only is None:
                self._done(p, "videos")
            return
        portrait = (p.get("aspect") or "9:16") != "16:9"
        w, h = (448, 832) if draft else (704, 1280)  # the model's own sizes: fast preview, or 720p
        if not portrait:
            w, h = h, w
        shots, n = [], len(todo)
        for i in todo:
            c = p["clips"][i]
            shot = c.get("image_prompt") or p["plan"].get("image_prompt", "")
            if i > 0:
                shot = f"{self.FRAMINGS[i % len(self.FRAMINGS)]}. {shot}"
            move = re.sub(r"\b(Says|Speaks?)\b.*?(\.|$)", "", c.get("video_prompt") or "")[:300]
            if c.get("edits"):  # ✨ Change on a moving shot: the change goes into the description, then it is re-made
                shot += ". " + "; ".join(c["edits"][-4:])
            prompt = knowledge.apply(self.MOTION_PROMPT.format(shot=shot.strip().rstrip("."), move=move.strip()))
            out = d / f"clip{i + 1:02d}_{c['seed']}{'_r%d' % c['rev'] if c.get('rev') else ''}{'_draft' if draft else ''}.mp4"
            shots.append({"prompt": prompt[:1500], "out": str(out), "seconds": min(5.0, float(c.get("seconds") or 4)), "seed": int(c["seed"])})
        t0 = time.time()
        done = {"n": 0}

        def prog(ev):
            k = int(ev.get("i") or 0)
            if ev.get("seconds"):
                done["n"] = k
            el = time.time() - t0
            eta = f" · about {int(el / done['n'] * (n - done['n']) / 60) + 1} min left" if done["n"] else ""
            self._say(p, "videos", f"AI motion on this Mac — {ev['progress']}{eta}", 30 + int(50 * min(k, n) / n))
        E.free_all()  # the picture model leaves memory: the motion model needs it
        self._say(p, "videos", f"AI motion on this Mac: {n} shot{'s' if n > 1 else ''} (first one also loads the model)", 30)
        stop = lambda: p["id"] in self.cancel or E.STOP["flag"]  # noqa: E731
        try:
            files = addons.make_motion(shots, w, h, fast=draft, on_progress=prog, cancelled=stop)
        except RuntimeError:
            if stop():
                raise E.Cancelled("Paused") from None
            raise
        for i, f in zip(todo, files):
            c = p["clips"][i]
            c["video"], c["status"] = f, "done"
            try:  # the clip's first frame is the shot's picture (thumbnail, Change, Retake)
                from .editor import ffmpeg_bin
                img = Path(f).with_suffix(".png")
                subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", f, "-frames:v", "1", str(img)], check=True)
                c["image"] = str(img)
            except Exception:
                pass
        p["log"].append(time.strftime("%H:%M:%S ") + f"AI motion: {len(files)} shot(s) in {int(time.time() - t0)} s")
        self.store.save(p)
        if only is None:
            self._done(p, "videos")

    def _voice(self, pid):
        p = self.store.load(pid)
        d = self.store.dir(pid)
        mode = p.get("voice_mode", "oncamera")
        if not (self.eng.talks or p.get("clip_talks")) and mode != "narrator":
            p["log"].append(time.strftime("%H:%M:%S ") + "This computer makes photo-motion ads, so a narrator speaks the lines.")
            mode = "narrator"
        if mode == "oncamera":
            p["steps"]["voice"] = "skipped"
            self.store.save(p)
            return
        self._say(p, "voice", "Recording the voices", 85)
        # one voice per character: the first speaker gets the chosen voice, the next one the other voice, and so on
        order = []
        for c in p["clips"]:
            if c.get("speaker") and c["speaker"] not in order:
                order.append(c["speaker"])
        mine = p.get("voice_gender", "female")
        other = "male" if mine == "female" else "female"
        for i, c in enumerate(p["clips"]):
            text = c.get("narration") or (c.get("line") if mode == "narrator" else "")
            if not text:
                continue
            k = order.index(c["speaker"]) if c.get("speaker") in order and not c.get("narration") else 0
            f = d / f"narr{i + 1:02d}.wav"
            self.eng.speech(text, f, p.get("language") or self.cfg.get("language", "Hindi"), mine if k % 2 == 0 else other,
                            p.get("voice_style", "warm") if k < 2 else "calm")
            c["narration_file"] = str(f)
            # each shot lasts as long as its line (+ a breath), so the ad has no dead air between lines
            if not c.get("narration"):
                try:
                    from .editor import probe
                    c["seconds"] = round(max(2.5, probe(f)["duration"] + 0.55), 2)
                except Exception:
                    pass
        self._done(p, "voice")

    def _talk(self, pid):
        """Free mode: make each speaking character's mouth move with its voice (see talk.py)."""
        from . import talk
        p = self.store.load(pid)
        if (p.get("engine_plan") or {}).get("video") != "photo_motion" or p.get("talking") is False or not self.eng.can_edit() or self.eng.mock:
            return
        speak = [c for c in p["clips"] if c.get("narration_file") and c.get("line") and not c.get("narration")]
        if not speak:
            return
        d = self.store.dir(pid)
        q = self.cfg["quality"]
        (gw, gh), _, draft = sizes(p)
        looks = {pp.get("key"): pp.get("look", "") for pp in (p.get("plan") or {}).get("people") or []}
        best = p.get("quality") != "draft"
        kinds = ("open", "blink") + (("half", "round") if best else ())
        made: dict[str, dict] = {}
        for n, c in enumerate(speak):
            self._say(p, "voice", f"Making the mouths move with the voices ({n + 1} of {len(speak)})", 86 + int(4 * n / len(speak)))
            closed = c.get("image") or (p.get("person_images") or {}).get(c.get("speaker")) or p.get("image")
            if not closed or not Path(closed).exists():
                continue
            if c.get("video") and Path(c["video"]).name.startswith("talk") and Path(c["video"]).exists():
                continue  # already talking
            if closed not in made:
                who = (looks.get(c.get("speaker")) or "the character").split(",")[0][:80]
                got = {}
                for kind in kinds:  # mouth shapes + a blink, each an edit of the same picture (cached on disk)
                    f = d / f"{Path(closed).stem}_talk{'' if kind == 'open' else '_' + kind}.png"
                    if f.exists():
                        got[kind] = str(f)
                        continue
                    if kind != "open" and "open" not in got:
                        break  # no usable mouth: don't spend time on the rest
                    try:
                        r = talk.variant(self.eng, Path(closed), f, kind, who)
                        if r:
                            got[kind] = str(r)
                    except E.Cancelled:
                        raise
                    except Exception as e:
                        p["log"].append(time.strftime("%H:%M:%S ") + f"Talking {kind} skipped: " + str(e)[:120])
                made[closed] = got
            got = made[closed]
            if not got.get("open"):
                continue
            i = p["clips"].index(c)
            out = d / f"talk{i + 1:02d}_{c['seed']}.mp4"
            try:
                talk.talk_clip(Path(closed), Path(got["open"]), Path(c["narration_file"]), float(c["seconds"]), out,
                               int(q["fps"]), gw, gh, half=got.get("half"), round_=got.get("round"), blink=got.get("blink"),
                               seed=int(c.get("seed") or 0))
                p["clips"][i]["video"] = str(out)
            except Exception as e:
                p["log"].append(time.strftime("%H:%M:%S ") + "Talking shot skipped: " + str(e)[:120])
            self.store.save(p)

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
        style = p.get("style_used") or p.get("style") or "auto"
        for c in p["clips"]:
            if not c.get("video"):
                continue
            mv = c.get("camera_move", "none")
            by_hand = bool(c.get("by_hand"))  # you set the move / shake on this shot yourself
            if photo and mv == "none":
                mv = "push_in"  # a still photo always needs a little life
            if not photo and not by_hand:
                mv = "none"  # real video already has its own camera motion: no extra zoom or wobble on top
            shake = float(c.get("shake") or 0) if (by_hand or style == "ugc") else 0.0
            clips.append({"video": c["video"], "duration": c["seconds"], "move": mv,
                          "amount": c.get("amount", 1) if photo or by_hand else 1, "shake": shake, "mute": mode == "narrator"})
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
               music=p.get("music"), music_volume=0.55 if p.get("music_from_ref") else 0.18, log=lambda m: None, size=(ow, oh), grade=grade,
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
        self._talk(pid)
        self._edit(pid)

    def _reedit(self, pid):
        self._edit(pid)

    # ------------------------------------------------------------ music
    def _music_for_ad(self, pid):
        from . import addons
        p = self.store.load(pid)
        if not p.get("ai_music") or p.get("music") or not (addons.music_installed() or self.eng.fal.on):
            p["steps"]["music"] = "skipped"
            self.store.save(p)
            return
        E.free()
        total = sum(float(c.get("seconds", 0)) for c in p["clips"]) + 1
        style = (p.get("plan") or {}).get("music_style") or "warm festive Indian background music"
        self._say(p, "music", "Composing background music to fit the ad", 88)
        if self.eng.fal.on:
            try:
                tr = self.eng.fal.music(caption=style + ". Instrumental background, leaves room for voice.", lyrics="[Instrumental]",
                                        seconds=max(10, total), language="unknown", out_dir=self.store.dir(pid), thinking=False)
                p = self.store.load(pid)
                p["music"], p["music_ai"] = tr[0]["file"], True
                self._done(p, "music")
            except Exception as e:
                p = self.store.load(pid)
                p["log"].append(time.strftime("%H:%M:%S ") + "Music skipped: " + str(e)[:200])
                p["steps"]["music"] = "skipped"
                self.store.save(p)
            return
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
        pl = planner.plan_music(self.cfg, idea=brief(p), style=p.get("music_style", "bollywood"), vocals=p.get("vocals", "ai"),
                                lyrics=p.get("dialogue", ""), seconds=int(p.get("music_seconds", 60)), language=p.get("language"))
        if p.get("title") in (None, "", "New ad"):
            p["title"] = pl.get("title") or "Song"
        p["song"] = pl
        if pl.get("note"):
            p["log"].append(pl["note"])
        p["tracks"] = p.get("tracks") or []
        self._done(p, "plan")
        if self._review_stop(p):
            return
        self._compose(pid, takes=1)  # one take = half the time; "Make another take" adds more

    def _compose(self, pid, takes=1):
        from . import addons
        p = self.store.load(pid)
        if not addons.music_installed() and not self.eng.fal.on:
            raise RuntimeError("Install the Music & songs add-on first (⚙ → Add-ons).")
        pl = p["song"]
        E.free()
        from . import llm
        llm.unload()
        self._say(p, "music", "Starting the music engine", 20)
        if self.eng.fal.on:  # cloud: same model, much faster
            self._say(p, "music", "Composing in the cloud (about 20–60 s)", 30)
            out = self.eng.fal.music(caption=pl["caption"], lyrics=pl.get("lyrics", "") or "", seconds=pl.get("seconds", 60),
                                     language=pl.get("language", "unknown"), out_dir=self.store.dir(pid), takes=takes, bpm=pl.get("bpm"),
                                     thinking=p.get("quality") != "draft" or (pl.get("lyrics") or "[Instrumental]") != "[Instrumental]")
            p = self.store.load(pid)
            for t in out:
                p["tracks"].append({**t, "n": len(p["tracks"]) + 1})
            p["steps"]["music"] = "done"
            p["status"], p["progress"] = "done", 100
            p["log"].append(time.strftime("%H:%M:%S ") + f"Finished {len(out)} take{'s' if len(out) > 1 else ''}")
            self.store.save(p)
            return
        try:
            mine = p.get("music") if p.get("music") and Path(p["music"]).exists() else None  # "Cover my song / my humming"
            if mine:
                self._say(p, "music", "Making a cover of your audio (same tune, new sound)", 25)
            out = addons.MusicServer.generate(caption=pl["caption"], lyrics=pl.get("lyrics", ""), seconds=pl.get("seconds", 60),
                                              language=pl.get("language", "unknown"), out_dir=self.store.dir(pid), takes=takes,
                                              bpm=pl.get("bpm"), describe="" if mine else pl.get("describe", ""), fast=p.get("quality") == "draft",
                                              src_audio=mine, cover_strength=float(p.get("cover_strength", 0.45)),
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

    def _resume(self, pid):
        """Continue after a pause, keeping everything already made."""
        p = self.store.load(pid)
        p["log"].append(time.strftime("%H:%M:%S ") + "Continuing")
        self.store.save(p)
        if p.get("make_with") in HANDOFF_APPS and (p.get("stills") or p.get("clips")) and not (p.get("handoff") or {}).get("finished"):
            return self._handoff_start(pid)
        if p.get("kind") == "images" and p.get("stills"):
            return self._stills(pid, resume=True)
        if p.get("kind") == "music":
            return self._compose(pid, takes=1) if p.get("song") else self._all(pid)
        if p.get("kind") == "dub":
            return self._dub_finish(pid) if (p.get("dub") or {}).get("segments") else self._dub(pid)
        if not p.get("clips"):
            return self._all(pid)
        if not p.get("image") or not Path(p["image"]).exists():
            self._images(pid)
        self._videos(pid, resume=True)
        self._voice(pid)
        self._talk(pid)
        self._music_for_ad(pid)
        self._edit(pid)

    def _more_music(self, pid):
        p = self.store.load(pid)
        p["steps"]["music"] = "waiting"
        self.store.save(p)
        self._compose(pid, takes=1)

    # ------------------------------------------------------------ images only
    def _images_only(self, pid, ref=None):
        p = self.store.load(pid)
        self._say(p, "plan", "Writing the image prompts", 8)
        pl = planner.plan_images(self.cfg, idea=brief(p) or p.get("dialogue", ""), reference=ref,
                                 count=int(p.get("count", 4)), aspect=p.get("aspect") or "9:16", language=p.get("language"),
                                 style=self._style(p, ref), fast=p.get("quality") == "draft", cast=p.get("cast", "auto"))
        if p.get("title") in (None, "", "New ad"):
            p["title"] = pl.get("title") or "Images"
        p.pop("anchor", None); p.pop("anchor_i", None)  # a new set starts a new main look
        p["stills"] = [{"caption": i.get("caption", ""), "prompt": i["prompt"], "seed": random.randint(1, 10**6), "status": "waiting"}
                       for i in pl["images"]]
        if pl.get("note"):
            p["log"].append(pl["note"])
        self._done(p, "plan")
        if self._review_stop(p):
            return
        if p.get("make_with") in HANDOFF_APPS:
            return self._handoff_start(pid)
        self._stills(pid)

    LOCK = ("Keep EXACTLY the same main subject as in the reference image (the same person: face, skin tone, hair, outfit, colours and "
            "jewellery; or the same animal or object: same markings, colours and shape), and the same place, light and colour mood. "
            "Only the framing, pose and action change: ")

    def _stills(self, pid, only: int | None = None, mode: str = "same", resume: bool = False):
        from . import addons, llm
        addons.MusicServer.stop()  # pictures need the memory
        llm.unload()
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
        if self.eng.plan()["image"] == "service" and len(list(idx)) > 1:
            return self._stills_parallel(p, list(idx), w, h, steps, refs, same, anchor, draft, resume)
        for i in idx:
            s = p["stills"][i]
            if resume and s.get("image") and s.get("status") == "done" and Path(s["image"]).exists():
                continue
            eta = f" · about {int(sum(took) / len(took))} s each" if took else (" · first one also loads the model" if i == 0 else "")
            self._say(p, "images", f"Image {i + 1} of {n}: {s.get('caption', '')}{eta}", 15 + int(80 * i / n))
            t0 = time.time()
            f = d / f"image{i + 1:02d}_{s['seed']}{'_draft' if draft else ''}.png"
            # Same look: every picture after the main one follows it (same person + place), via the reference model
            use_anchor = same and anchor is not None and self.eng.plan()["image"] in ("local", "nvidia", "service")
            ref_imgs = ([anchor] if use_anchor else []) + refs
            prompt = (self.LOCK + s["prompt"]) if use_anchor else s["prompt"]
            self.eng.image(prompt, f, w, h, seed=int(s["seed"]), ref_images=ref_imgs[:3] or None, steps=steps,
                           fallback=refs[i % len(refs)] if refs else None, best=p.get("quality") != "draft")
            f = self._checked(p, i, f, lambda seed, out: self.eng.image(prompt, out, w, h, seed=seed, ref_images=ref_imgs[:3] or None,
                                                                        steps=steps, fallback=None, best=p.get("quality") != "draft"), draft)
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

    def _stills_parallel(self, p, idx, w, h, steps, refs, same, anchor, draft, resume):
        """Cloud: the main look first, then every other picture at the same time (seconds, not minutes)."""
        from concurrent.futures import ThreadPoolExecutor
        d = self.store.dir(p["id"])
        todo = [i for i in idx if not (resume and p["stills"][i].get("status") == "done" and p["stills"][i].get("image"))]

        def make(i, ref_list):
            s = p["stills"][i]
            f = d / f"image{i + 1:02d}_{s['seed']}{'_draft' if draft else ''}.png"
            prompt = (self.LOCK + s["prompt"]) if ref_list and same else s["prompt"]
            self.eng.image(prompt, f, w, h, seed=int(s["seed"]), ref_images=ref_list or None, steps=steps, best=not draft)
            return i, f
        if todo and same and anchor is None:
            first = todo.pop(0)
            self._say(p, "images", f"Image {first + 1}: the main look", 20)
            i, f = make(first, refs[:3])
            p["stills"][i].update(image=str(f), status="done", look="fresh")
            anchor = f
            p["anchor"], p["anchor_i"] = str(f), i
            self.store.save(p)
        if todo:
            self._say(p, "images", f"Making {len(todo)} pictures at the same time", 45)
            with ThreadPoolExecutor(max_workers=4) as ex:
                for i, f in ex.map(lambda j: make(j, ([anchor] if same and anchor else []) + refs[:2]), todo):
                    p["stills"][i].update(image=str(f), status="done", look="same" if same and anchor else "fresh")
            self.store.save(p)
        for st in ("videos", "voice", "edit"):
            p["steps"][st] = "skipped"
        p["steps"]["images"] = "done"
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Finished {len(idx)} images")
        self.store.save(p)

    def _checked(self, p, i, f: Path, remake, draft: bool) -> Path:
        """Final pictures: if the vision check finds bad hands/eyes/face, make one more take and keep the better one."""
        from . import qc
        if draft or not qc.available() or self.eng.plan()["image"] == "photos":
            return f
        from . import llm
        try:
            r1 = qc.check(f)
            llm.unload()  # the checking model leaves memory before the next picture
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

    # ------------------------------------------------------------ live editing ("Change")
    def _edit_pic(self, p, src: Path, instruction: str, tag: str) -> Path:
        out = self.store.dir(p["id"]) / f"{tag}_edit{int(time.time() * 1000) % 10**8}.png"
        return self.eng.edit_image(src, instruction, out, seed=random.randint(1, 10**6))

    def _change_still(self, pid, i: int, instruction: str, scope: str = "one"):
        """Edit the SAME picture(s): "shorter hair", "make it a man". The old version is kept for Undo."""
        from . import addons, llm
        addons.MusicServer.stop()
        llm.unload()
        p = self.store.load(pid)
        idx = [j for j, s in enumerate(p["stills"]) if s.get("image")] if scope == "all" else [i]
        if scope == "all":
            p.setdefault("undo", []).append({"what": instruction, "stills": [dict(image=s.get("image"), edits=list(s.get("edits") or [])) for s in p["stills"]],
                                             "anchor": p.get("anchor")})
            p["undo"] = p["undo"][-10:]
        for n, j in enumerate(idx):
            s = p["stills"][j]
            src = Path(s.get("image") or "")
            if not src.exists():
                continue
            self._say(p, "images", f"Changing image {j + 1}: {instruction}", 10 + int(85 * n / len(idx)))
            out = self._edit_pic(p, src, instruction, f"image{j + 1:02d}")
            if scope != "all":
                s.setdefault("history", []).append({"image": str(src), "what": instruction})
                s["history"] = s["history"][-10:]
            s["image"], s["status"] = str(out), "done"
            s.setdefault("edits", []).append(instruction)
            if p.get("anchor_i") == j or p.get("anchor") == str(src):
                p["anchor"] = str(out)  # the main look follows the change, so new pictures match it
            self.store.save(p)
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Changed: {instruction}")
        self.store.save(p)

    def _change_clip(self, pid, i: int, instruction: str, scope: str = "one"):
        """Edit the SAME shot (or every shot): its picture is changed, then only those shots are re-made."""
        from . import addons, llm
        addons.MusicServer.stop()
        llm.unload()
        p = self.store.load(pid)
        idx = list(range(len(p["clips"]))) if scope == "all" else [i]
        if scope == "all":
            p.setdefault("undo", []).append({"what": instruction, "image": p.get("image"), "person_images": dict(p.get("person_images") or {}),
                                             "clips": [dict(image=c.get("image"), video=c.get("video"), rev=c.get("rev"),
                                                           edits=list(c.get("edits") or [])) for c in p["clips"]]})
            p["undo"] = p["undo"][-10:]
        done: dict[str, str] = {}  # each picture is changed once, even if several shots share it

        def changed(src: str, tag: str) -> str:
            if src and src not in done and Path(src).exists():
                self._say(p, "images", f"Changing the picture: {instruction}", 15)
                done[src] = str(self._edit_pic(p, Path(src), instruction, tag))
            return done.get(src, src)
        if scope == "all":
            if p.get("image"):
                p["image"] = changed(p["image"], "frame_main")
            for k, f in (p.get("person_images") or {}).items():
                p["person_images"][k] = changed(f, f"frame_{k}")
        for j in idx:
            c = p["clips"][j]
            start = c.get("image") or (p.get("person_images") or {}).get(c.get("speaker")) or p.get("image")
            if scope != "all":
                c.setdefault("history", []).append({"image": c.get("image"), "video": c.get("video"), "what": instruction})
                c["history"] = c["history"][-10:]
            c["image"] = changed(start, f"shot{j + 1:02d}")
            c["status"] = "waiting"
            c["rev"] = int(c.get("rev") or 0) + 1  # a new file, so Undo can bring back the old one
            c.setdefault("edits", []).append(instruction)
        self.store.save(p)
        if scope == "all":
            self._videos(pid)
        else:
            self._videos(pid, only=i)
        self._talk(pid)
        self._edit(pid)

    def _change_song(self, pid, n: int, instruction: str, strength: float = 0.5):
        """Same song, new sound: keeps the melody and words (local music engine, cover mode) and adds it as a new take."""
        from . import addons
        p = self.store.load(pid)
        tr = next((t for t in p.get("tracks") or [] if int(t.get("n", 0)) == int(n)), None)
        if not tr or not Path(tr["file"]).exists():
            raise RuntimeError("That take is missing; make a new one first.")
        pl = p.get("song") or {}
        self._say(p, "plan", f"Changing take {n}: {instruction}", 10)
        caption = planner.rewrite(self.cfg, tr.get("caption") or pl.get("caption", ""), instruction, kind="caption")
        lyrics = tr.get("lyrics") or pl.get("lyrics") or "[Instrumental]"
        secs = float(tr.get("seconds") or pl.get("seconds") or 60)
        E.free()
        from . import llm
        llm.unload()
        if addons.music_installed():
            self._say(p, "music", "Re-recording the same song with the change", 30)
            try:
                out = addons.MusicServer.generate(caption=caption, lyrics=lyrics, seconds=secs, language=pl.get("language", "unknown"),
                                                  out_dir=self.store.dir(pid), takes=1, bpm=tr.get("bpm") or pl.get("bpm"),
                                                  src_audio=tr["file"], cover_strength=strength,
                                                  log=lambda m: self._say(self.store.load(pid), "music", m))
            finally:
                addons.MusicServer.stop_later()
            how = "same tune"
        elif self.eng.fal.on:
            self._say(p, "music", "Making it in the cloud with the change (the tune can differ a little)", 30)
            out = self.eng.fal.music(caption=caption, lyrics=lyrics, seconds=secs, language=pl.get("language", "unknown"),
                                     out_dir=self.store.dir(pid), takes=1, bpm=tr.get("bpm") or pl.get("bpm"))
            how = "new tune"
        else:
            raise RuntimeError("Install the Music & songs add-on first (⚙ → Add-ons).")
        p = self.store.load(pid)
        for t in out:
            p["tracks"].append({**t, "caption": caption, "lyrics": lyrics, "n": len(p["tracks"]) + 1,
                                "from": int(n), "change": instruction, "how": how})
        p["steps"]["music"] = "done"
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Changed take {n}: {instruction} → take {len(p['tracks'])}")
        self.store.save(p)

    def _replace_part(self, pid, n: int, start: float, end: float, instruction: str = "", lyrics: str = ""):
        """Suno-style "Replace section": re-make only start..end of a take; everything else stays exactly as it was."""
        from . import addons
        p = self.store.load(pid)
        tr = next((t for t in p.get("tracks") or [] if int(t.get("n", 0)) == int(n)), None)
        if not tr or not Path(tr["file"]).exists():
            raise RuntimeError("That take is missing.")
        if not addons.music_installed():
            raise RuntimeError("Replacing a part needs the Music & songs add-on (⚙ → Add-ons).")
        pl = p.get("song") or {}
        caption = tr.get("caption") or pl.get("caption", "")
        if instruction:
            caption = planner.rewrite(self.cfg, caption, instruction, kind="caption")
        self._say(p, "music", f"Re-making {start:.0f}–{end:.0f} s of take {n}" + (f": {instruction}" if instruction else ""), 20)
        E.free()
        from . import llm
        llm.unload()
        try:
            out = addons.MusicServer.generate(caption=caption, lyrics=lyrics or tr.get("lyrics") or pl.get("lyrics") or "[Instrumental]",
                                              seconds=float(tr.get("seconds") or pl.get("seconds") or 60), language=pl.get("language", "unknown"),
                                              out_dir=self.store.dir(pid), takes=1, src_audio=tr["file"], task="repaint", repaint=(start, end),
                                              log=lambda m: self._say(self.store.load(pid), "music", m))
        finally:
            addons.MusicServer.stop_later()
        p = self.store.load(pid)
        for t in out:
            p["tracks"].append({**t, "caption": caption, "lyrics": lyrics or tr.get("lyrics"), "n": len(p["tracks"]) + 1, "from": int(n),
                                "change": f"{start:.0f}–{end:.0f} s re-made" + (f": {instruction}" if instruction else ""), "how": "same tune"})
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Replaced {start:.0f}–{end:.0f} s of take {n} → take {len(p['tracks'])}")
        self.store.save(p)

    def _stems(self, pid, n: int):
        """Split a take into vocals, drums, bass and the rest (Demucs)."""
        from . import addons
        p = self.store.load(pid)
        tr = next((t for t in p.get("tracks") or [] if int(t.get("n", 0)) == int(n)), None)
        if not tr:
            raise RuntimeError("That take is missing.")
        if not addons.stems_installed():
            raise RuntimeError("Install the Song stems add-on first (⚙ → Add-ons).")
        self._say(p, "music", f"Splitting take {n} into vocals, drums, bass and the rest (about 1–2 min)", 30)
        found = addons.split_stems(Path(tr["file"]), self.store.dir(pid) / f"stems{n}")
        p = self.store.load(pid)
        for t in p["tracks"]:
            if int(t.get("n", 0)) == int(n):
                t["stems"] = found
        p["status"], p["progress"] = "done", 100
        p["log"].append(time.strftime("%H:%M:%S ") + f"Take {n} split into {len(found)} stems")
        self.store.save(p)

    # ------------------------------------------------------------ dubbing
    def _dub(self, pid):
        from . import dub
        p = self.store.load(pid)
        d = self.store.dir(pid)
        for st in ("images", "videos", "music"):
            p["steps"][st] = "skipped"
        src = Path(p.get("reference") or "")
        if not src.exists():
            raise RuntimeError("Add the video to dub (press + and choose the video).")
        self._say(p, "analyse", "Listening to the video", 5)
        segs, code = self.eng.transcribe_segments(src)
        if not segs:
            raise RuntimeError("Could not hear any speech in the video. Dubbing needs the Script AI add-on (it includes speech-to-text).")
        src_lang = dub.LANG_NAMES.get(code, code or "")
        target = p.get("language") or "English"
        p["dub"] = {"from": src_lang, "to": target, "segments": segs, "seconds": dub.probe(src)["duration"]}
        if p.get("title") in (None, "", "New ad"):
            p["title"] = f"{(p.get('idea') or 'Video')[:40]} - {target} dub"
        p["log"].append(time.strftime("%H:%M:%S ") + f"Heard {len(segs)} sentences in {src_lang or 'the video'}")
        self._done(p, "analyse")
        self._say(p, "plan", f"Translating {len(segs)} sentences into {target}", 20)
        from . import llm
        lines = planner.translate_lines(self.cfg, [s["text"] for s in segs], src_lang, target, p.get("idea", ""))
        llm.unload()
        p = self.store.load(pid)
        for s, t in zip(p["dub"]["segments"], lines):
            s["dub"] = t
        self._done(p, "plan")
        if self._review_stop(p):
            return
        self._dub_finish(pid)

    def _dub_finish(self, pid):
        from . import addons, dub
        p = self.store.load(pid)
        d = self.store.dir(pid)
        src = Path(p["reference"])
        segs = p["dub"]["segments"]
        target = p["dub"]["to"]
        self._say(p, "voice", f"Recording {len(segs)} lines in {target}", 40)
        raw = [{"text": s.get("dub") or s["text"], "out": str(d / f"dubraw{i:03d}.wav")} for i, s in enumerate(segs)]
        self.eng.speak_many(raw, target, p.get("voice_gender", "female"), p.get("voice_style", "warm"))
        total = p["dub"].get("seconds") or dub.probe(src)["duration"]
        lines, sped = [], 0
        for i, (s, slot) in enumerate(zip(segs, dub.slots(segs, total))):
            E.check_stop()
            f = d / f"dub{i:03d}.wav"
            length = dub.fit(Path(raw[i]["out"]), f, slot)
            sped += length < (dub.probe(Path(raw[i]["out"]))["duration"] - 0.05)
            lines.append({"file": str(f), "at": s["start"]})
        self._done(p, "voice")
        self._say(p, "edit", "Keeping the background and mixing", 85)
        bg, vol = None, 0.12
        whole = dub.extract_audio(src, d / "dub_original.wav")
        if addons.stems_installed():
            try:
                found = addons.split_stems(whole, d / "dub_stems", two=True)
                bg, vol = Path(found.get("no_vocals") or ""), 1.0
                if not bg.exists():
                    bg, vol = whole, 0.12
            except Exception as e:
                p["log"].append(time.strftime("%H:%M:%S ") + "Background split skipped: " + str(e)[:120])
                bg, vol = whole, 0.12
        else:
            bg = whole
            p["log"].append(time.strftime("%H:%M:%S ") + "Tip: install Song stems to remove the original voice and keep only the music.")
        safe = "".join(ch for ch in p["title"] if ch.isalnum() or ch in " -_").strip() or "dubbed"
        out = dub.mix(src, bg, vol, lines, d / f"{safe} - dubbed.mp4")
        p = self.store.load(pid)
        p["final"] = str(out)
        p["status"], p["progress"] = "done", 100
        if sped:
            p["log"].append(time.strftime("%H:%M:%S ") + f"{sped} line(s) were sped up a little to fit their moment. Shorter wording fits better (✎ Edit).")
        self._done(p, "edit")
        p["log"].append(time.strftime("%H:%M:%S ") + "Finished: " + out.name)
        self.store.save(p)

    def _undo_all(self, pid):
        """Undo the last change that was applied to every picture / shot."""
        p = self.store.load(pid)
        u = (p.get("undo") or []).pop() if p.get("undo") else None
        if not u:
            return
        if "stills" in u:
            for s, old in zip(p["stills"], u["stills"]):
                if old.get("image"):
                    s["image"], s["edits"] = old["image"], old.get("edits") or []
            p["anchor"] = u.get("anchor") or p.get("anchor")
            p["status"] = "done"
            p["log"].append(time.strftime("%H:%M:%S ") + f"Undid: {u.get('what', '')}")
            self.store.save(p)
            return
        p["image"], p["person_images"] = u.get("image") or p.get("image"), u.get("person_images") or p.get("person_images")
        for c, old in zip(p["clips"], u["clips"]):
            c["image"], c["video"], c["status"], c["edits"] = old.get("image"), old.get("video"), "done", old.get("edits") or []
        p["log"].append(time.strftime("%H:%M:%S ") + f"Undid: {u.get('what', '')}")
        self.store.save(p)
        self._edit(pid)

    def _undo_clip(self, pid, i: int):
        p = self.store.load(pid)
        c = p["clips"][i]
        if not c.get("history"):
            return
        h = c["history"].pop()
        c["image"], c["video"], c["status"] = h.get("image"), h.get("video"), "done"
        c["edits"] = (c.get("edits") or [])[:-1]
        p["log"].append(time.strftime("%H:%M:%S ") + f"Shot {i + 1}: undid {h.get('what', '')}")
        self.store.save(p)
        self._edit(pid)

    def undo_still(self, pid, i: int) -> dict:
        """Instant (no re-making): bring back the picture from before the last change."""
        p = self.store.load(pid)
        s = p["stills"][i]
        if not s.get("history"):
            return p
        h = s["history"].pop()
        if p.get("anchor") == s.get("image"):
            p["anchor"] = h["image"]
        s["image"] = h["image"]
        s["edits"] = (s.get("edits") or [])[:-1]
        p["log"].append(time.strftime("%H:%M:%S ") + f"Image {i + 1}: undid {h.get('what', '')}")
        self.store.save(p)
        return p

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
        self._talk(pid)
        self._edit(pid)
