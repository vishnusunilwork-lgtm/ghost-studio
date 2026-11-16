"""Offline check of 🎙 Live voice: a stand-in Gemini Live server (WebSocket) and a stand-in browser.
Run:  python3 -m tests.test_live"""
from __future__ import annotations

import asyncio
import base64
import json

from app import voice_live as VL


class Goog:
    project = "demo-project"

    def _headers(self):
        return {"Authorization": "Bearer t", "x-goog-user-project": "demo-project"}


class Browser:
    """Like FastAPI's WebSocket: receive() / send_text() / send_bytes()."""
    def __init__(self, script):
        self.script, self.text, self.audio = list(script), [], []

    async def receive(self):
        await asyncio.sleep(0.05)
        if self.script:
            return self.script.pop(0)
        await asyncio.sleep(0.6)  # let the model's answers arrive, then hang up
        return {"type": "websocket.disconnect"}

    async def send_text(self, t):
        self.text.append(json.loads(t))

    async def send_bytes(self, b):
        self.audio.append(b)


def test_live_bridge():
    from websockets.asyncio.server import serve
    got = {"setups": [], "audio": 0, "tool_response": None}

    async def gemini(ws):
        setup = json.loads(await ws.recv())["setup"]
        got["setups"].append(setup["model"].rsplit("/", 1)[-1])
        got.setdefault("tools", []).append([list(t)[0] for t in setup["tools"]])
        if setup["model"].endswith("gemini-3.8-live"):  # pretend the project doesn't have the newest model yet
            await ws.close(1008, "model not found")
            return
        assert setup["tools"][0]["functionDeclarations"][0]["name"] == "ghost_do" and "Diwali" in setup["systemInstruction"]["parts"][0]["text"]
        await ws.send(json.dumps({"setupComplete": {}}))
        async for raw in ws:
            d = json.loads(raw)
            if "realtimeInput" in d:
                assert d["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
                got["audio"] += 1
                if got["audio"] == 2:
                    await ws.send(json.dumps({"serverContent": {"inputTranscription": {"text": "15 second Diwali video banao"}}}))
                    await ws.send(json.dumps({"toolCall": {"functionCalls": [{"id": "c1", "name": "ghost_do",
                                                                              "args": {"request": "15 second Diwali video", "confirmed": True}}]}}))
            if "toolResponse" in d:
                got["tool_response"] = d["toolResponse"]["functionResponses"][0]
                await ws.send(json.dumps({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                                                                    "data": base64.b64encode(b"\x01\x00" * 480).decode()}}]},
                                                            "outputTranscription": {"text": "Bana rahi hoon!"}}}))
                await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))

    async def main():
        async with serve(gemini, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            from websockets.asyncio.client import connect

            async def conn(headers):
                assert headers["Authorization"] == "Bearer t"
                return await connect(f"ws://127.0.0.1:{port}")
            b = Browser([{"type": "websocket.receive", "bytes": b"\x00\x00" * 640}] * 3)
            saved, ran, spent = [], [], []
            await VL.bridge(b, Goog(), lambda name, a: (ran.append((a.get("request"), a.get("confirmed"))) or ("done: Making “Diwali”", [{"t": "open", "project": "p1"}])),
                            lambda you, ghost: saved.append((you, ghost)), extra="OPEN PROJECT title=\"Diwali\"", connect=conn,
                            spend=spent.append)
            return b, saved, ran, spent
    b, saved, ran, spent = asyncio.run(main())
    assert got["setups"] == ["gemini-3.8-live", "gemini-3.8-live", "gemini-live-2.5-flash-native-audio"]  # falls back to the model the project has
    assert got["tools"][0] == ["functionDeclarations", "googleSearch"] and got["tools"][1] == ["functionDeclarations"]
    assert b.text[0] == {"type": "ready", "model": "gemini-live-2.5-flash-native-audio"}
    assert got["audio"] == 3 and ran == [("15 second Diwali video", True)]
    assert got["tool_response"] == {"id": "c1", "name": "ghost_do", "response": {"result": "done: Making “Diwali”"}}
    assert {"type": "event", "event": {"t": "open", "project": "p1"}} in b.text
    assert b.audio and len(b.audio[0]) == 960  # Gemini's voice reaches the speaker
    assert saved == [("15 second Diwali video banao", "Bana rahi hoon!")] and spent and spent[0] >= 0


if __name__ == "__main__":
    test_live_bridge()
    print("ok live")
