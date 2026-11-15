"""Offline check: deleting a project while it is being made stops the job and the folder never comes back.
Run:  python3 -m tests.test_delete"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

from app import pipeline
from app import engines as E
from tests.test_handoff import runner


def test_delete_while_running():
    r = runner(Path(tempfile.mkdtemp()))
    p = r.store.new(kind="video", idea="x", quality="draft", aspect="9:16", seconds=8, refs=[])
    pid = p["id"]
    state = {"started": False, "stopped": False}

    def _slow(pid_):  # stands in for a long cloud video: waits, checking Pause/Delete like the Veo wait does
        q = r.store.load(pid_)
        state["started"] = True
        try:
            for _ in range(200):
                time.sleep(0.05)
                E.check_stop()
        except E.Cancelled:
            state["stopped"] = True
            r.store.save(q)  # a late save after delete must not bring the folder back
            (r.store.dir(pid_)).mkdir(parents=True, exist_ok=True)  # nor any file written meanwhile
            raise
    r._slow = _slow
    threading.Thread(target=r._loop, daemon=True).start()
    r.submit(pid, "slow")
    while not state["started"]:
        time.sleep(0.02)
    r.forget(pid)  # what the Delete button does, then the folder is removed
    shutil.rmtree(r.store.dir(pid), ignore_errors=True)
    for _ in range(100):
        if r.current is None and state["stopped"]:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    assert state["stopped"] and r.current is None
    assert not r.store.dir(pid).exists()
    assert pid not in [x["id"] for x in r.store.list()]


if __name__ == "__main__":
    test_delete_while_running()
    print("ok delete")
