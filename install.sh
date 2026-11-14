#!/usr/bin/env bash
# AI Film Studio Local - one-time setup for Linux (NVIDIA GPU) or macOS (test mode only)
set -e
cd "$(dirname "$0")"
if [ "$(uname)" = "Darwin" ]; then
  command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
  brew install python@3.11 ffmpeg ollama
  PY=python3.11
else
  sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv ffmpeg curl
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  PY=python3.11
fi
$PY -m venv .venv
./.venv/bin/pip install --upgrade pip
if [ "$(uname)" = "Darwin" ]; then ./.venv/bin/pip install torch torchvision torchaudio
else ./.venv/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128; fi
./.venv/bin/pip install -r requirements.txt
(ollama serve >/dev/null 2>&1 &) ; sleep 5
ollama pull qwen2.5:14b
ollama pull qwen2.5vl:7b
echo "Done! Run ./run.sh to start."
