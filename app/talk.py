"""Talking pictures (free mode, no video service): the character's mouth moves with its voice.

  1 mouth shapes       the picture AI edits the SAME picture into a few mouth shapes:
                         open  ("aa", always)        half (lips parted)        round ("oo"/"o")
                       plus one with the eyes closed, for natural blinks. Fast mode makes open + blink;
                       Quality also makes half + round.
  2 keep only the part each pair of pictures is compared; only the area that changed (mouth, or eyelids) is
     that changed        blended in with a soft edge, so face, fur, background and light never flicker
  3 which shape when   the voice is measured 24× a second: how loud (how wide the mouth opens) and how "dark"
                       the sound is (o/u vowels are darker than a/e, so the lips round). Each shape is held at
                       least 2 frames and every change passes through a half-way frame, so it reads as
                       syllables, not flicker
  4 blinks             every 2–4.5 s, a 4-frame blink (half, closed, closed, half)
  5 clip               frames are streamed to ffmpeg; the editor then adds the camera move as usual
Works for animals and people. It is still a puppet-style trick: real body action needs the video service.
(Neural lip-sync models such as LatentSync / MuseTalk only work on human faces and need a big GPU.)
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

from .editor import ffmpeg_bin

KEEP = "; the same pose, eyes, fur or skin, clothes, background and light, exactly identical"
SHAPES = {
    "open": ("{who} has its mouth open mid-word as if saying 'aa', only the mouth and jaw move" + KEEP, 0.22),
    "half": ("{who} has its lips slightly parted mid-word, mouth just a little open, only the lips move" + KEEP, 0.18),
    "round": ("{who} has its lips pushed forward into a small round 'oo' shape as if saying 'oo', only the lips move" + KEEP, 0.18),
    "blink": ("{who} has both eyes closed in a natural blink, only the eyelids move; the same pose, mouth, "
              "fur or skin, clothes, background and light, exactly identical", 0.10),
}
CLOSED, HALF, OPEN, ROUND = 0, 1, 2, 3


def _mask(a, b):
    """Soft mask of where picture b differs from picture a, and the changed share of the picture."""
    from PIL import ImageChops, ImageFilter
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 38 else 0)
    diff = diff.filter(ImageFilter.MedianFilter(7)).filter(ImageFilter.MaxFilter(17)).filter(ImageFilter.GaussianBlur(9))
    hist = diff.histogram()
    return diff, sum(hist[64:]) / float(a.size[0] * a.size[1])


def variant(eng, closed: Path, out: Path, kind: str = "open", who: str = "the character") -> Path | None:
    """One edited version of the picture (a mouth shape or a blink), with only the changed area kept.
    None if the edit changed nothing, or too much (it would flicker)."""
    from PIL import Image
    prompt, limit = SHAPES[kind]
    raw = out.with_name(out.stem + "_raw.png")
    eng.edit_image(closed, prompt.format(who=who), raw, seed={"open": 7, "half": 11, "round": 13, "blink": 17}[kind])
    a = Image.open(closed).convert("RGB")
    b = Image.open(raw).convert("RGB").resize(a.size, Image.LANCZOS)
    m, changed = _mask(a, b)
    if changed < 0.002 or changed > limit:
        return None
    Image.composite(b, a, m).save(out)
    return out


def mouth_frame(eng, closed: Path, out: Path, who: str = "the character") -> Path | None:
    """The mouth-open version (kept for older callers)."""
    return variant(eng, closed, out, "open", who)


def features(wav: Path, fps: int) -> tuple[list[float], list[float]]:
    """Per video frame: loudness (0..1) and darkness (0..1, high for o/u-like sounds)."""
    import array
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(wav), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    pcm = array.array("h", raw)
    step = max(1, 16000 // fps)
    loud, cent = [], []
    try:
        import numpy as np
        x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32)
        win = np.hanning(step)
        freqs = np.fft.rfftfreq(step, 1 / 16000)
        band = (freqs > 90) & (freqs < 4000)
        for i in range(0, len(x), step):
            seg = x[i:i + step]
            loud.append(float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0)
            if len(seg) == step:
                spec = np.abs(np.fft.rfft(seg * win))[band]
                cent.append(float((spec * freqs[band]).sum() / (spec.sum() + 1e-6)))
            else:
                cent.append(0.0)
    except ImportError:  # no numpy: loudness only (no rounded lips)
        for i in range(0, len(pcm), step):
            w = pcm[i:i + step]
            loud.append((sum(v * v for v in w) / max(1, len(w))) ** 0.5 if w else 0.0)
            cent.append(0.0)
    top = sorted(loud)[int(len(loud) * 0.9)] if loud else 1.0
    loud = [min(1.0, v / top) if top else 0.0 for v in loud]
    voiced = sorted(c for c, v in zip(cent, loud) if v > 0.35 and c > 0)
    med = voiced[len(voiced) // 2] if voiced else 0.0
    dark = [max(0.0, min(1.0, (med - c) / (0.45 * med))) if med and c else 0.0 for c in cent]
    return loud, dark


def shapes_for(loud: list[float], dark: list[float], n: int, start: int, has_round: bool) -> list[int]:
    """Mouth shape per frame, held at least 2 frames."""
    out, state, run = [], CLOSED, 0
    for k in range(n):
        j = k - start
        v = loud[j] if 0 <= j < len(loud) else 0.0
        d = dark[j] if 0 <= j < len(dark) else 0.0
        want = CLOSED if v < 0.2 else HALF if v < 0.48 else (ROUND if has_round and d > 0.55 else OPEN)
        if want != state and run >= 2:
            state, run = want, 0
        run += 1
        out.append(state)
    return out


def blinks(n: int, fps: int, seed: int) -> dict[int, float]:
    """Frame -> how closed the eyes are (0..1). A blink every 2–4.5 s."""
    rnd, out = random.Random(seed), {}
    t = rnd.uniform(0.6, 2.0)
    while int(t * fps) + 4 < n:
        k = int(t * fps)
        for i, w in enumerate((0.5, 1.0, 1.0, 0.5)):
            out[k + i] = w
        t += rnd.uniform(2.0, 4.5)
    return out


def talk_clip(closed: Path, opened: Path, wav: Path, seconds: float, out: Path, fps: int, w: int, h: int,
              offset: float = 0.2, half: Path | None = None, round_: Path | None = None, blink: Path | None = None,
              seed: int = 0) -> Path:
    """The shot as frames whose mouth follows the voice (and eyes blink), streamed straight into ffmpeg."""
    from PIL import Image
    base = Image.open(closed).convert("RGB")
    size = (base.size[0] // 2 * 2, base.size[1] // 2 * 2)
    base = base.crop((0, 0) + size)

    def load(p):
        return Image.open(p).convert("RGB").resize(base.size, Image.LANCZOS) if p and Path(p).exists() else None
    op = load(opened)
    look = {CLOSED: base, OPEN: op, HALF: load(half) or Image.blend(base, op, 0.5), ROUND: load(round_)}
    eyes = load(blink)
    eye_mask = _mask(base, eyes)[0] if eyes else None

    loud, dark = features(wav, fps)
    n = max(1, int(round(seconds * fps)))
    seq = shapes_for(loud, dark, n, int(round(offset * fps)), look[ROUND] is not None)
    shut = blinks(n, fps, seed) if eye_mask is not None else {}

    cache: dict = {}

    def frame(k):
        s, prev = seq[k], seq[k - 1] if k else seq[0]
        key = (prev, s) if (k and prev != s) else (s, s)  # a change shows one half-way frame first
        if key not in cache:
            cache[key] = look[s] if key[0] == key[1] else Image.blend(look[key[0]], look[key[1]], 0.5)
        im = cache[key]
        if k in shut:
            im = Image.composite(eyes, im, eye_mask.point(lambda v, a=shut[k]: int(v * a)))
        return im.tobytes()

    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
           "-r", str(fps), "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.2f}",
           "-vf", f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * 2}:{h * 2},format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-c:a", "aac", "-shortest", str(out)]
    pr = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for k in range(n):
            pr.stdin.write(frame(k))
        pr.stdin.close()
    except BrokenPipeError:
        pass
    err = pr.stderr.read().decode(errors="ignore")
    if pr.wait() != 0:
        raise RuntimeError("talking clip: " + err[-300:])
    return out
