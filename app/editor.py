"""Edit + export with ffmpeg in ONE encode (no generation loss).

Camera moves (same set as the browser studio): none, push_in, pull_out,
pan_left, pan_right, tilt_up, tilt_down, plus a natural handheld shake that
can be added on top of any move.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

W, H = 1080, 1920
MOVES = {
    "none": "No move",
    "push_in": "Slow push-in",
    "pull_out": "Slow pull-out",
    "pan_left": "Pan left",
    "pan_right": "Pan right",
    "tilt_up": "Tilt up",
    "tilt_down": "Tilt down",
}


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not installed. Run the installer again.")
    return exe


def probe(path: str | Path) -> dict:
    exe = shutil.which("ffprobe")
    out = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    st = d.get("streams", [])
    v = next((s for s in st if s.get("codec_type") == "video"), {})
    return {
        "duration": float(d.get("format", {}).get("duration", 0) or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in st),
        "width": v.get("width"), "height": v.get("height"),
    }


def camera_chain(move: str, dur: float, fps: int, amount: float = 1.0, shake: float = 0.0, W: int = W, H: int = H) -> str:
    """ffmpeg filter chain turning any input into a WxH shot with the camera move."""
    a = max(0.2, min(2.0, float(amount or 1)))
    frames = max(1, int(round(dur * fps)))
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}"
    parts = [cover, f"fps={fps}", "setsar=1"]
    if move in ("push_in", "pull_out"):
        z = 0.10 * a
        big = f"scale={int(W*1.5)}:{int(H*1.5)}:flags=lanczos"
        expr = f"1+{z:.4f}*on/{frames}" if move == "push_in" else f"1+{z:.4f}*(1-on/{frames})"
        parts += [big, f"zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}"]
    elif move in ("pan_left", "pan_right", "tilt_up", "tilt_down"):
        k = 1 + 0.12 * a
        bw, bh = int(W * k) // 2 * 2, int(H * k) // 2 * 2
        p = f"min(1,t/{dur:.3f})"
        if move == "pan_right":
            x, y = f"(iw-{W})*{p}", f"(ih-{H})/2"
        elif move == "pan_left":
            x, y = f"(iw-{W})*(1-{p})", f"(ih-{H})/2"
        elif move == "tilt_down":
            x, y = f"(iw-{W})/2", f"(ih-{H})*{p}"
        else:
            x, y = f"(iw-{W})/2", f"(ih-{H})*(1-{p})"
        parts += [f"scale={bw}:{bh}:flags=lanczos", f"crop={W}:{H}:x='{x}':y='{y}'"]
    if shake and shake > 0:
        s = max(0.1, min(2.0, float(shake)))
        k = 1 + 0.045 * s
        bw, bh = int(W * k) // 2 * 2, int(H * k) // 2 * 2
        mx, my = f"(iw-{W})/2", f"(ih-{H})/2"
        # three layered sine waves = organic hand sway, never mechanical
        wob = "(0.55*sin(2*PI*0.53*t)+0.30*sin(2*PI*1.37*t+1.1)+0.15*sin(2*PI*3.10*t+2.3))"
        wob2 = "(0.55*sin(2*PI*0.47*t+0.7)+0.30*sin(2*PI*1.21*t+2.0)+0.15*sin(2*PI*2.90*t+0.4))"
        parts += [f"scale={bw}:{bh}:flags=lanczos",
                  f"crop={W}:{H}:x='{mx}+{mx}*0.85*{wob}':y='{my}+{my}*0.85*{wob2}'"]
    parts += ["setsar=1", "format=yuv420p"]
    return ",".join(parts)


def render(clips: list[dict], out: str | Path, fps: int = 24, crf: int = 16,
           narration: list[dict] | None = None, music: str | None = None,
           music_volume: float = 0.18, log=None, size: tuple[int, int] = (W, H)) -> Path:
    """clips: [{video, duration?, move, amount, shake, mute, volume}]
    narration: [{file, at}] seconds from the start; music: path (ducked under speech).
    One ffmpeg run = one encode = no quality loss between steps."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    fc, vlabels, alabels = [], [], []
    t = 0.0
    starts = []
    for i, c in enumerate(clips):
        info = probe(c["video"])
        dur = float(c.get("duration") or info["duration"] or 1)
        args += ["-i", str(c["video"])]
        fc.append(f"[{i}:v]tpad=stop_mode=clone:stop_duration={dur:.3f},trim=0:{dur:.3f},setpts=PTS-STARTPTS,{camera_chain(c.get('move','none'), dur, fps, c.get('amount',1), c.get('shake',0), size[0], size[1])}[v{i}]")
        vol = 0.0 if c.get("mute") else float(c.get("volume", 1.0))
        if info["has_audio"]:
            fc.append(f"[{i}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={vol}[a{i}]")
        else:
            fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f}[a{i}]")
        vlabels.append(f"[v{i}][a{i}]")
        starts.append(t)
        t += dur
    n = len(clips)
    fc.append("".join(vlabels) + f"concat=n={n}:v=1:a=1[vout][speech]")
    total = t
    mix = ["[speech]"]
    idx = n
    for nar in narration or []:
        args += ["-i", str(nar["file"])]
        ms = int(float(nar.get("at", 0)) * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},volume={float(nar.get('volume',1.0))}[n{idx}]")
        mix.append(f"[n{idx}]")
        idx += 1
    if len(mix) > 1:
        fc.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first[voice]")
    else:
        fc.append("[speech]anull[voice]")
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume={music_volume}[mus]")
        fc.append("[voice]asplit=2[vo1][vo2]")
        fc.append("[mus][vo2]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]")
        fc.append("[vo1][duck]amix=inputs=2:normalize=0:duration=first[amix]")
        last = "[amix]"
    else:
        last = "[voice]"
    fc.append(f"{last}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    args += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-profile:v", "high",
             "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "256k",
             "-movflags", "+faststart", "-t", f"{total:.3f}", str(out)]
    if log:
        log(f"Rendering {n} clips, {total:.1f} s")
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-1500:])
    return out


def last_frame(video: str | Path, out_png: str | Path) -> Path:
    """Last frame of a clip, used to start the next clip so long scenes continue smoothly."""
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", str(video),
                    "-frames:v", "1", "-update", "1", str(out_png)], check=True)
    return Path(out_png)
