"""Read a reference ad: camera cuts, key frames, and what is said."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .editor import ffmpeg_bin, probe


def scene_cuts(video: Path, threshold: float = 0.32) -> list[float]:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(video), "-vf",
                        f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def key_frames(video: Path, outdir: Path, max_frames: int = 8) -> list[dict]:
    """One frame from the middle of every scene (at most max_frames)."""
    outdir.mkdir(parents=True, exist_ok=True)
    dur = probe(video)["duration"] or 1
    cuts = [0.0] + [c for c in scene_cuts(video) if 0.4 < c < dur - 0.3] + [dur]
    scenes = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1) if cuts[i + 1] - cuts[i] > 0.25]
    if len(scenes) > max_frames:
        step = len(scenes) / max_frames
        scenes = [scenes[int(i * step)] for i in range(max_frames)]
    out = []
    for i, (a, b) in enumerate(scenes):
        t = (a + b) / 2
        p = outdir / f"scene{i + 1}.jpg"
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=-2:768", "-q:v", "3", str(p)], check=True)
        out.append({"scene": i + 1, "start": round(a, 2), "end": round(b, 2), "frame": str(p)})
    return out
