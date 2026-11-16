"""🎙 Live voice: a real-time spoken conversation with Gemini Live, on your Google Cloud sign-in (no key).

  your mic ──16 kHz PCM──▶ Ghost (this computer) ──▶ Gemini Live (Vertex AI, your project)
  speaker ◀──24 kHz PCM── Ghost ◀── a natural voice, answers start in about half a second, you can interrupt it

Ghost sits in the middle so your Google access token never reaches the browser. When you ask for something to be
made or changed, Gemini calls one tool, `ghost_do`, and Ghost's own agent (the same one as Talk to Ghost) does it;
the result goes back to Gemini, which tells you aloud. What you both say is written into the open chat (the
project's own chat when a project is open) and saved with it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

MODELS = ["gemini-3.8-live", "gemini-live-2.5-flash-native-audio"]  # newest first; the first one your project has is used
LOCATION = "us-central1"  # Live models are regional (not "global")
VOICES = {"female": "Aoede", "male": "Charon"}
PRICE_PER_MIN = 0.03  # USD, rough (audio in + out), so the monthly limit still works

SYSTEM = """You are Ghost, the voice of Ghost Studio, a personal assistant and ad / content director for an Indian creator.
Talk like a warm, quick, smart friend: short spoken sentences, no lists, no markdown, no emojis. Reply in the language the
person speaks (Hindi, Malayalam, Manglish, Tamil, English or a mix), the way people really talk.
You can do things in Ghost Studio with the tool ghost_do: make video ads, pictures and songs, change or retake shots,
open, pause or delete projects, rewrite text, remember preferences, look up their projects. Put the full request in
the person's own words into `request` (include length, language, mood, place and music if they said them).
Before anything big (making a new video, deleting, anything that costs money) say in one line what you'll do and wait
for a clear yes. While a tool runs, say you're on it; afterwards tell them the result in a sentence.
For facts that may have changed (news, prices, today's events) say what you know may be out of date unless the tool
looked it up. Never invent results of actions."""

TOOL = {"functionDeclarations": [{
    "name": "ghost_do",
    "description": "Do something in Ghost Studio, or ask it something about the person's projects. Ghost's own agent carries it out "
                   "and returns what happened.",
    "parameters": {"type": "OBJECT", "properties": {
        "request": {"type": "STRING", "description": "What to do, in the person's words, with every detail they gave."},
        "confirmed": {"type": "BOOLEAN", "description": "True when the person already said yes to this exact plan."}},
        "required": ["request"]}}]}


def url() -> str:
    return f"wss://{LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"


def setup_msg(project: str, model: str, voice: str = "female", extra: str = "") -> dict:
    return {"setup": {
        "model": f"projects/{project}/locations/{LOCATION}/publishers/google/models/{model}",
        "generationConfig": {"responseModalities": ["AUDIO"],
                             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICES.get(voice, VOICES["female"])}}}},
        "systemInstruction": {"parts": [{"text": SYSTEM + ("\n\n" + extra if extra else "")}]},
        "tools": [TOOL],
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {"automaticActivityDetection": {"disabled": False, "silenceDurationMs": 600}},
    }}


async def _connect(headers: dict):
    from websockets.asyncio.client import connect
    return await connect(url(), additional_headers=headers, max_size=16 * 1024 * 1024, open_timeout=20, ping_interval=20)


async def open_live(goog, voice: str, extra: str, connect=None):
    """Connects and finishes setup with the newest Live model your project has. Returns (upstream, model)."""
    connect = connect or _connect
    headers = await asyncio.to_thread(goog._headers)
    last = ""
    for m in MODELS:
        up = None
        try:
            up = await connect(headers)
            await up.send(json.dumps(setup_msg(goog.project, m, voice, extra)))
            first = json.loads(await asyncio.wait_for(up.recv(), 20))
            if "setupComplete" in first:
                return up, m
            last = json.dumps(first)[:200]
        except Exception as e:
            last = str(e)[:200]
        if up is not None:
            try:
                await up.close()
            except Exception:
                pass
    raise RuntimeError("Gemini Live didn't start (" + last + "). Check that Vertex AI is on for your project, then try again.")


async def bridge(client, goog, run_tool, save_turn, voice: str = "female", extra: str = "", connect=None, spend=None):
    """client: the browser's WebSocket (FastAPI). run_tool(request, confirmed) -> (text, events) runs in a thread.
    save_turn(you, ghost) stores what was said. Ends when either side closes."""
    up, model = await open_live(goog, voice, extra, connect)
    await client.send_text(json.dumps({"type": "ready", "model": model}))
    t0 = time.time()
    said = {"you": "", "ghost": ""}

    async def from_browser():
        while True:
            msg = await client.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("bytes"):  # 16 kHz 16-bit mono PCM from the mic
                await up.send(json.dumps({"realtimeInput": {"audio": {"data": base64.b64encode(msg["bytes"]).decode(),
                                                                      "mimeType": "audio/pcm;rate=16000"}}}))
            elif msg.get("text"):
                d = json.loads(msg["text"])
                if d.get("type") == "text" and d.get("text"):  # typed while talking
                    await up.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": d["text"]}]}],
                                                                "turnComplete": True}}))
                elif d.get("type") == "stop":
                    return

    async def from_gemini():
        async for raw in up:
            d = json.loads(raw)
            sc = d.get("serverContent") or {}
            for part in (sc.get("modelTurn") or {}).get("parts") or []:
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    await client.send_bytes(base64.b64decode(data))  # 24 kHz 16-bit mono PCM
            if (sc.get("inputTranscription") or {}).get("text"):
                said["you"] += sc["inputTranscription"]["text"]
                await client.send_text(json.dumps({"type": "you", "text": said["you"]}))
            if (sc.get("outputTranscription") or {}).get("text"):
                said["ghost"] += sc["outputTranscription"]["text"]
                await client.send_text(json.dumps({"type": "ghost", "text": said["ghost"]}))
            if sc.get("interrupted"):
                await client.send_text(json.dumps({"type": "interrupted"}))
            if sc.get("turnComplete"):
                if said["you"].strip() or said["ghost"].strip():
                    await asyncio.to_thread(save_turn, said["you"].strip(), said["ghost"].strip())
                said.update(you="", ghost="")
                await client.send_text(json.dumps({"type": "turn"}))
            calls = (d.get("toolCall") or {}).get("functionCalls") or []
            if calls:
                answers = []
                for c in calls:
                    a = c.get("args") or {}
                    await client.send_text(json.dumps({"type": "working", "text": str(a.get("request", ""))[:200]}))
                    try:
                        text, events = await asyncio.to_thread(run_tool, str(a.get("request", "")), bool(a.get("confirmed")))
                    except Exception as e:
                        text, events = f"It didn't work: {str(e)[:200]}", []
                    for ev in events:
                        await client.send_text(json.dumps({"type": "event", "event": ev}))
                    answers.append({"id": c.get("id"), "name": c.get("name", "ghost_do"), "response": {"result": text[:4000]}})
                await up.send(json.dumps({"toolResponse": {"functionResponses": answers}}))
            if "goAway" in d:
                await client.send_text(json.dumps({"type": "reconnect"}))
                return

    tasks = [asyncio.create_task(from_browser()), asyncio.create_task(from_gemini())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception():
                raise t.exception()
    finally:
        try:
            await up.close()
        except Exception:
            pass
        if spend:
            spend(round((time.time() - t0) / 60 * PRICE_PER_MIN, 4))
