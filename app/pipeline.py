"""One-click pipeline: (reference) -> plan -> images -> videos -> voice -> edit -> final MP4.
Every project lives in its own folder, so nothing is ever mixed between projects."""
from __future__ import annotations

import json
import queue
import random
import re
import subprocess
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


def load_brand() -> dict:
    """The brand kit saved once under ⚙ (logo, colours, font): used by Performance ads."""
    from . import addons
    f = addons.GHOST / "brand" / "brand.json"
    try:
        b = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        b = {}
    for k in ("logo", "font"):
        if b.get(k) and not Path(b[k]).exists():
            b.pop(k)
    return b


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
                if pid in self.cancel or not (self.store.dir(pid) / "project.json").exists():
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
        if p.get("kind") == "perf":
            return self._perf(pid)
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
                          language=p.get("language"), aspect=p.get("aspect") or "9:16", style=self._style(p, ref),
                          fast=p.get("quality") == "draft", cast=p.get("cast", "auto"),
                          photo=self.eng.plan()["video"] != "service")
        p["plan"] = {k: v for k, v in pl.items() if k != "clips"}
        p["title"] = p.get("title") if p.get("title") not in (None, "", "New ad") else pl.get("title") or "New ad"
        p["clips"] = [{**c, "status": "waiting", "seed": random.randint(1, 10**6)} for c in pl.get("clips", [])]
        if pl.get("note"):
            p["log"].append(pl["note"])
        self._done(p, "plan")
        if self._review_stop(p):
            return
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

    # ------------------------------------------------------------ Performance ad mode (see perf.py)
    def _perf(self, pid):
        from . import llm, perf
        p = self.store.load(pid)
        d = self.store.dir(pid)
        pf = p.setdefault("perf", {})
        lang = p.get("language") or self.cfg.get("language", "Hindi")
        fast = llm.fast_model(self.cfg["models"]["llm"]) if llm.available() else ""

        def ask(prompt, fallback):
            if not llm.available():
                return fallback()
            try:
                r = llm.ask_json(fast or self.cfg["models"]["llm"], prompt, temperature=0.5)
                return r if r else fallback()
            except Exception as e:
                p["log"].append(time.strftime("%H:%M:%S ") + f"Script AI unavailable ({str(e)[:80]}), using the simple writer")
                return fallback()
        # 1 brief: read every input once
        if not pf.get("brief"):
            self._say(p, "analyse", "1 · Reading your brief" + (" and landing page" if p.get("url") else ""), 3)
            page = ""
            if p.get("url"):
                try:
                    page = perf.fetch_page(p["url"])
                except Exception as e:
                    p["log"].append(time.strftime("%H:%M:%S ") + f"Could not open the landing page ({str(e)[:80]}); using your text only")
            source = "\n".join(x for x in (p.get("idea", ""), p.get("dialogue", ""), page, p.get("notes", "")) if x)
            b = ask(perf.brief_prompt(p.get("idea", ""), page + "\n" + p.get("notes", ""), lang, p.get("platform", "both")),
                    lambda: perf.fallback_brief(p.get("idea", ""), page, lang))
            if not isinstance(b.get("product"), dict):
                b = {**perf.fallback_brief(p.get("idea", ""), page, lang), **{k: v for k, v in b.items() if k in ("claims", "cta")}}
            b = perf.finish_brief(b, source, {"offer": p.get("offer", ""), "cta": p.get("cta", ""), "name": p.get("product", ""),
                                              "disclaimer": p.get("disclaimer", ""), "platform": p.get("platform", "both")})
            pf["brief"], pf["source_chars"] = b, len(source)
            miss = [m for m in (b.get("missing") or []) if m][:3]
            if miss:
                p["log"].append(time.strftime("%H:%M:%S ") + "Assumed (add them to the brief next time): " + "; ".join(map(str, miss)))
            blocked = [c["text"] for c in b["claims"] if c["status"] == "blocked"]
            p["log"].append(time.strftime("%H:%M:%S ") + f"Brief: {len(b['claims']) - len(blocked)} approved claim(s)"
                            + (f", {len(blocked)} blocked (not in your material): " + " · ".join(blocked[:4]) if blocked else ""))
            p["title"] = p.get("title") if p.get("title") not in (None, "", "New ad") else (b["product"].get("name") or "Performance ad")
            self._done(p, "analyse")
        b = pf["brief"]
        # 2 ideas: 4 angles, scored, the best 2 go on
        if not pf.get("angles"):
            self._say(p, "plan", "2 · Writing 4 ad ideas and scoring them", 8)
            got = ask(perf.ideas_prompt(b, lang), lambda: perf.fallback_ideas(b)).get("angles") or []
            have = {a.get("kind") for a in got}
            got += [a for a in perf.fallback_ideas(b)["angles"] if a["kind"] not in have]
            pf["angles"] = perf.rank([a for a in got if a.get("kind") in perf.ANGLES][:4])
            self.store.save(p)
        # 3 script: 15 s on the beat, for the top 2
        if not pf.get("variants"):
            self._say(p, "plan", "3 · Writing the 15-second scripts", 12)
            top = pf["angles"][:1] if p.get("dialogue") else pf["angles"][:2]
            pf["variants"] = []
            for n, a in enumerate(top, 1):
                if p.get("dialogue"):  # a finished script: its lines become the voiceover on the beat
                    parts = planner.split_lines(p["dialogue"], 9)
                    s = perf.fallback_script(b, a)
                    for sh, ln in zip(s["shots"], parts):
                        sh["voiceover"] = ln.get("line", "")
                else:
                    s = ask(perf.script_prompt(b, a, lang), lambda: perf.fallback_script(b, a))
                sc = perf.clean_script(s, b)
                a["scores"]["product_on_screen"] = min(a["scores"]["product_on_screen"], round(5 * min(1.0, sc["product_share"] / 0.6)))
                pf["variants"].append({"n": n, "kind": a["kind"], "angle": perf.ANGLE_NAME[a["kind"]], "hook": a.get("hook"), "script": sc})
            self._done(p, "plan")
        # 4 + 5 pictures and video: each variant is made like an ad of its own (inside this project's folder)
        refs = [Path(r) for r in p.get("refs") or []]
        for v in pf["variants"]:
            if v.get("child"):
                continue
            cid = f"{pid}/v{v['n']}"
            shots = v["script"]["shots"]
            look = " The product looks exactly as in the reference photo." if refs else ""
            child = {"id": cid, "parent": pid, "kind": "video", "title": v["angle"], "idea": p.get("idea", ""), "aspect": "9:16",
                     "quality": p.get("quality", "final"), "language": lang, "voice_mode": "narrator", "ai_music": p.get("ai_music", True),
                     "voice_gender": p.get("voice_gender", "female"), "voice_style": p.get("voice_style", "excited"),
                     "refs": [str(r) for r in refs], "status": "running", "log": [], "steps": {s: "waiting" for s in STEPS},
                     "plan": {"image_prompt": shots[2]["image_prompt"] + look, "people": []}, "seconds": 15,
                     "clips": [{"speaker": "N", "line": sh["voiceover"], "narration": sh["voiceover"], "seconds": float(sh["end"] - sh["start"]),
                                "image_prompt": sh["image_prompt"] + (look if sh["product_visible"] else "") + " No text, no logos, no price tags.",
                                "video_prompt": (sh["motion"] or "natural movement") + ". No text, no logos.", "camera_move": "push_in",
                                "shake": 0.2, "status": "waiting", "seed": random.randint(1, 10**6)} for sh in shots]}
            child["steps"]["analyse"] = child["steps"]["plan"] = "done"
            self.store.save(child)
            v["child"] = cid
            self.store.save(p)
        for v in pf["variants"]:
            cid = v["child"]
            c = self.store.load(cid)
            if c.get("final") and Path(c["final"]).exists():
                continue
            self._say(p, "images", f"4 · Key pictures for idea {v['n']}: {v['angle']}", 20 + 20 * (v["n"] - 1))
            if not c.get("image") or not Path(c["image"]).exists():
                self._images(cid)
            c = self.store.load(cid)
            (gw, gh), _, draft = sizes(c)
            k = 1 if draft else 2
            q = self.cfg["quality"]
            steps_ = int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"])
            for i, cl in enumerate(c["clips"]):  # one still per shot: static ads + the first frame of each video
                if not cl.get("image") or not Path(cl["image"]).exists():
                    self._shot_image(c, i, gw * k, gh * k, steps_, refs, Path(c["image"]))
                    self.store.save(c)
            self._done(p, "images")
            self._say(p, "videos", f"5 · Video for idea {v['n']}", 45 + 10 * (v["n"] - 1))
            self._videos(cid, resume=True)
            self._perf_review(p, v)
            self._say(p, "voice", f"Voice and music for idea {v['n']}", 65)
            self._voice(cid)
            self._talk(cid)
            self._music_for_ad(cid)
            self._edit(cid)
        self._done(p, "videos")
        self._done(p, "voice")
        p["steps"]["music"] = "done"
        # 6 finishing + 7 check: by script, exact
        self._perf_finish(pid)

    def _perf_review(self, p, v):
        """Each shot: hard cuts inside it, and (with the vision model) one frame a second for wrong product,
        warped hands/faces or garbled text. A failed shot is re-made once; everything else is kept."""
        from . import perf, qc
        cid = v["child"]
        c = self.store.load(cid)
        moving = (c.get("engine_plan") or {}).get("video") != "photo_motion"
        vision = qc.available()
        log = v.setdefault("review", [])
        for i, cl in enumerate(c["clips"]):
            if not cl.get("video") or not Path(cl["video"]).exists():
                continue
            probs = []
            if moving and perf.scene_cuts(Path(cl["video"])) > 0:
                probs.append("a cut inside the shot")
            if vision:
                frames = perf.frames_1fps(Path(cl["video"]), self.store.dir(cid) / f"qc{i + 1:02d}") if moving else [Path(cl["image"])]
                for f in frames:
                    self._say(p, "videos", f"5 · Checking idea {v['n']}, shot {i + 1} ({f.stem})")
                    r = qc.check(f)
                    if not r.get("ok"):
                        probs.append(r.get("problem") or "defect")
                        break
            if probs and not cl.get("redone"):
                log.append({"shot": i + 1, "problems": probs, "action": "re-made"})
                self._say(p, "videos", f"5 · Shot {i + 1} of idea {v['n']}: {probs[0]} — re-making only this shot")
                cl["seed"], cl["redone"], cl["status"] = random.randint(1, 10**6), True, "waiting"
                if not moving:
                    (gw, gh), _, draft = sizes(c)
                    q = self.cfg["quality"]
                    self._shot_image(c, i, gw * (1 if draft else 2), gh * (1 if draft else 2),
                                     int(q.get("image_steps_draft", 20)) if draft else int(q["image_steps"]),
                                     [Path(r) for r in c.get("refs") or []], Path(c["image"]))
                self.store.save(c)
                self._videos(cid, only=i)
                c = self.store.load(cid)
            else:
                log.append({"shot": i + 1, "problems": probs, "action": "kept" if not probs else "kept (already re-made once)"})
        self.store.save(p)

    def _perf_finish(self, pid):
        from . import addons, perf
        p = self.store.load(pid)
        d = self.store.dir(pid)
        pf = p["perf"]
        b = pf["brief"]
        brand = load_brand()
        logo = brand.get("logo")
        fonts = addons.GHOST / "fonts"
        out_dir = d / "deliver"
        out_dir.mkdir(exist_ok=True)
        outputs = []
        fmts = list(perf.FORMATS)
        total = len(pf["variants"]) * len(fmts) * 2
        k = 0
        for v in pf["variants"]:
            c = self.store.load(v["child"])
            master = Path(c["final"])
            tag = f"Ad{v['n']}_{v['kind']}"
            for fmt in fmts:
                k += 1
                self._say(p, "edit", f"6 · Logo, text, end slate and size {fmt} for idea {v['n']}", 80 + int(15 * k / total))
                f = out_dir / f"{tag}_{fmt.replace(':', 'x')}.mp4"
                rec = perf.finish_video(master, f, fmt, v["script"], b, brand, logo, fonts, d / "work")
                outputs.append({"file": str(f), "fmt": fmt, "type": "video", "variant": v["n"], "rec": rec,
                                "problems": perf.check_output(f, fmt, rec, b, True, brand)})
            picks = [(0, v["script"]["shots"][0]["on_screen"] or b["product"].get("name") or v.get("hook") or ""),
                     (2, v["script"]["shots"][2]["on_screen"] or b["product"].get("name") or "")]
            for si, headline in picks:
                img = c["clips"][si].get("image")
                if not img or not Path(img).exists():
                    continue
                for fmt in fmts:
                    k += 1
                    f = out_dir / f"{tag}_still{si + 1}_{fmt.replace(':', 'x')}.jpg"
                    rec = perf.finish_still(Path(img), f, fmt, headline, b, brand, logo, fonts)
                    outputs.append({"file": str(f), "fmt": fmt, "type": "image", "variant": v["n"], "rec": rec,
                                    "problems": perf.check_output(f, fmt, rec, b, False, brand)})
        self._done(p, "edit")
        bad = [o for o in outputs if o["problems"]]
        pf["outputs"] = [{k2: v2 for k2, v2 in o.items() if k2 != "rec"} for o in outputs]
        pf["checked"] = {"passed": len(outputs) - len(bad), "failed": len(bad), "at": time.time()}
        p["final"] = next((o["file"] for o in outputs if o["type"] == "video" and o["fmt"] == "9:16" and not o["problems"]),
                          next((o["file"] for o in outputs if o["type"] == "video"), None))
        p["log"].append(time.strftime("%H:%M:%S ") + f"7 · Final check: {len(outputs) - len(bad)} of {len(outputs)} files passed"
                        + ("" if not bad else " — problems: " + "; ".join(f"{Path(o['file']).name}: {o['problems'][0]}" for o in bad[:4])))
        p["status"], p["progress"] = "done", 100
        self.store.save(p)

    def _review_stop(self, p) -> bool:
        """"Check prompts first": stop after writing so you can edit, then Continue makes it."""
        if not p.get("review") or p.get("reviewed"):
            return False
        p["reviewed"] = True
        p["status"] = "review"
        p["log"].append(time.strftime("%H:%M:%S ") + "Ready for your check: edit anything, then press Make it.")
        self.store.save(p)
        return True

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
        prompt = c.get("image_prompt") or f"{p['plan']['image_prompt']} Moment: {c.get('line', '')}"
        if i > 0:  # every shot gets a clearly different framing, so the ad cuts instead of repeating one picture
            prompt = f"{self.FRAMINGS[i % len(self.FRAMINGS)]}. {prompt}"
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
            self._say(p, "videos", f"Making {len(todo)} shots at the same time in the cloud (about 1–2 min)", 40)

            def mk(i):
                c = p["clips"][i]
                start = c.get("image") or p.get("person_images", {}).get(c.get("speaker")) or p["image"]
                out = d / f"clip{i + 1:02d}_{c['seed']}{'_r%d' % c['rev'] if c.get('rev') else ''}{'_draft' if draft else ''}.mp4"
                self.eng.video(Path(start), c["video_prompt"], float(c["seconds"]), out, seed=c["seed"],
                               width=gw, height=gh, steps=steps, aspect=p.get("aspect") or "9:16")
                return i, out
            with ThreadPoolExecutor(max_workers=4) as ex:
                for i, out in ex.map(mk, todo):
                    p["clips"][i]["video"], p["clips"][i]["status"] = str(out), "done"
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
            self.eng.video(Path(start), c["video_prompt"], float(c["seconds"]), out, seed=c["seed"],
                           width=gw, height=gh, steps=steps, aspect=p.get("aspect") or "9:16")
            c["video"], c["status"] = str(out), "done"
            self.store.save(p)
        if only is None:
            self._done(p, "videos")

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
        if not self.eng.talks and mode != "narrator":
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
        pl = planner.plan_music(self.cfg, idea=p.get("idea", ""), style=p.get("music_style", "bollywood"), vocals=p.get("vocals", "ai"),
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
        if p.get("kind") == "images" and p.get("stills"):
            return self._stills(pid, resume=True)
        if p.get("kind") == "music":
            return self._compose(pid, takes=1) if p.get("song") else self._all(pid)
        if p.get("kind") == "dub":
            return self._dub_finish(pid) if (p.get("dub") or {}).get("segments") else self._dub(pid)
        if p.get("kind") == "perf":
            return self._perf(pid)
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
        pl = planner.plan_images(self.cfg, idea=p.get("idea", "") or p.get("dialogue", ""), reference=ref,
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
