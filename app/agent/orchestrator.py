"""Orchestrator: the agent loop.

  person's message
    -> router (grammar / fast model / careful model)
    -> loop, at most MAX_STEPS:
         model sees context + tool schemas -> proposes tool calls
         each call: validate -> policy (run now, or hold for the person's OK) -> execute -> result back to the model
       until the model answers in words
    -> events streamed to the screen as they happen (route, each action, its real result, the answer)
    -> trace written to .ghost/agent.log (timings, model, calls, errors) for debugging and evaluation
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from .. import llm
from . import context, router, tools

MAX_STEPS = 6
TRACE = Path(__file__).resolve().parent.parent.parent / ".ghost" / "agent.log"


def _calls_from(msg: dict) -> list[tuple[str, dict]]:
    """Native tool calls; also accepts a model that wrote the call as JSON text (small models sometimes do)."""
    out = []
    for c in msg.get("tool_calls") or []:
        f = c.get("function") or {}
        a = f.get("arguments") or {}
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except Exception:
                a = {}
        if f.get("name"):
            out.append((f["name"], a))
    if not out:
        txt = (msg.get("content") or "").strip()
        for m in re.finditer(r"\{[^{}]*\"name\"\s*:\s*\"(\w+)\"[^{}]*\"arguments\"\s*:\s*(\{[^{}]*\})[^{}]*\}", txt):
            try:
                out.append((m.group(1), json.loads(m.group(2))))
            except Exception:
                pass
    return out


class Agent:
    def __init__(self, cfg: dict, backend, chat_fn: Callable | None = None, stream_fn: Callable | None = None):
        self.cfg, self.backend = cfg, backend
        self.chat = chat_fn or llm.chat  # injectable: tests use a scripted model
        self.stream = stream_fn or llm.chat_stream

    # ------------------------------------------------------------ one turn
    def turn(self, sid: str, text: str, project_id: str | None, emit: Callable[[dict], None], confirm: bool | None = None):
        t0, trace = time.time(), {"turn": uuid.uuid4().hex[:8], "sid": sid, "text": text[:300], "steps": []}
        s = context.load(sid)
        project = self.backend.project(project_id) if project_id else None
        text = (text or "").strip()
        try:
            if confirm is not None or (s.get("pending") and text):
                r = router.Route(0, confirm=confirm) if confirm is not None else router.grammar(text, project, True)
                if r and r.confirm is not None:
                    return self._answer_pending(s, r.confirm, project_id, emit, trace)
                if s.get("pending"):  # they said something else: the old plan is dropped
                    s["pending"] = None
                    emit({"t": "note", "text": "Earlier plan cancelled."})
            if not text:
                return
            s["messages"].append({"role": "user", "content": text})
            r = router.route(self.cfg, text, project, False)
            trace["route"] = {"tier": r.tier, "model": r.model, "why": r.why}
            emit({"t": "route", "tier": r.tier, "model": r.model, "why": r.why})
            if r.tier == 0:
                done = self._run_calls(s, r.calls, project_id, emit, trace)
                self._say(s, emit, done or "Done.")
            elif r.tier == 3:
                self._chat(s, r, project, emit, trace)
            elif r.tier == -1:
                self._say(s, emit, "I can do short commands like “retake 2”, “undo”, “pause” or “remember X means Y”. "
                                   "For anything else, install the free Script add-on (⚙ → Add-ons) and I’ll understand normal sentences.")
            else:
                self._loop(s, r, project_id, emit, trace)
        except Exception as e:
            trace["error"] = str(e)[:300]
            self._say(s, emit, "Sorry, something went wrong: " + str(e)[:200])
        finally:
            context.compact(s)
            context.save(s)
            trace["ms"] = int((time.time() - t0) * 1000)
            self._trace(trace)
            emit({"t": "done", "ms": trace["ms"]})

    # ------------------------------------------------------------ conversation (like ChatGPT): streamed, no tools
    def _chat(self, s, r, project, emit, trace):
        st, first, out = time.time(), None, []
        msgs = context.build_chat(s, project)
        for piece in self.stream(r.model, msgs):
            if first is None:
                first = time.time() - st
                emit({"t": "start"})
            out.append(piece)
            emit({"t": "delta", "text": piece})
        text = re.sub(r"<think>.*?</think>", "", "".join(out), flags=re.S).strip() or "…"
        trace["steps"].append({"model": r.model, "chat": True, "first_ms": int((first or 0) * 1000), "ms": int((time.time() - st) * 1000)})
        s["messages"].append({"role": "assistant", "content": text})
        emit({"t": "end", "text": text})

    # ------------------------------------------------------------ model loop
    def _loop(self, s, r, project_id, emit, trace):
        model, escalated, bad = r.model, False, 0
        for step in range(MAX_STEPS):
            project = self.backend.project(project_id) if project_id else None
            kind = (project or {}).get("kind") or ("video" if project else None)
            schemas = tools.schemas(kind)
            msgs = context.build(s, project, self.backend.projects(), s["messages"][-1]["content"] if s["messages"] else "")
            st = time.time()
            emit({"t": "thinking", "step": step + 1})
            try:
                msg = self.chat(model, msgs, schemas)
            except Exception as e:  # e.g. not enough memory to load the bigger model: fall back, don't fail
                fast, careful = router.pick_models(self.cfg)
                other = fast if model != fast else careful
                if not other or other == model:
                    raise
                trace["fallback"] = f"{model} failed ({str(e)[:80]}), used {other}"
                emit({"t": "route", "tier": 1 if other == fast else 2, "model": other, "why": "the other model could not load"})
                llm.unload()
                model = other
                msg = self.chat(model, msgs, schemas)
            calls = _calls_from(msg)
            trace["steps"].append({"model": model, "ms": int((time.time() - st) * 1000), "calls": [c[0] for c in calls]})
            if not calls:
                txt = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
                self._say(s, emit, txt or "Done.")
                return
            s["messages"].append({"role": "assistant", "content": msg.get("content") or "",
                                  "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]})
            held, acted = [], []
            for name, args in calls:
                name = tools.repair(name, args if isinstance(args, dict) else {}, kind)
                try:
                    a = tools.validate(name, args)
                except tools.ToolError as e:
                    bad += 1
                    s["messages"].append({"role": "tool", "content": f"{name}: ERROR {e}"})
                    continue
                t = tools.BY_NAME[name]
                asked = " ".join(m["content"] for m in s["messages"][-3:] if m["role"] == "user").lower()
                if name == "remember" and not re.search(r"remember|always|means|yaad|ഓർക്ക|याद", asked):
                    s["messages"].append({"role": "tool", "content": "remember: ERROR only save memories when the person asks you to remember something"})
                    continue  # never store a memory the person didn't ask for
                if t.needs_ok(a):
                    held.append((name, a))
                    s["messages"].append({"role": "tool", "content": f"{name}: waiting for the person's OK ({t.describe(a)})"})
                    continue
                res = self._exec(name, a, project_id, emit, trace)
                s["messages"].append({"role": "tool", "content": res})
                acted.append((t, a, res))
                if name == "open_project" and a.get("project_id"):
                    project_id = a["project_id"]
            if held:
                s["pending"] = {"calls": held, "project": project_id}
                emit({"t": "confirm", "plan": [tools.BY_NAME[n].describe(a) for n, a in held]})
                self._say(s, emit, "Shall I go ahead?" if len(held) == 1 else f"Shall I do these {len(held)} things?")
                return
            if (r.tier == 1 and acted and not bad and not escalated
                    and all(t.kind == "act" and t.name != "open_project" and " OK " in res for t, a, res in acted)):
                # early exit: every action ran fine and none needs reading back -> answer from the real results
                # (skips one whole model step, roughly halving the wait)
                trace["early_exit"] = True
                done = "; ".join(res.split(" OK ", 1)[1] for _, _, res in acted)
                self._say(s, emit, done + ("." if not done.endswith(".") else "") + " You’ll see it update in a moment.")
                return
            if bad >= 2 and not escalated:  # the fast model keeps getting it wrong: hand over to the careful one
                _, careful = router.pick_models(self.cfg)
                if careful and careful != model:
                    model, escalated = careful, True
                    trace["escalated"] = careful
                    emit({"t": "route", "tier": 2, "model": careful, "why": "switched to the careful model"})
        self._say(s, emit, "I stopped after several steps. Tell me what is still missing.")

    # ------------------------------------------------------------ actions
    def _exec(self, name, a, project_id, emit, trace) -> str:
        t = tools.BY_NAME[name]
        if t.kind != "read":
            emit({"t": "action", "name": name, "label": t.describe(a)})
        st = time.time()
        try:
            res = tools.run(self.backend, name, a, project_id)
            ok, text = True, res.get("text", "done")
        except tools.ToolError as e:
            res, ok, text = {}, False, str(e)
        except Exception as e:  # the app refused (e.g. HTTP 409 busy) — report truthfully
            res, ok, text = {}, False, getattr(e, "detail", None) or str(e)
        trace["steps"].append({"tool": name, "args": a, "ok": ok, "ms": int((time.time() - st) * 1000)})
        if t.kind != "read":
            emit({"t": "result", "name": name, "ok": ok, "text": text})
        if res.get("open"):
            emit({"t": "open", "project": res["open"]})
        if res.get("refresh"):
            emit({"t": "refresh", "project": res["refresh"]})
        return f"{name}: {'OK' if ok else 'ERROR'} {text}"[:1500]

    def _run_calls(self, s, calls, project_id, emit, trace) -> str:
        outs = []
        for name, args in calls:
            try:
                a = tools.validate(name, args)
            except tools.ToolError as e:
                outs.append(str(e))
                continue
            if tools.BY_NAME[name].needs_ok(a):
                s["pending"] = {"calls": [(name, a)], "project": project_id}
                emit({"t": "confirm", "plan": [tools.BY_NAME[name].describe(a)]})
                return "Shall I go ahead?"
            r = self._exec(name, a, project_id, emit, trace)
            s["messages"].append({"role": "tool", "content": r})
            outs.append(r.split(" ", 2)[-1] if " OK " in r else r.split("ERROR", 1)[-1].strip())
        return " ".join(o for o in outs if o)[:400]

    def _answer_pending(self, s, yes: bool, project_id, emit, trace):
        p = s.get("pending")
        s["pending"] = None
        if not p:
            self._say(s, emit, "There is nothing waiting for your OK.")
            return
        if not yes:
            s["messages"].append({"role": "tool", "content": "The person said no; nothing was done."})
            self._say(s, emit, "OK, I won't do it.")
            return
        s["messages"].append({"role": "user", "content": "Yes, go ahead."})
        outs = [self._exec(n, a, p.get("project") or project_id, emit, trace) for n, a in p["calls"]]
        for o in outs:
            s["messages"].append({"role": "tool", "content": o})
        ok = all(" OK " in o for o in outs)
        self._say(s, emit, "Started. You can keep talking to me while it works." if ok else "Some of it could not be done — see above.")

    def _say(self, s, emit, text):
        s["messages"].append({"role": "assistant", "content": text})
        emit({"t": "message", "text": text})

    def _trace(self, tr):
        try:
            TRACE.parent.mkdir(parents=True, exist_ok=True)
            with TRACE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **tr}, ensure_ascii=False) + "\n")
            if TRACE.stat().st_size > 2_000_000:
                TRACE.write_text("\n".join(TRACE.read_text(encoding="utf-8").splitlines()[-2000:]) + "\n", encoding="utf-8")
        except Exception:
            pass
