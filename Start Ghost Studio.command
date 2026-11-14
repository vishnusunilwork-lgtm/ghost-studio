#!/bin/bash
# Ghost Studio — double-click to start.
# Every start it checks the team's GitHub project for a newer version and updates itself.
# Your ads (projects/), your settings (config.local.yaml) and the installed parts (.venv/) are never touched.

REPO="vishnusunilwork-lgtm/ghost-studio"   # filled in by the person who set up the team project, e.g. yourname/ghost-studio

main() {
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")" || exit 1
mkdir -p .ghost/bin
export PATH="$PWD/.ghost/bin:$PATH"
clear
echo ""
echo "  ◯  Ghost Studio"
echo "  ---------------------------------------------"
echo "  Keep this window open while you use the app. To stop: close this window."
echo ""

# ---------- 1) Updates from the team project ----------
if [ "$1" != "--no-update" ] && [[ "$REPO" == */* ]]; then
  printf '%s' "$REPO" > .ghost/repo
  get_gh
  if command -v gh >/dev/null 2>&1; then
    if ! gh auth status -h github.com >/dev/null 2>&1; then
      echo "  First time only: sign in to GitHub so the app can get updates."
      echo "  1. A code appears below.  2. Press Enter — your browser opens."
      echo "  3. Paste the code there and click Authorize."
      echo ""
      gh auth login -h github.com -p https --web --skip-ssh-key 2>&1 </dev/tty || true
      echo ""
    fi
    REMOTE=$(gh api "repos/$REPO/commits/main" --jq .sha 2>/dev/null)
    LOCAL=$(cat .ghost/version 2>/dev/null)
    if [ -n "$REMOTE" ] && [ "$REMOTE" != "$LOCAL" ]; then
      echo "  Getting the newest version…"
      rm -rf .ghost/new && mkdir -p .ghost/new
      if gh api "repos/$REPO/tarball/main" > .ghost/new.tgz 2>/dev/null && tar -xzf .ghost/new.tgz -C .ghost/new; then
        SRC=$(find .ghost/new -mindepth 1 -maxdepth 1 -type d | head -1)
        if [ -f "$SRC/app/server.py" ]; then
          cp -R "$SRC"/. ./
          echo "$REMOTE" > .ghost/version
          chmod +x "Start Ghost Studio.command" 2>/dev/null
          rm -rf .ghost/new .ghost/new.tgz
          echo "  Updated to $(cat VERSION 2>/dev/null) (${REMOTE:0:7})."
          exec bash "$SELF" --no-update      # restart with the new version of this file
        fi
      fi
      echo "  Could not update right now — starting the version you have."
    elif [ -z "$REMOTE" ]; then
      echo "  Could not check for updates (offline or no access) — starting the version you have."
    fi
  fi
fi
if [ ! -f app/server.py ]; then
  echo "  The app files are missing and could not be downloaded."
  echo "  Check your internet and that Vishnu invited your GitHub account, then try again."
  read -r; exit 1
fi

# ---------- 2) Python 3.10 or newer ----------
if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "  Getting Python (one time)…"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$PWD/.ghost/bin" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
    uv venv --python 3.11 .venv >/dev/null 2>&1 && uv pip install --python .venv/bin/python pip >/dev/null 2>&1 \
      || { echo "  Could not get Python. Send a screenshot of this window to Vishnu."; read -r; exit 1; }
  else
    echo "  Setting up (one time)…"
    "$PY" -m venv .venv || { echo "  Could not set up Python. Send a screenshot of this window to Vishnu."; read -r; exit 1; }
  fi
fi

# ---------- 3) The app's parts (installs only what is missing) ----------
if ! .venv/bin/python -c "import fastapi, uvicorn, multipart, yaml, PIL" 2>/dev/null; then
  echo "  Installing the app (one time, ~2 min)…"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q fastapi "uvicorn[standard]" python-multipart pyyaml pillow \
    || { echo "  Install failed. Send a screenshot of this window to Vishnu."; read -r; exit 1; }
fi

# ---------- 4) ffmpeg (the video editor engine) ----------
for d in /opt/homebrew/bin /usr/local/bin; do [ -x "$d/ffprobe" ] && export PATH="$PATH:$d"; done
if ! command -v ffprobe >/dev/null 2>&1; then
  .venv/bin/python -c "import static_ffmpeg" 2>/dev/null || { echo "  Getting the video editor (one time)…"; .venv/bin/python -m pip install -q static-ffmpeg; }
  FFDIR=$(.venv/bin/python -c "import static_ffmpeg,shutil,os; static_ffmpeg.add_paths(); print(os.path.dirname(shutil.which('ffprobe') or ''))" 2>/dev/null | tail -1)
  [ -n "$FFDIR" ] && export PATH="$FFDIR:$PATH"
fi

# ---------- 5) Script AI (optional, uses Ollama if installed) ----------
if command -v ollama >/dev/null 2>&1; then (ollama serve >/dev/null 2>&1 &); fi

# ---------- 6) Start ----------
echo "  Starting Ghost Studio $(cat VERSION 2>/dev/null)… your browser opens in a moment."
( sleep 4; open "http://localhost:7860" ) &
.venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 7860 2>&1 | grep -v "GET /api\|GET /files\|GET /static"
echo ""
echo "  Ghost Studio stopped. If that was unexpected, send a screenshot of this window to Vishnu."
read -r
}

get_gh() {
  command -v gh >/dev/null 2>&1 && return
  echo "  Getting the GitHub helper (one time)…"
  A=amd64; [ "$(uname -m)" = "arm64" ] && A=arm64
  URL=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | grep -o "\"browser_download_url\": *\"[^\"]*macOS_${A}\.zip\"" | head -1 | sed 's/.*"\(https[^"]*\)"/\1/')
  [ -z "$URL" ] && return
  curl -fsSL "$URL" -o .ghost/gh.zip && (cd .ghost && unzip -oq gh.zip && cp gh_*_macOS_${A}/bin/gh bin/gh && chmod +x bin/gh && rm -rf gh.zip gh_*_macOS_${A})
}

main "$@"
exit
