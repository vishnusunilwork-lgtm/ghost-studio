"""Offline checks for Talk to Ghost: router, tool validation, permission gate, context, retrieval, agent loop.
Run:  python3 -m tests.test_agent      (no models or internet needed: the model is scripted)"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.agent import context, orchestrator, router, tools

context.CHATS = Path(tempfile.mkdtemp()) / "chats"
orchestrator.TRACE = Path(tempfile.mkdtemp()) / "agent.log"
router.llm.text_models = lambda: ["qwen2.5:3b", "qwen2.5:14b"]

P = {"id": "p1", "kind": "images", "title": "Desi dog", "idea": "desi dog on a Kochi street", "status": "done", "quality": "draft",
     "stills": [{"caption": "Close-up", "prompt": "x"}, {"caption": "Wide", "prompt": "y"}], "anchor_i": 0, "undo": []}
Q = {"id": "p2", "kind": "music", "title": "Onam song", "idea": "happy Onam song for family", "status": "done",
     "tracks": [{"n": 1}], "song": {"caption": "festive", "lyrics": "la"}}


class FakeBackend:
    def __init__(self):
        self.done = []

    def project(self, pid):
        return {"p1": P, "p2": Q}.get(pid)

    def projects(self):
        return [P, Q]

    def __getattr__(self, name):
        def act(**kw):
            self.done.append((name, kw))
            if name == "change_picture" and kw["index"] > 2:
                raise tools.ToolError("There are only 2 pictures (1–2)")
            return {"text": f"{name} ok", "refresh": kw.get("project_id")}
        return act


def scripted(*replies):
    it = iter(replies)
    seen = []

    def chat(model, msgs, schemas):
        seen.append((model, msgs))
        return next(it)
    chat.seen = seen
    return chat


def call(name, **a):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": a}}]}


def say(t):
    return {"content": t}


def run(agent, text, pid="p1", confirm=None, sid="t"):
    ev = []
    agent.turn(sid, text, pid, ev.append, confirm)
    return ev


def test_grammar():
    g = lambda t, pending=False: router.grammar(t, P, pending)  # noqa: E731
    assert g("retake 2").calls == [("retake_picture", {"index": 2, "fresh": False})]
    assert g("new take of picture three").calls == [("retake_picture", {"index": 3, "fresh": True})]
    assert g("retake last").calls[0][1]["index"] == 2
    assert g("pause").calls == [("pause", {})]
    assert g("make 2 the main look").calls == [("set_main_look", {"index": 2})]
    assert g("remember our dog means a ginger street dog").calls[0][1] == {"kind": "means", "text": "our dog", "means": "a ginger street dog"}
    assert g("yes", True).confirm is True and g("haan", True).confirm is True and g("no", True).confirm is False
    assert g("yes") is None  # nothing pending: not a command
    assert g("make her hair shorter in picture 2") is None  # needs understanding: goes to a model


def test_router_tiers():
    cfg = {"models": {"llm": "qwen2.5:14b"}}
    assert router.route(cfg, "shorter hair on picture 2", P, False).model == "qwen2.5:3b"
    r = router.route(cfg, "make the dog sit on the step and then give me three more like it, but warmer light", P, False)
    assert r.tier == 2 and r.model == "qwen2.5:14b"


def test_validate():
    assert tools.validate("change_picture", {"index": "2", "instruction": " shorter  hair "}) == {"index": 2, "instruction": "shorter hair"}
    for bad in ({"index": 0, "instruction": "x"}, {"instruction": "x"}):
        try:
            tools.validate("change_picture", bad)
            raise AssertionError("should fail")
        except tools.ToolError:
            pass
    try:
        tools.validate("delete_everything", {})
        raise AssertionError
    except tools.ToolError:
        pass
    assert tools.BY_NAME["change_picture"].needs_ok({"all": True}) and not tools.BY_NAME["change_picture"].needs_ok({})


def test_retrieval_and_context():
    assert context.search([P, Q], "onam family song")[0]["id"] == "p2"
    s = {"id": "x", "messages": [{"role": "user", "content": "hi " * 3000}] + [{"role": "user", "content": "latest"}], "summary": []}
    msgs = context.build(s, P, [P, Q], "latest")
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == context.SYSTEM  # static: cacheable
    assert "picture 2" in msgs[-1]["content"] and msgs[-1]["content"].endswith("latest")
    assert sum(len(m["content"]) for m in msgs) < context.BUDGET + 4000


def test_agent_loop_acts_and_reports():
    b = FakeBackend()
    chat = scripted(call("change_picture", index=2, instruction="shorter hair"), say("Done, picture 2 is getting shorter hair."))
    a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b, chat)
    ev = run(a, "picture 2 shorter hair")
    assert b.done == [("change_picture", {"index": 2, "instruction": "shorter hair", "project_id": "p1"})]
    assert [e["t"] for e in ev if e["t"] in ("action", "result", "message")] == ["action", "result", "message"]
    assert len(chat.seen) == 1  # early exit: the answer comes from the real result, no second model step


def test_read_then_act_continues():
    b = FakeBackend()
    chat = scripted(call("list_projects", query="onam"), call("open_project", project_id="p2"), say("Opened your Onam song."))
    a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b, chat)
    ev = run(a, "open my onam song", sid="t6")
    assert [d[0] for d in b.done] == ["list_projects", "open_project"] and ev[-2]["text"] == "Opened your Onam song."
    # the list result went back to the model before it acted
    assert any(m["role"] == "tool" and "list_projects: OK" in m["content"] for m in chat.seen[1][1])


def test_repair_and_kind_tools():
    names = {s["function"]["name"] for s in tools.schemas("images")}
    assert "change_picture" in names and "change_shot" not in names and "change_song" not in names
    b = FakeBackend()
    a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b, scripted(call("change_shot", index=1, instruction="sit")))
    run(a, "make the dog in 1 sit", sid="t7")
    assert b.done[0][0] == "change_picture"


def test_error_goes_back_to_model():
    b = FakeBackend()
    chat = scripted(call("change_picture", index=5, instruction="x"), call("change_picture", index=2, instruction="x"))
    a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b, chat)
    ev = run(a, "change picture five", sid="t2")
    assert [d[1]["index"] for d in b.done] == [5, 2]
    assert any(e["t"] == "result" and not e["ok"] for e in ev)


def test_gate_waits_for_ok():
    b = FakeBackend()
    chat = scripted(call("create_project", kind="images", idea="desi dog at Diwali", count=3), say("unused"))
    a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b, chat)
    ev = run(a, "same dog but for diwali, three pictures", sid="t3")
    assert not b.done and any(e["t"] == "confirm" for e in ev)
    ev = run(a, "yes", sid="t3")
    assert b.done and b.done[0][0] == "create_project"
    b2 = FakeBackend()
    a2 = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b2, scripted(call("delete_project", project_id="p1")))
    run(a2, "delete this", sid="t4")
    run(a2, "no", sid="t4")
    assert not b2.done


def test_tier0_needs_no_model():
    b = FakeBackend()
    a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b, scripted())  # any model call would raise StopIteration
    run(a, "retake 1", sid="t5")
    assert b.done == [("retake_picture", {"index": 1, "fresh": False, "project_id": "p1"})]


def test_music_tools():
    assert tools.validate("replace_song_part", {"take": "1", "start": "30s", "end": 45}) == {"take": 1, "start": 30.0, "end": 45.0}
    try:
        tools.validate("replace_song_part", {"take": 1, "start": 30, "end": 31})
        raise AssertionError
    except tools.ToolError:
        pass
    names = {s["function"]["name"] for s in tools.schemas("music")}
    assert {"replace_song_part", "split_song_stems", "change_song"} <= names and "change_picture" not in names


def test_json_text_tool_call():
    m = {"content": 'Sure {"name": "pause", "arguments": {}}'}
    assert orchestrator._calls_from(m) == [("pause", {})]


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            n += 1
            print("ok ", k)
    print(f"{n} checks passed")
