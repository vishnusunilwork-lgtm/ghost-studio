"""Offline check of the brain chain: switch on limits, keep the conversation, finish a cut-off answer, private mode.
Three stand-in OpenAI-compatible services run on this computer. Run:  python3 -m tests.test_brains"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import llm

SEEN: list = []


def service(mode: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self._json(200, {"data": [{"id": "m-" + mode}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            SEEN.append((mode, body))
            if mode == "daily":
                return self._json(429, {"error": {"message": "Rate limit reached: requests per day (RPD)"}})
            if mode == "minute":
                self.send_response(429)
                self.send_header("Retry-After", "20")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"slow down"}}')
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                words = ["Thiru", "vanan"] if mode == "cut" else ["thapuram."] if any("cut off" in m["content"] for m in body["messages"]) else ["Kochi."]
                for w in words:
                    self.wfile.write(f'data: {json.dumps({"choices": [{"delta": {"content": w}}]})}\n\n'.encode())
                    self.wfile.flush()
                if mode == "cut":
                    self.connection.shutdown(2)  # the service drops mid-answer
                    return
                self.wfile.write(b"data: [DONE]\n\n")
                return
            last = body["messages"][-1]["content"]
            self._json(200, {"choices": [{"message": {"content": '{"ok": true, "who": "' + mode + '", "saw": ' + json.dumps(last[:20]) + "}"}}]})

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}/v1"


def brain(i, mode, url):
    return {"id": f"b{i}", "name": mode, "base": url, "key": "k" * 20, "model": "m-" + mode}


def test_chain():
    urls = {m: service(m) for m in ("daily", "minute", "good", "cut")}
    llm.STATE.clear()
    llm.PRIVATE["on"] = False
    llm.CHAIN[:] = [brain(1, "daily", urls["daily"]), brain(2, "minute", urls["minute"]), brain(3, "good", urls["good"])]
    d = llm.ask_json("x", "hello ghost")
    assert d["who"] == "good" and d["saw"] == "hello ghost"
    st = {e["name"]: llm._st(e) for e in llm.CHAIN}
    assert st["daily"]["cool_until"] > time.time() + 3600 and "daily" in st["daily"]["why"]
    assert 10 < st["minute"]["cool_until"] - time.time() <= 21
    SEEN.clear()
    llm.ask_json("x", "again")
    assert [m for m, _ in SEEN] == ["good"]  # resting brains aren't asked again until they're back
    assert llm.CLOUD["name"] == "good" and llm._st(llm.CHAIN[2])["n"] == 2

    # an answer cut off mid-way is finished by the next brain, which sees what was already said
    llm.STATE.clear()
    llm.CHAIN[:] = [brain(4, "cut", urls["cut"]), brain(5, "good", urls["good"])]
    out = "".join(llm.chat_stream("cloud:m-cut", [{"role": "user", "content": "capital of Kerala?"}]))
    assert out == "Thiruvananthapuram.", out
    handoff = SEEN[-1][1]["messages"]
    assert handoff[-2] == {"role": "assistant", "content": "Thiruvanan"} and "cut off" in handoff[-1]["content"]

    # private: no cloud at all
    llm.PRIVATE["on"] = True
    assert not llm.cloud_on() and llm.text_models()[:1] != ["cloud:m-good"]
    llm.PRIVATE["on"] = False

    # every cloud brain resting and no local brain: a clear message, not a crash
    llm.STATE.clear()
    llm.CHAIN[:] = [brain(6, "daily", urls["daily"])]
    llm.installed = lambda: []
    try:
        llm.ask_json("x", "hi")
        raise AssertionError("should explain")
    except llm.CloudDown as e:
        assert "Ollama" in str(e)


if __name__ == "__main__":
    test_chain()
    print("ok brains")
