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

## Measured on the build machine

These numbers come from a cloud test machine with no graphics chip, so it is much slower than a Mac.

- **Accuracy:** 12/14 correct on `tests/eval_live.py`. The two failures happened when the 7B model couldn't load in that machine's memory. After the fallback fix, both requests ran on the 3B model, but it got one of them only partly right.
- **Speed on that machine:** tier 0 replies take under 10 ms. A tier 1 (3B model) reply takes 4–25 s when the model is already loaded. On Apple-chip Macs, local models run much faster, so most replies should take a few seconds. That hasn't been measured yet.

## Limits

- Small local models understand well but make more mistakes than ChatGPT-class models. The schema check, the repair step, the
  permission gate and the visible results exist to catch those mistakes.
- Talk to Ghost needs the Script add-on (Ollama + Qwen 2.5). Without it, only tier 0 commands work.
