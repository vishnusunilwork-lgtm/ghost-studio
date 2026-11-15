"""Dubbing: one video in, the same video speaking another language out.

  1 listen      faster-whisper: every sentence with its start and end time, and the spoken language
  2 translate   script AI: natural spoken target language, about the same length so it fits the same moment
  3 voice       Natural voices (Indic Parler-TTS) or the built-in voice: one clip per sentence, model loaded once
  4 fit         each clip is sped up a little (never more than 1.45x) only if it is longer than its slot
  5 background  Song stems add-on: the original voice is removed and music/sounds kept; otherwise the
                original audio is turned down under the new voice
  6 mix         one ffmpeg pass: new voice clips at their times + background, video copied untouched
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .editor import ffmpeg_bin, probe

LANG_NAMES = {"ml": "Malayalam", "hi": "Hindi", "en": "English", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "mr": "Marathi",
              "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "ur": "Urdu", "or": "Odia", "as": "Assamese", "ne": "Nepali",
              "es": "Spanish", "fr": "French", "de": "German", "ar": "Arabic", "zh": "Chinese", "ja": "Japanese"}
MAX_SPEEDUP = 1.45


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + (r.stderr or "")[-300:])


def slots(segs: list[dict], total: float) -> list[float]:
    """How long each new line may last: until the next sentence starts (or a little past its own end)."""
    out = []
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
        out.append(max(0.6, min(nxt, s["end"] + 0.8) - s["start"]))
    return out


def fit(src: Path, dst: Path, slot: float) -> float:
    """Speed a clip up just enough to fit its slot (natural speech allows ~1.45x). Returns the final length."""
    have = probe(src)["duration"] or slot
    tempo = min(MAX_SPEEDUP, have / slot) if have > slot else 1.0
    _run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(src), "-af", f"atempo={tempo:.3f},aresample=48000", "-ac", "1", str(dst)])
    return have / tempo


def extract_audio(video: Path, out: Path) -> Path:
    _run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "2", "-ar", "44100", str(out)])
    return out


def mix(video: Path, background: Path | None, bg_volume: float, lines: list[dict], out: Path) -> Path:
    """lines: [{file, at}] seconds. Video stream is copied (no quality loss)."""
    args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    fc, labels, n = [], [], 1
    if background:
        args += ["-i", str(background)]
        fc.append(f"[{n}:a]volume={bg_volume:.2f},aresample=48000[bg]")
        labels.append("[bg]")
        n += 1
    for k, ln in enumerate(lines):
        args += ["-i", str(ln["file"])]
        ms = int(max(0.0, ln["at"]) * 1000)
        fc.append(f"[{n}:a]aresample=48000,adelay=delays={ms}:all=1[v{k}]")
        labels.append(f"[v{k}]")
        n += 1
    fc.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0:dropout_transition=0,alimiter=limit=0.95[a]")
    total = probe(video)["duration"]
    _run(args + ["-filter_complex", ";".join(fc), "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-t", f"{total:.3f}", str(out)])
    return out
