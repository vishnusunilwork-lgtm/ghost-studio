# How Talk to Ghost works

Talk to Ghost is a small, local version of the architecture behind large chat assistants. The left column lists what
OpenAI has published about ChatGPT, mainly function calling, the router in the GPT-5 system card, and conversation
state. The right column shows how Ghost does the same job on one computer with free models. It is not a copy of
ChatGPT. Its code is private, and none of it is used here.

```
 you ──► UI (glass drawer) ──NDJSON event stream──► /api/agent/chat
                                                     │
                                         ┌───────────▼───────────┐
                                         │  Router (router.py)   │  tier 0 grammar · tier 1 fast model · tier 2 careful model
                                         └───────────┬───────────┘
                                                     │
          ┌──────────────────────────────────────────▼──────────────────────────────────────────┐
          │ Orchestrator loop (orchestrator.py), at most 6 steps                                  │
          │   context.build ──► model step (Ollama /api/chat with tools) ──► tool calls           │
          │   each call: repair ─► validate (JSON schema) ─► policy (now / wait for OK) ─► execute │
          │   result goes back to the model ─► next step … until it answers in words              │
          └───────┬──────────────────────────┬───────────────────────────────┬──────────────────┘
                  │                          │                               │
        Context (context.py)        Tools (tools.py)                Backend (server.py GhostBackend)
        static system rules         19 declared actions             the same code the buttons use:
        memory (knowledge.py)       typed parameters                change / retake / undo / rewrite /
        open project state          read · act · gated              create / delete … via the job queue
        BM25 search of projects
        chat summary + recent turns
```

| Layer | In large assistants | In Ghost (file) |
|---|---|---|
| Routing | A router picks a fast model or a reasoning model per request. | `router.py`. Tier 0 is a grammar for exact commands ("retake 2", "undo", "yes") and needs no model, so it is instant. Tier 1 is the smallest installed model. Tier 2 is the largest. If the fast model fails twice, the turn moves to the careful model. If a model can't load (not enough memory), Ghost uses the other one. |
| Function calling | The model gets JSON-schema tool definitions and proposes calls. The app runs them and returns results. | `tools.py`. The model only proposes a call. Ghost repairs common slips ("shot" when the project has pictures), checks every argument against the schema, then runs it through the same code the buttons use. |
| Permission policy | Big or irreversible actions need confirmation. | Every tool is marked **read**, **act** or **gated**. Gated tools wait for your **Go ahead**: create, delete, start again, re-make in Quality, and any change to *all* pictures or shots. |
| Conversation state | Earlier messages are passed back as context. This does not retrain the model. | `context.py`. Each chat is saved to `.ghost/chats/<id>.json`. Older turns are compacted into a one-line-per-turn summary, so nothing is silently dropped. |
| Long-term memory | Saved memories are added to the context. | `knowledge.py`: "When I write X I mean Y", "Always…", plus a built-in dictionary of Indian words. |
| Retrieval (RAG) | Search finds relevant documents to add to the context. | BM25 ranking over your past projects, so "same as my Onam song" finds the right project without an extra model. |
| Prompt caching | The model server reuses work it has already done on an unchanged prompt prefix. | The system prompt and tool list never change. Everything that does change rides on the newest message, so Ollama reuses its cache. Measured: 80 s → 20 s for the second turn on a CPU test machine. |
| Early exit | — | When a simple request's actions all succeed, the reply is written from the real results. This skips a second model step, so replies come about twice as fast. |
| Streaming | The answer streams as tokens. | Events stream as they happen: route, thinking, each action, its real result, the request for your OK, the answer. |
| Grounding / honesty | — | Ghost shows every action with its real result (✓ or !). The model is told never to claim something a tool didn't report. |
| Observability | Tracing and metrics. | `.ghost/agent.log` is one JSON line per turn: route, model, each step's time, tool calls, errors, fallbacks. |
| Evaluation | Benchmarks. | `tests/test_agent.py` has 11 offline checks with a scripted model. `tests/eval_live.py` has 14 real requests in English and Hinglish, run against the real local models. |

## Two modes in one box (v3.13)

`router.wants_action` decides per message, with no model call:
- **Conversation** (questions, writing help, ideas, translations): streamed word by word like ChatGPT. It uses no tools, so it can't change anything by accident.
- **Action** (something Ghost makes + an action, a number or a change word, or a short request about what is open): the tool loop above.

A memory is saved only when the person says remember / always / means, even if the model tries to save one.

## Live, memory, deep thinking and the optional smarter brain (v3.14)

| Ability | How |
|---|---|
| Live facts | `live.py` runs before the model when a question needs fresh facts (today, news, weather, prices, "who is <Name>"…). Sources: Google News RSS (India edition), Open-Meteo for weather (India-first place lookup), Wikipedia; with an optional Brave Search key, the whole web. Lookups run in parallel in about a second. Results go into the prompt as numbered sources; the answer cites [1], [2]; the sources are shown under the question |
| Knows the date | The current date and time are in every chat prompt (models don't know today's date) |
| Memory across chats | BM25 search over your earlier conversations (`.ghost/chats/`) adds the 3 most relevant past exchanges; no extra model call. Plus "What Ghost has learned" |
| Deep thinking | Qwen 3.x reasoning mode, switched on for plans, strategy, analysis or long questions, or always with the 🧠 button. Slower, smarter |
| Smarter brain (optional) | Your own key for any OpenAI-compatible service (Google Gemini, OpenAI, OpenRouter, Groq). Talk to Ghost, scripts and prompts then use it, with the same tools, permission gate and streaming. Tool calls are translated between the two formats (`llm._to_openai`). Pictures, music and voices stay local. Off by default |

Tested: a real "today news in trivandrum" question answered from 8 live headlines with citations; the cloud adapter checked against a stand-in OpenAI-compatible server (model list, JSON mode, tool call → result → answer, streaming).

## Speed work (v3.13)

- **Short answers from the script AI:** it returns only the subject, the place and one short action per shot. Ghost assembles the full prompt (framing + subject + action + place + quality). That is about 5× fewer words to generate, measured 205 s → 30–40 s on the CPU test machine, and the person looks identical in every shot.
- **Fast mode uses the smallest current model** for prompts; Quality uses the larger one.
- **Models stay in memory:** on computers with 32 GB or more, the script AI stays loaded while pictures are made. The picture model is pre-loaded at start on 16 GB+. The chat model loads when the Talk drawer opens.

## Lessons applied from the Claude engineering report (v3.17)

| Report finding | In Ghost |
|---|---|
| Stream cut mid-answer; overload errors | The chat retries once when the brain is busy. A cut-off answer is marked as cut, and the page says so if the connection drops, with ↻ to try again |
| Edits and regenerate make a branch | ↻ Regenerate and ✎ Edit my message rewind the chat to just before your last message, so the old branch isn't kept in its memory |
| Malicious tool output / prompt injection | Live results go inside an untrusted block that outside text can't close, and the model is told never to follow instructions inside them. Source links must be http(s) |
| Environment controls beat prompts | A local-only guard: the Host must be this computer (no DNS rebinding), and every change must come from Ghost's own page (no cross-site requests from other websites) |
| URL-encoded leaks via rendered output | The chat renders only bold, code and lists: no images or links from model text |
| Human approval for irreversible actions | Already in place: create, delete, start again and change-all wait for Go ahead |
| Bounded loops | At most 6 model rounds per turn |
| Per-platform canaries | ⚙ → **Check Ghost** tests every part with timings: editor, script AI (a real answer), pictures, voice, music, internet, disk |
| Prefix caching, stable prompt order | Already in place (v3.11) |

The report's figures about Anthropic's own fleet (chip types, costs) don't apply to a single computer and weren't used.

## Talking pictures (v3.19, free mode)

`talk.py`: the picture AI edits each speaking shot into mouth shapes: **open** ("aa") always, and in Quality also
**half** (lips parted) and **round** ("oo"), plus an **eyes-closed** picture for blinks. Each edit is compared with the
original and only the changed area (mouth or eyelids) is blended in with a soft edge, so nothing else flickers; an
edit that changed too much is skipped. The voice is measured 24× a second: loudness picks closed / half / open, and
how dark the sound is (o/u vowels have less high-frequency energy) picks round lips. Each shape is held at least 2
frames and every change passes through a half-way frame. Blinks come every 2–4.5 s. Frames are streamed straight into
ffmpeg; the editor then adds the camera move. It works for animals and people.

Why not a neural lip-sync model (the BRAHMA / VAANI idea)? The open ones (LatentSync, MuseTalk) detect and redraw
*human* faces only, so they can't help a talking cat or cow; LatentSync 1.6 needs about 18 GB of graphics memory
and takes roughly 20 s per second of video even with the MLX port; and the face detector some of them use (InsightFace)
has non-commercial model weights. Wav2Lip is non-commercial. It may be added later as an optional add-on for human
presenters, after testing on a Mac.

## General purpose + scene logic (v3.23)

- No default brand any more (it used to be ShareChat with a devotional description, which pulled scripts toward greetings
  and "download the app"). Brand only when you save a Brand kit.
- `scene.py`, applied to every picture prompt: real sizes (ant 5 mm, elephant 3 m → "about 600 times smaller", macro lens at
  ground level), deliberate swaps ("a small elephant and a big ant" → a clearly surreal scene), exact counts ("exactly 3 cats").
- Talk to Ghost makes things itself: "generate a cat", "make 3 pictures of…", "make a 15 s video of…" start straight away
  (a few pictures need no OK; video and music still ask). It never points people to other tools. Manglish = Malayalam in
  English letters (Hinglish, Tanglish… likewise).

## 🧬 Ghost DNA: shrink old projects (v3.22)

`dna.py`. DNA stores the recipe for a body, not the body. A Ghost picture's recipe (prompt, seed, size, steps, the
main-look reference) is a few hundred bytes in `project.json`; the picture itself is megabytes. **Shrink** keeps the
recipe and a 384 px preview; **🧬 Regrow** (`Runner._regrow`) makes the picture again, main look first so the others
can follow it, and measures how close it came (64×64 comparison, shown as "regrown 99%").

- Only *pure* recipes become DNA: made once from prompt + seed, never ✨ Changed, not a quality-check retake, and
  (for "same look" pictures) the main look is pure too. Everything else stays as a full-size JPEG (quality 92,
  about 5–10× smaller than PNG).
- Video ads keep the finished video, narration, music and uploads; shot pictures and clips are regrown on demand.
- Any job on a shrunk project (Retake, Change, Re-edit…) regrows first, automatically. Download-all waits for Regrow.
- Undo history is cleared, because it points at removed files.
- Honest limit: the same seed gives the same picture only with the same model, settings and computer type. After
  a model update or on a different Mac, Regrow can give a close variation; that's why the preview is kept and the
  match is shown. Fully general compression of 2 TB into 1 KB is impossible (8,192 bits can't tell apart that many files).

## AI motion on this Mac (v3.21)

Three ways Ghost makes a shot, picked automatically: **Google Cloud / video service** (Veo 3.1: motion, sound,
lip-sync) → **AI motion on this Mac** (add-on) → **photo motion** (pictures + camera moves, talking mouths).

The AI motion add-on runs **FastMetal-5B-QAD** (Hao AI Lab, FastVideo, Apache 2.0): Wan2.2-TI2V-5B (Apache 2.0)
distilled from ~50 denoising steps to 3 and quantised to INT8 for Apple's MLX. Its makers measured, on an M4 Max
with 36 GB, about 151 s for a 5-second 720p clip and about 47 s in fast mode (half the frames, the rest filled in by
RIFE frame interpolation); a 24 GB M5 MacBook Air took about 90–200 s. Peak memory about 9 GB.

How Ghost uses it (`addons.make_motion` → `workers/motion_worker.py`, its own Python 3.12):
- every shot of the ad in ONE run: the text encoder (UMT5, on the CPU) encodes all shot descriptions once, then the
  video model loads once and makes the shots one after another (the stock script loads both per shot);
- the picture model is dropped from memory first (`engines.free_all`);
- each prompt = framing + the shot's subject/place/action + its motion, Indian words spelled out (knowledge.py),
  spoken lines removed (the model has no sound; Ghost adds voice and music);
- Fast = 832×448 with frame interpolation; Quality = 1280×704;
- ✨ Change on a moving shot adds the change to its description and re-makes it; Pause stops the run at once;
- the clip's first frame becomes the shot's picture.

Limits: text-to-video only in the released Mac build (no starting picture), so the same character can differ a little
between shots; no lip-sync. Wan2.2 on a Mac without this distilled model took over an hour for 2 seconds.

## Your own Google Cloud (v3.20)

Why a free local Ghost can't match Veo in seconds: Veo, Gemini image, Seedance and the models behind Higgsfield are
very large models running on data-centre chips (TPU / H100-class) many times faster than a laptop graphics chip,
spread over many chips per request. A single Veo clip still takes from about 11 seconds to several minutes on
Google's own servers. On a Mac, Ghost can only make pictures and animate them (photo motion).

`engines.Google` connects Ghost to a company's own Google Cloud project, so the same models as the Google Cloud
console make the ad: **Veo 3.1** (`predictLongRunning` → `fetchPredictOperation`, image-to-video so every shot keeps
the same character, native sound and lip-sync) and **Gemini image** (`generateContent` with `responseModalities: IMAGE`,
the main look passed as a reference). Sign-in is an API key or the Google Cloud CLI token. Model names change often
(Imagen was shut down in August 2026, Veo preview names in October 2026), so Ghost tries a list newest-first and
remembers the one that answered. All shots and pictures are made at the same time, so an ad takes about as long as its
slowest clip. Spending is counted per month and stops at the limit. Pictures, video and sound come from Google;
the script AI stays as configured.

## Media engines: the same ideas as Gemini, ElevenLabs and Suno, with free models

The big services publish what they do, not their code or weights. Ghost uses the same published ideas with
open models that allow commercial use, running on your computer.

| Idea from the reports | The big service | Ghost |
|---|---|---|
| Fast model + careful model behind one box | Gemini Flash/Pro, GPT-5 router | Router tiers (above), Qwen 3.5 small + large, "thinking" switched off for quick tool picks |
| Natively multimodal understanding | Gemini | Qwen3-VL checks pictures (hands, eyes) and reads reference ads; faster-whisper hears them |
| Voice design (describe a voice in words) | ElevenLabs Voice Design | Natural voices: Indic Parler-TTS (AI4Bharat). The voice is a sentence: speaker, tone, pace, recording quality |
| Dubbing (transcribe → translate → re-voice → keep background) | ElevenLabs Dubbing | `dub.py`: whisper timings → script AI translation of the same length → natural voice → fit each line to its slot (≤1.45×) → Demucs removes the old voice, keeps music → one ffmpeg mix, video untouched |
| Music from text, cover, replace a section | Suno Create / Cover / Replace Section | ACE-Step 1.5: text2music, cover (same tune, new sound, from your own audio too), repaint (re-make only seconds A–B) |
| Stem separation | Suno Stems | Demucs htdemucs: vocals, drums, bass, other |
| Content provenance | SynthID, C2PA | Not built in yet. Label AI content yourself |

Isolation: voices and stems each run in their own small Python (`.ghost/engines/<name>/.venv`) through a worker
(`app/workers/`), the same way the music engine does. Their library versions can't break the picture models,
and a crash in one engine can't take down the app.

## Measured on the build machine

These numbers come from a cloud test machine with no graphics chip, so it is much slower than a Mac.

- **Accuracy with Qwen 3.5 4B (v3.12):** 13/14 correct on `tests/eval_live.py`, including the two-step song request the older model got wrong. The miss ("4 more pictures like this but at Diwali") led to a new rule in the system prompt; with it, that request is now handled correctly (proposes a new project based on the open one).
- **Accuracy with Qwen 2.5 (v3.11):** 12/14 correct. The two failures happened when the 7B model couldn't load in that machine's memory. After the fallback fix, both requests ran on the 3B model, but it got one of them only partly right.
- **Dubbing (end-to-end test):** a 12-second English video with music → Hindi in 60 s on that machine. Demucs removed the original voice (a speech check of the result found none) and kept the music.
- **Speed on that machine:** tier 0 replies take under 10 ms. A tier 1 (3B model) reply takes 4–25 s when the model is already loaded. On Apple-chip Macs, local models run much faster, so most replies should take a few seconds. That hasn't been measured yet.

## Limits

- Small local models understand well but make more mistakes than ChatGPT-class models. The schema check, the repair step, the
  permission gate and the visible results exist to catch those mistakes.
- Talk to Ghost needs the Script add-on (Ollama + Qwen 2.5). Without it, only tier 0 commands work.
