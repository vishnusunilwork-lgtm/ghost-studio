"""Offline check of the Google Cloud adapter against a stand-in server (no real Google calls).
Run:  python3 -m tests.test_google"""
from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app import engines as E

SEEN = []


def png(w=64, h=32):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


class H(BaseHTTPRequestHandler):
    polls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, body, self.headers.get("x-goog-api-key")))
        if "gemini-nano-banana-2.1" in self.path:  # newest name not on this project: Ghost must try the next
            return self._send(404, {"error": {"code": 404, "message": "Publisher Model not found"}})
        if self.path.endswith(":generateContent"):
            return self._send(200, {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": png()}}]}}]})
        if self.path.endswith(":predictLongRunning"):
            return self._send(200, {"name": "projects/p/locations/us-central1/publishers/google/models/veo/operations/42"})
        if self.path.endswith(":fetchPredictOperation"):
            H.polls += 1
            if H.polls < 2:
                return self._send(200, {"name": body["operationName"], "done": False})
            return self._send(200, {"done": True, "response": {"videos": [{"bytesBase64Encoded": base64.b64encode(b"MP4DATA").decode(), "mimeType": "video/mp4"}]}})
        self._send(400, {"error": {"message": "bad"}})

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def test_google():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    E.Spend.f = Path(tempfile.mkdtemp()) / "spend.json"
    g = E.Google({"api": {"google_key": "k" * 39, "google_project": "my-proj-1", "monthly_limit_usd": 5}})
    g._prefixes = lambda m, locs: [f"{base}/v1/projects/my-proj-1/locations/{locs[0]}/publishers/google/models/{m}"]
    E.time.sleep = lambda s: None
    d = Path(tempfile.mkdtemp())
    g.image("a desi dog", d / "a.png", 90, 160, seed=3)
    from PIL import Image
    assert Image.open(d / "a.png").size == (90, 160)
    assert g.works["image"][0] == "gemini-3.1-flash-image"  # fell through the missing model, remembered the working one
    gen = [b for p, b, k in SEEN if p.endswith(":generateContent")][-1]
    assert gen["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16" and SEEN[-1][2] == "k" * 39
    g.video(d / "a.png", "the dog walks", 5, d / "v.mp4", seed=9)
    assert (d / "v.mp4").read_bytes() == b"MP4DATA"
    sub = [b for p, b, k in SEEN if p.endswith(":predictLongRunning")][0]
    assert sub["parameters"]["durationSeconds"] == 6 and sub["parameters"]["generateAudio"] is True
    assert sub["instances"][0]["image"]["mimeType"] == "image/jpeg"
    assert abs(E.Spend.month()["usd"] - (0.04 + 0.30)) < 1e-6
    try:
        g.video(d / "a.png", "x", 8, d / "w.mp4")  # 0.34 + 0.40 is fine; push past the $5 limit
        E.Spend.add(5)
        g.video(d / "a.png", "x", 8, d / "w.mp4")
        raise AssertionError("limit not enforced")
    except RuntimeError as e:
        assert "limit" in str(e)
    eng = E.Engines({"api": {"google_key": "k" * 39}, "quality": {}})
    assert eng.cloud and eng.plan()["video"] == "service" and eng.plan()["image"] == "service"
    # the console's settings: model, 1080p (8 s clips), no audio, 2 results, text-to-video
    E.Spend.f = Path(tempfile.mkdtemp()) / "spend.json"
    g3 = E.Google({"api": {"google_key": "k" * 39, "google_project": "my-proj-1", "google_tier": "fast", "google_resolution": "1080p",
                           "google_audio": False, "google_takes": 2, "google_mode": "text", "monthly_limit_usd": 50}})
    g3._prefixes = lambda m, locs: [f"{base}/v1/projects/my-proj-1/locations/{locs[0]}/publishers/google/models/{m}"]
    H.polls = 0
    SEEN.clear()
    g3.video(d / "a.png", "train at night", 5, d / "t.mp4")
    body = [b for p, b, k in SEEN if p.endswith(":predictLongRunning")][0]
    pr = body["parameters"]
    assert "veo-3.1-fast" in [p for p, b, k in SEEN if p.endswith(":predictLongRunning")][0]
    assert pr["durationSeconds"] == 8 and pr["resolution"] == "1080p" and pr["generateAudio"] is False and pr["sampleCount"] == 2
    assert "image" not in body["instances"][0] and abs(E.Spend.month()["usd"] - 8 * 0.10 * 2) < 1e-6
    # a key from a project where Vertex AI is off: Ghost explains the fix and makes pictures on this computer instead
    g2 = E.Google({"api": {"google_key": "k" * 39, "google_project": "my-proj-1"}})
    err = E.GoogleError(403, "Agent Platform API has not been used in project 78328758841 before or it is disabled. Enable it by visiting x")
    why = g2.setup_problem(err)
    assert "78328758841" in why and "aiplatform.googleapis.com?project=78328758841" in why
    eng2 = E.Engines({"api": {"google_key": "k" * 39}, "quality": {}})
    eng2.goog.broken = why
    assert not eng2.goog.on and eng2.plan()["video"] != "service"
    srv.shutdown()


if __name__ == "__main__":
    test_google()
    print("ok google")
