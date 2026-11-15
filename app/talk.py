"""Talking pictures (free mode, no video service): the character's mouth moves with its voice.

  1 mouth-open frame   the picture AI edits the SAME picture: "mouth open, mid-word, everything else identical"
  2 keep only the mouth the two pictures are compared; only the area that changed (the mouth) is blended in,
                        with a soft edge, so the face, fur, background and light never flicker
  3 lip timing         the voice's loudness is measured 24 times a second; loud = mouth open, quiet = closed,
                        with a little smoothing so it looks like syllables, not flicker
  4 clip               the frames become the shot's video; the editor then adds the camera move as usual
It is a puppet-style trick, not real video: real body action and lip-sync need the video service.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .editor import ffmpeg_bin


def mouth_frame(eng, closed: Path, out: Path, who: str = "the character") -> Path | None:
    """Make the mouth-open version of a picture. None if the edit changed too much (it would flicker)."""
    from PIL import Image, ImageChops, ImageFilter
    raw = out.with_name(out.stem + "_raw.png")
    eng.edit_image(closed, f"{who} has its mouth open mid-word as if talking, only the mouth and jaw move; the same pose, eyes, "
                   "fur or skin, clothes, background and light, exactly identical", raw, seed=7)
    a = Image.open(closed).convert("RGB")
    b = Image.open(raw).convert("RGB").resize(a.size, Image.LANCZOS)
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 38 else 0)
    diff = diff.filter(ImageFilter.MedianFilter(7)).filter(ImageFilter.MaxFilter(17)).filter(ImageFilter.GaussianBlur(9))
    hist = diff.histogram()
    changed = sum(hist[64:]) / float(a.size[0] * a.size[1])
    if changed < 0.002 or changed > 0.22:  # nothing changed, or the whole picture changed: not usable
        return None
    Image.composite(b, a, diff).save(out)
    return out


def envelope(wav: Path, fps: int) -> list[float]:
    """Loudness of the voice per video frame (0..1)."""
    import array
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(wav), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    pcm = array.array("h", raw)
    step = max(1, 16000 // fps)
    out = []
    for i in range(0, len(pcm), step):
        win = pcm[i:i + step]
        out.append((sum(x * x for x in win) / max(1, len(win))) ** 0.5 if win else 0.0)
    top = sorted(out)[int(len(out) * 0.9)] if out else 1.0
    return [min(1.0, v / top) if top else 0.0 for v in out]


def talk_clip(closed: Path, opened: Path, wav: Path, seconds: float, out: Path, fps: int, w: int, h: int, offset: float = 0.2) -> Path:
    """The shot as frames that switch between mouth closed and open with the voice."""
    env = envelope(wav, fps)
    n = max(1, int(round(seconds * fps)))
    start = int(round(offset * fps))
    state, run, frames = False, 0, []
    for k in range(n):
        j = k - start
        v = env[j] if 0 <= j < len(env) else 0.0
        want = v > 0.32
        if want != state and run >= 2:  # hold each state at least 2 frames (~80 ms): syllables, not flicker
            state, run = want, 0
        run += 1
        frames.append(state)
    segs, cur, length = [], frames[0], 0
    for f in frames:
        if f == cur:
            length += 1
        else:
            segs.append((cur, length))
            cur, length = f, 1
    segs.append((cur, length))
    lst = out.with_suffix(".txt")
    lines = []
    for is_open, length in segs:
        lines.append(f"file '{(opened if is_open else closed).resolve().as_posix()}'")
        lines.append(f"duration {length / fps:.4f}")
    lines.append(f"file '{(opened if segs[-1][0] else closed).resolve().as_posix()}'")
    lst.write_text("\n".join(lines))
    r = subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.2f}",
                        "-vf", f"fps={fps},scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * 2}:{h * 2},format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-c:a", "aac", "-shortest", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("talking clip: " + r.stderr[-300:])
    return out
