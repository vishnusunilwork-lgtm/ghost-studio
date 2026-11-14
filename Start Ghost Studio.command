#!/bin/bash
# Ghost Studio (Mac). The Ghost Studio app icon runs this file.
#   (no option)  open Ghost Studio: start the quiet background engine if needed, then open the browser
#   --service    the background engine: keeps the app running, restarts it after updates
#   --once       one run of the engine: update check, setup, start (used by --service)
# Your ads (projects/), your settings (config.local.yaml) and the installed parts (.venv/) are never touched.

REPO="vishnusunilwork-lgtm/ghost-studio"
PORT=7860

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")" || exit 1
mkdir -p .ghost/bin
export PATH="$PWD/.ghost/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
up() { curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/api/status"; }
say_status() { printf '%s' "$1" > .ghost/status.txt; echo "$(date '+%H:%M:%S') $1"; }

# ------------------------------------------------------------------ open (the icon)
open_app() {
  if ! up; then
    rm -f .ghost/stop
    [ -f .ghost/server.log ] && [ "$(wc -c < .ghost/server.log)" -gt 5000000 ] && mv -f .ghost/server.log .ghost/server.old.log
    ( nohup /bin/bash "$SELF" --service >> .ghost/server.log 2>&1 & ) >/dev/null 2>&1
    if [ -f launcher/starting.html ]; then
      open "file://$PWD/launcher/starting.html"
      exit 0
    fi
    osascript -e 'display notification "Starting… the first time takes about 5 minutes." with title "Ghost Studio"' >/dev/null 2>&1
    for _ in $(seq 1 900); do up && break; sleep 1; done
  fi
  open "http://localhost:$PORT"
}

# ------------------------------------------------------------------ the background engine
service() {
  fails=0
  while true; do
    [ -f .ghost/stop ] && { rm -f .ghost/stop; say_status "Stopped"; exit 0; }
    t0=$(date +%s)
    /bin/bash "$SELF" --once
    if [ $(( $(date +%s) - t0 )) -lt 20 ]; then fails=$((fails + 1)); else fails=0; fi
    if [ "$fails" -ge 5 ]; then say_status "Could not start. Open Ghost Studio again, or send .ghost/server.log to Vishnu."; exit 1; fi
    sleep 2
  done
}

newer() {  # true when version $1 is newer than $2 (3.10.0 > 3.9.2)
  local IFS=. i x y; local -a a=($1) b=($2)
  for i in 0 1 2; do
    x=${a[i]//[^0-9]/}; y=${b[i]//[^0-9]/}; x=${x:-0}; y=${y:-0}
    if ((10#$x > 10#$y)); then return 0; fi
    if ((10#$x < 10#$y)); then return 1; fi
  done
  return 1
}

once() {
  # 1) updates (public project, no sign-in)
  if [[ "$REPO" == */* ]]; then
    printf '%s' "$REPO" > .ghost/repo
    REMOTE=$(curl -fsSL --max-time 15 "https://raw.githubusercontent.com/$REPO/main/VERSION?$(date +%s)" 2>/dev/null | tr -d '[:space:]')
    LOCAL=$(cat VERSION 2>/dev/null | tr -d '[:space:]')
    # only when GitHub has a NEWER version (its version file can lag a few minutes behind)
    if [ -n "$REMOTE" ] && { newer "$REMOTE" "$LOCAL" || [ ! -f app/server.py ]; }; then
      say_status "Getting Ghost Studio $REMOTE…"
      rm -rf .ghost/new && mkdir -p .ghost/new
      if curl -fsSL --max-time 300 "https://github.com/$REPO/archive/refs/heads/main.tar.gz" -o .ghost/new.tgz && tar -xzf .ghost/new.tgz -C .ghost/new; then
        SRC=$(find .ghost/new -mindepth 1 -maxdepth 1 -type d | head -1)
        if [ -f "$SRC/app/server.py" ]; then
          cp -R "$SRC"/. ./
          chmod +x "Start Ghost Studio.command" 2>/dev/null
          rm -rf .ghost/new .ghost/new.tgz
          NOW=$(cat VERSION 2>/dev/null | tr -d '[:space:]')
          say_status "Updated to $NOW"
          [ "$NOW" != "$LOCAL" ] && exit 0   # the engine loop starts the new version right away
        fi
      fi
    fi
  fi
  [ -f app/server.py ] || { say_status "Could not download Ghost Studio. Check your internet."; exit 1; }

  # 2) Python 3.10+ (its own copy)
  if [ ! -x ".venv/bin/python" ]; then
    PY=""
    for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
      if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
    done
    say_status "Setting up (one time, about 5 minutes)…"
    if [ -z "$PY" ]; then
      curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$PWD/.ghost/bin" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
      uv venv --python 3.11 .venv >/dev/null 2>&1 && uv pip install --python .venv/bin/python pip >/dev/null 2>&1 \
        || { say_status "Could not get Python."; exit 1; }
    else
      "$PY" -m venv .venv || { say_status "Could not set up Python."; exit 1; }
    fi
  fi

  # 3) the app's parts
  if ! .venv/bin/python -c "import fastapi, uvicorn, multipart, yaml, PIL" 2>/dev/null; then
    say_status "Installing the app (one time, about 2 minutes)…"
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q fastapi "uvicorn[standard]" python-multipart pyyaml pillow || { say_status "Install failed."; exit 1; }
  fi

  # 4) ffmpeg
  if ! command -v ffprobe >/dev/null 2>&1; then
    .venv/bin/python -c "import static_ffmpeg" 2>/dev/null || { say_status "Getting the video editor (one time)…"; .venv/bin/python -m pip install -q static-ffmpeg; }
    FFDIR=$(.venv/bin/python -c "import static_ffmpeg,shutil,os; static_ffmpeg.add_paths(); print(os.path.dirname(shutil.which('ffprobe') or ''))" 2>/dev/null | tail -1)
    [ -n "$FFDIR" ] && export PATH="$FFDIR:$PATH"
  fi

  # 5) optional script AI
  if command -v ollama >/dev/null 2>&1 && ! curl -s --max-time 1 http://127.0.0.1:11434 >/dev/null; then (ollama serve >/dev/null 2>&1 &); fi

  # 6) start
  say_status "Ready"
  export GHOST_SERVICE=1 GHOST_PORT=$PORT
  exec .venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port $PORT --log-level warning
}

case "$1" in
  --service) service ;;
  --once) once ;;
  *) open_app ;;
esac
exit 0
