"""Offline check for talking pictures: a drawn face, a fake picture editor, a synthetic voice.
Run:  python3 -m tests.test_talk"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app import talk
from app.editor import ffmpeg_bin


class FakeEditor:
    """Draws the asked mouth shape / closed eyes on the same face, like the picture AI would."""
    def edit_image(self, src, prompt, out, seed=0):
        from PIL import Image, ImageDraw
        im = Image.open(src).convert("RGB")
        d = ImageDraw.Draw(im)
        if "eyes closed" in prompt:
            for x in (150, 250):
                d.ellipse((x - 22, 170, x + 22, 200), fill=(200, 150, 110))
                d.line((x - 20, 188, x + 20, 188), fill=(40, 30, 20), width=4)
        elif "'oo'" in prompt:
            d.ellipse((182, 290, 218, 330), fill=(60, 10, 10))
        elif "slightly" in prompt:
            d.ellipse((160, 300, 240, 318), fill=(60, 10, 10))
        else:
            d.ellipse((150, 285, 250, 345), fill=(60, 10, 10))
        im.save(out)
        return out


def face(path):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 560), (90, 140, 90))
    d = ImageDraw.Draw(im)
    d.ellipse((60, 60, 340, 420), fill=(200, 150, 110))
    for x in (150, 250):
        d.ellipse((x - 20, 170, x + 20, 200), fill=(255, 255, 255))
        d.ellipse((x - 8, 178, x + 8, 194), fill=(20, 20, 20))
    d.line((165, 310, 235, 310), fill=(90, 30, 30), width=5)
    im.save(path)


def voice(path):
    """'aa'-like bright sound, a pause, 'oo'-like dark sound, a pause."""
    parts = "sine=f=900:d=0.6,sine=f=0.1:d=0.4,sine=f=260:d=0.6,sine=f=0.1:d=0.6".split(",")
    filt = ";".join(f"[{i}:a]volume={'0.8' if 'f=0.1' not in p else '0'}[a{i}]" for i, p in enumerate(parts))
    cmd = [ffmpeg_bin(), "-y", "-v", "error"]
    for p in parts:
        cmd += ["-f", "lavfi", "-i", p + ":sample_rate=16000"]
    cmd += ["-filter_complex", filt + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1", str(path)]
    subprocess.run(cmd, check=True)


def test_talking():
    d = Path(tempfile.mkdtemp())
    face(d / "f.png")
    voice(d / "v.wav")
    got = {k: talk.variant(FakeEditor(), d / "f.png", d / f"{k}.png", k, "a man") for k in ("open", "half", "round", "blink")}
    assert all(got.values()), got
    loud, dark = talk.features(d / "v.wav", 24)
    seq = talk.shapes_for(loud, dark, len(loud), 0, True)
    assert talk.OPEN in seq[:14] and talk.ROUND in seq[24:38] and seq[-6:] == [talk.CLOSED] * 6, seq
    assert talk.blinks(240, 24, 1) and max(talk.blinks(240, 24, 1)) < 240
    out = talk.talk_clip(d / "f.png", got["open"], d / "v.wav", 2.4, d / "t.mp4", 24, 200, 280,
                         half=got["half"], round_=got["round"], blink=got["blink"], seed=3)
    probe = subprocess.run([ffmpeg_bin(), "-i", str(out), "-f", "null", "-"], capture_output=True, text=True).stderr
    assert "frame=" in probe and out.stat().st_size > 1000
    return d


if __name__ == "__main__":
    print("ok", test_talking())
