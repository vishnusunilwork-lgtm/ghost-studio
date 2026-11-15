"""Live evaluation of Talk to Ghost with the REAL local models (needs Ollama running).
Each case: what a person types -> which tool(s) Ghost must call. Prints accuracy and time per case.
Run:  python3 -m tests.eval_live            (uses whatever qwen/llama models are installed)
Gated actions count as correct when they are proposed for OK (nothing runs)."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from app import llm
from app.agent import context, orchestrator, router

_real = llm.text_models
from tests.test_agent import P, Q, FakeBackend  # noqa: E402  (this import fakes the model list for unit tests)

router.llm.text_models = _real

context.CHATS = Path(tempfile.mkdtemp()) / "chats"
orchestrator.TRACE = Path(tempfile.mkdtemp()) / "agent.log"

V = {"id": "p3", "kind": "video", "title": "Saree sale", "idea": "Onam saree sale in Kochi", "status": "done",
     "clips": [{"line": "Onam offer!"}, {"line": "Visit today"}, {"line": "Kochi's best sarees"}]}

CASES = [  # (project, text, expected tool names in order, any of)
    ("p1", "make the dog in picture 2 look older", ["change_picture"]),
    ("p1", "picture 1 ki background ko beach bana do", ["change_picture"]),
    ("p1", "I don't like the second one, give me a completely different one", ["retake_picture"]),
    ("p1", "use the first picture as the main look", ["set_main_look"]),
    ("p1", "make 4 more pictures like this but at Diwali", ["create_project"]),
    ("p1", "delete this project", ["delete_project"]),
    ("p3", "in shot 2 make her saree blue", ["change_shot"]),
    ("p3", "shot 3 again please", ["retake_shot"]),
    ("p3", "change the line in shot 1 to say it in Malayalam", ["rewrite_text"]),
    ("p2", "take 1 but with a male voice and more upbeat", ["change_song"]),
    ("p2", "rewrite the lyrics to be shorter", ["rewrite_text"]),
    (None, "remember that our shop means a small kirana store in Kochi", ["remember"]),
    (None, "open my onam song", ["open_project"]),  # found through search of earlier projects
    ("p2", "make the song happier and then make two more takes", ["change_song"]),
]


class Backend(FakeBackend):
    def project(self, pid):
        return {"p1": P, "p2": Q, "p3": V}.get(pid)

    def projects(self):
        return [P, Q, V]

    def list_projects(self, query="", limit=8):
        self.done.append(("list_projects", {"query": query}))
        return {"text": "; ".join(f"{p['id']}: {p['kind']} “{p['title']}”" for p in context.search(self.projects(), query or "x") or self.projects())}


def main():
    ok, rows = 0, []
    for n, (pid, text, want) in enumerate(CASES):
        b = Backend()
        a = orchestrator.Agent({"models": {"llm": "qwen2.5:14b"}}, b)
        ev, t0 = [], time.time()
        a.turn(f"e{n}", text, pid, ev.append)
        got = [d[0] for d in b.done] + [p for e in ev if e["t"] == "confirm" for p in ["(ok?)"]]
        proposed = [c[0] for c in (context.load(f"e{n}").get("pending") or {}).get("calls", [])]
        names = [d[0] for d in b.done] + proposed
        good = all(w in names for w in want)
        ok += good
        route = next((f"tier{e['tier']} {e['model']}" for e in ev if e["t"] == "route"), "")
        rows.append(f"{'PASS' if good else 'FAIL'} {time.time() - t0:5.1f}s  {route:22} {text[:48]:48} -> {names or got}")
        print(rows[-1], flush=True)
    print(f"\n{ok}/{len(CASES)} correct")


if __name__ == "__main__":
    main()
