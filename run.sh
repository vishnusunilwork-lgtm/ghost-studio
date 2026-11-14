#!/usr/bin/env bash
cd "$(dirname "$0")"
(ollama serve >/dev/null 2>&1 &) ; sleep 3
( sleep 3; (xdg-open http://localhost:7860 || open http://localhost:7860) >/dev/null 2>&1 ) &
./.venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 7860
