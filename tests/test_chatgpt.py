"""Offline check of Continue with ChatGPT: the sign-in (PKCE, state, issued client id, nonce), token refresh, and the
ChatGPT plan as a brain over the Responses API. Run:  python3 -m tests.test_chatgpt"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app import chatgpt_auth as C
from app import llm

SEEN: list = []


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".sig"


def server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith("/models"):
                return self._json({"models": [{"slug": "gpt-6.1-mini", "visibility": "list"}, {"slug": "gpt-6.1-sol", "visibility": "list"},
                                              {"slug": "hidden", "visibility": "hide"}]})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
            if self.path.endswith("/token"):
                f = dict(urllib.parse.parse_qsl(raw))
                SEEN.append(("token", f))
                if f["grant_type"] == "authorization_code":
                    assert hashlib.sha256(f["code_verifier"].encode()).digest()  # a real verifier
                    return self._json({"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
                                       "id_token": jwt({"email": "me@example.com", "sub": "u1", "nonce": SEEN[0][1]["nonce"]})})
                return self._json({"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600})
            body = json.loads(raw)
            SEEN.append(("responses", body, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for t in ['{"ok": ', 'true}']:
                self.wfile.write(f'event: response.output_text.delta\ndata: {json.dumps({"type": "response.output_text.delta", "delta": t})}\n\n'.encode())
            self.wfile.write(b'event: response.completed\ndata: {"type": "response.completed"}\n\n')

        def _json(self, d):
            b = json.dumps(d).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


def test_sign_in_and_brain():
    base = server()
    C.FILE = Path(tempfile.mkdtemp()) / "chatgpt.json"
    C.TOKEN, C.RESOURCE = base + "/token", base + "/v1"
    url = C.start(7860)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "Ghost Studio" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://127.0.0.1:7860/auth/callback" and "chatgpt.tokens.use.direct" in q["scope"]
    assert q["ext_agent_host_id"].startswith("urn:uuid:")
    SEEN.append(("authorize", q))
    SEEN.insert(0, SEEN.pop())  # the nonce the fake server puts in the ID token
    try:
        C.finish({"state": "wrong", "code": "c"})
        raise AssertionError("a wrong state must be refused")
    except RuntimeError:
        pass
    C.start(7860)  # a fresh sign-in after the refused one
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(C.start(7860)).query))
    SEEN[0] = ("authorize", q)
    assert C.finish({"state": q["state"], "code": "abc", "client_id": "oaiapp_123"}) == "me@example.com"
    tok = [s for s in SEEN if s[0] == "token"][-1][1]
    assert tok["client_id"] == "oaiapp_123" and tok["redirect_uri"] == q["redirect_uri"] and tok["resource"] == C.RESOURCE
    assert C.signed_in() and C.token() == "at1" and (C.FILE.stat().st_mode & 0o077) == 0
    d = json.loads(C.FILE.read_text())
    d["expires_at"] = time.time() - 1
    C.FILE.write_text(json.dumps(d))
    assert C.token() == "at2" and json.loads(C.FILE.read_text())["refresh_token"] == "rt2"  # refreshed, refresh token replaced
    nxt = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(C.start(7860)).query))
    assert nxt["client_id"] == "oaiapp_123" and "agent_name_hint" not in nxt  # re-sign-in reuses Ghost's registration
    assert C.models() == ["gpt-6.1-mini", "gpt-6.1-sol"]

    # the plan as a brain: Responses API, store false, stream true, no temperature; tool calls go to the next brain
    llm.STATE.clear()
    llm.PRIVATE["on"] = False
    llm.AUTH["chatgpt"] = (C.signed_in, C.headers)
    llm.CHAIN[:] = [{"id": "chatgpt", "provider": "chatgpt", "name": "ChatGPT (your plan)", "key": "plan", "base": C.RESOURCE,
                     "model": "gpt-6.1-sol", "auth": "chatgpt", "api": "responses"}]
    assert llm.ask_json("x", "Answer JSON", temperature=0.4) == {"ok": True}
    body, auth = SEEN[-1][1], SEEN[-1][2]
    assert auth == "Bearer at2" and body["store"] is False and body["stream"] is True and "temperature" not in body
    assert body["input"] == [{"role": "user", "content": "Answer JSON"}]
    out = "".join(llm.chat_stream("cloud:gpt-6.1-sol", [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]))
    assert out == '{"ok": true}' and SEEN[-1][1]["instructions"] == "be brief"
    try:
        llm._cloud_chat([{"role": "user", "content": "do it"}], tools=[{"type": "function"}])
        raise AssertionError("tools must be skipped")
    except llm.CloudDown as e:
        assert "skip" in str(e)
    assert llm._st(llm.CHAIN[0])["cool_until"] <= time.time()  # skipping isn't a failure: still ready
    llm.CHAIN[:] = []


if __name__ == "__main__":
    test_sign_in_and_brain()
    print("ok chatgpt")
