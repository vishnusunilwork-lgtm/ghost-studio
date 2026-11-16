"""Continue with ChatGPT: use your ChatGPT Plus / Pro plan as one of Ghost's brains (OpenAI's "Sign in with ChatGPT").

OpenAI lets open-source apps that run on your own computer use the ChatGPT plan you already pay for, with your OK:
  1 Ghost opens OpenAI's sign-in page in your browser (OAuth with PKCE; Ghost never sees your password)
  2 you approve "Ghost Studio" and how much of your plan it may use (ChatGPT → Settings → Usage)
  3 OpenAI sends Ghost back to http://127.0.0.1:<port>/auth/callback with a one-time code; Ghost swaps it for tokens
     (kept only in .ghost/chatgpt.json on this computer, readable only by you) and refreshes them by itself
Requests go to OpenAI's Responses API with the plan token (store=false, stream=true). Plan usage covers text answers
(scripts, prompts, ideas, Talk to Ghost); pictures and voice from OpenAI still need an API key.
Docs: https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

AUTH = "https://auth.openai.com/api/accounts/authorize"
TOKEN = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
APP = "Ghost Studio"
FILE = Path(__file__).resolve().parent.parent / ".ghost" / "chatgpt.json"
PENDING: dict = {}
UA = "GhostStudio/3 (+https://github.com/vishnusunilwork-lgtm/ghost-studio)"


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(FILE)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _claims(id_token: str) -> dict:
    """The ID token's claims. It came straight from OpenAI's token endpoint over TLS, so its contents are trusted
    (OpenID Connect allows that); Ghost still checks the nonce it sent."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def signed_in() -> bool:
    d = _load()
    return bool(d.get("refresh_token") and d.get("client_id"))


def account() -> str:
    return _load().get("email", "")


def start(port: int) -> str:
    """The sign-in address to open in the browser."""
    d = _load()
    if not d.get("host_id"):
        d["host_id"] = "urn:uuid:" + str(uuid.uuid4())
        _save(d)
    verifier = _b64(secrets.token_bytes(48))
    state, nonce = _b64(secrets.token_bytes(24)), _b64(secrets.token_bytes(24))
    redirect = f"http://127.0.0.1:{int(port)}/auth/callback"
    client = d.get("client_id") or "dynamic_agent_client"
    PENDING.clear()
    PENDING.update(state=state, nonce=nonce, verifier=verifier, redirect=redirect, client=client, at=time.time())
    q = {"client_id": client, "response_type": "code", "redirect_uri": redirect, "scope": SCOPE, "resource": RESOURCE,
         "state": state, "nonce": nonce, "code_challenge_method": "S256",
         "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "ext_agent_host_id": d["host_id"]}
    if client == "dynamic_agent_client":
        q["agent_name_hint"] = APP
    if d.get("email"):
        q["login_hint"] = d["email"]
    return AUTH + "?" + urllib.parse.urlencode(q)


def _post(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode() or "{}"
        return json.loads(body)


def finish(params: dict) -> str:
    """The callback: checks it, swaps the code for tokens. Returns the account email."""
    p = dict(PENDING)
    if not p or params.get("state") != p.get("state") or time.time() - p.get("at", 0) > 900:
        raise RuntimeError("This sign-in link is old or wasn't started by Ghost. Press Continue with ChatGPT again.")
    PENDING.clear()
    if params.get("error"):
        raise RuntimeError("ChatGPT sign-in was cancelled (" + str(params["error"])[:80] + ").")
    client = params.get("client_id") or p["client"]
    if client == "dynamic_agent_client" or (p["client"] != "dynamic_agent_client" and client != p["client"]):
        raise RuntimeError("OpenAI didn't return Ghost's app registration. Press Continue with ChatGPT again.")
    t = _post(TOKEN, {"grant_type": "authorization_code", "client_id": client, "code": params.get("code", ""),
                      "code_verifier": p["verifier"], "redirect_uri": p["redirect"], "resource": RESOURCE})
    c = _claims(t.get("id_token", ""))
    if c.get("nonce") and c["nonce"] != p["nonce"]:
        raise RuntimeError("The sign-in answer didn't match (nonce). Press Continue with ChatGPT again.")
    d = _load()
    d.update(client_id=client, access_token=t["access_token"], refresh_token=t.get("refresh_token", ""),
             expires_at=time.time() + int(t.get("expires_in") or 3600) - 120, email=c.get("email", ""), sub=c.get("sub", ""),
             id_token=t.get("id_token", ""))
    _save(d)
    return d["email"]


def token() -> str:
    """A fresh access token (refreshed by itself; the refresh token is replaced each time)."""
    d = _load()
    if not d.get("refresh_token"):
        raise RuntimeError("Not connected to ChatGPT")
    if d.get("access_token") and time.time() < d.get("expires_at", 0):
        return d["access_token"]
    try:
        t = _post(TOKEN, {"grant_type": "refresh_token", "client_id": d["client_id"], "refresh_token": d["refresh_token"],
                          "resource": RESOURCE})
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            for k in ("access_token", "refresh_token", "expires_at"):
                d.pop(k, None)
            _save(d)
            raise RuntimeError("ChatGPT disconnected Ghost: press Continue with ChatGPT again (⚙ → Smarter brain).") from None
        raise
    d.update(access_token=t["access_token"], refresh_token=t.get("refresh_token") or d["refresh_token"],
             expires_at=time.time() + int(t.get("expires_in") or 3600) - 120)
    _save(d)
    return d["access_token"]


def headers() -> dict:
    return {"Authorization": "Bearer " + token()}


def models() -> list[str]:
    req = urllib.request.Request(RESOURCE + "/models", headers={**headers(), "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    out = [m.get("slug") or m.get("id") for m in d.get("models") or d.get("data") or []
           if (m.get("visibility") in (None, "list")) and (m.get("slug") or m.get("id"))]
    return out


def signout():
    d = _load()
    if d.get("refresh_token") and d.get("client_id"):
        try:
            with urllib.request.urlopen(urllib.request.Request(DISCOVERY, headers={"User-Agent": UA}), timeout=15) as r:
                revoke = json.loads(r.read().decode())["revocation_endpoint"]
            _post(revoke, {"token": d["refresh_token"], "token_type_hint": "refresh_token", "client_id": d["client_id"]})
        except Exception:
            pass
    keep = {k: d[k] for k in ("host_id", "client_id", "email") if k in d}  # the app registration is reused next time
    _save(keep)
