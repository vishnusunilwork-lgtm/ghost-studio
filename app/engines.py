"""The generators. Real ones run open models on this computer's NVIDIA GPU.
Mock ones make simple test media so the whole app can be tried on any laptop.

Only one big model is kept in GPU memory at a time: each engine loads on first
use and `free()` unloads it before the next stage starts.
"""
from __future__ import annotations

import gc
import math
import subprocess
from pathlib import Path

from .editor import ffmpeg_bin

_LOADED: dict = {}


def gpu_info() -> dict:
    try:
        import torch
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            return {"gpu": True, "name": p.name, "vram_gb": round(p.total_memory / 1024**3, 1)}
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return {"gpu": True, "name": "Apple GPU (slow for video)", "vram_gb": 0, "mps": True}
    except Exception:
        pass
    return {"gpu": False, "name": "No GPU found", "vram_gb": 0}


def free():
    _LOADED.clear()
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------- real engines
class RealEngines:
    name = "real"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _pipe(self, key, loader):
        if key not in _LOADED:
            free()
            _LOADED[key] = loader()
        return _LOADED[key]

    def image(self, prompt: str, out: Path, width: int, height: int, seed: int = 0, ref_images: list[Path] | None = None, steps: int | None = None) -> Path:
        import torch
        from PIL import Image
        mid = self.cfg["models"]["image"]

        def load():
            import diffusers
            cls = diffusers.Flux2KleinPipeline if "klein" in mid.lower() else (
                diffusers.QwenImagePipeline if "qwen" in mid.lower() else diffusers.FluxPipeline)
            p = cls.from_pretrained(mid, torch_dtype=torch.bfloat16)
            if _device() == "cuda":
                p.enable_model_cpu_offload()
            else:
                p.to(_device())
            return p
        pipe = self._pipe("image", load)
        kw = dict(prompt=prompt, width=width, height=height,
                  num_inference_steps=int(steps or self.cfg["quality"]["image_steps"]),
                  generator=torch.Generator("cpu").manual_seed(seed))
        if ref_images and "klein" in mid.lower():
            kw["image"] = [Image.open(p).convert("RGB") for p in ref_images]
        img = pipe(**kw).images[0]
        img.save(out)
        return out

    def video(self, image: Path, prompt: str, seconds: float, out: Path, seed: int = 0,
              width: int | None = None, height: int | None = None, steps: int | None = None) -> Path:
        """Image -> video WITH speech and lip-sync (LTX-2 makes the audio itself)."""
        import torch
        from diffusers.utils import encode_video, load_image
        q = self.cfg["quality"]
        mid = self.cfg["models"]["video"]

        def load():
            from diffusers import LTX2ImageToVideoPipeline
            p = LTX2ImageToVideoPipeline.from_pretrained(mid, torch_dtype=torch.bfloat16)
            if _device() == "cuda":
                p.enable_model_cpu_offload()
            else:
                p.to(_device())
            return p
        pipe = self._pipe("video", load)
        fps = float(q["fps"])
        frames = int(8 * round(seconds * fps / 8) + 1)
        video, audio = pipe(image=load_image(str(image)), prompt=prompt,
                            negative_prompt="worst quality, inconsistent motion, blurry, jittery, distorted face, extra fingers, text, subtitles, watermark, logo",
                            width=int(width or q["video_width"]), height=int(height or q["video_height"]), num_frames=frames,
                            frame_rate=fps, num_inference_steps=int(steps or q["video_steps"]), guidance_scale=3.0,
                            generator=torch.Generator("cpu").manual_seed(seed),
                            output_type="np", return_dict=False)
        encode_video(video[0], fps=int(fps), audio=audio[0].float().cpu(),
                     audio_sample_rate=pipe.vocoder.config.output_sampling_rate, output_path=str(out))
        return out

    def speech(self, text: str, out: Path, lang: str = "Hindi") -> Path:
        """Narrator voice with Kokoro (open, local)."""
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
        code = {"Hindi": "h", "English": "a"}.get(lang, "h")
        tts = self._pipe("tts:" + code, lambda: KPipeline(lang_code=code))
        chunks = [a for _, _, a in tts(text, voice=self.cfg["models"]["tts_voice"])]
        sf.write(str(out), np.concatenate(chunks), 24000)
        return out

    def transcribe(self, media: Path) -> str:
        from faster_whisper import WhisperModel
        m = self._pipe("whisper", lambda: WhisperModel(self.cfg["models"]["whisper"], device="cuda" if _device() == "cuda" else "cpu"))
        segs, _ = m.transcribe(str(media), language=None, vad_filter=True)
        return " ".join(s.text.strip() for s in segs)


# ---------------------------------------------------------------- mock engines
class MockEngines:
    """Fake but valid media, so the app, the editor and the export can be tried without a GPU."""
    name = "mock"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def image(self, prompt, out, width, height, seed=0, ref_images=None, steps=None):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (width, height), (30 + seed * 37 % 120, 40, 80 + seed * 53 % 120))
        d = ImageDraw.Draw(img)
        y = 40
        for line in [prompt[i:i + 38] for i in range(0, min(len(prompt), 380), 38)]:
            d.text((30, y), line, fill=(255, 220, 120))
            y += 18
        img.save(out)
        return out

    def video(self, image, prompt, seconds, out, seed=0, width=None, height=None, steps=None):
        q = self.cfg["quality"]
        w, h = int(width or q["video_width"]), int(height or q["video_height"])
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-loop", "1", "-i", str(image),
                        "-f", "lavfi", "-i", f"sine=frequency={220 + seed * 40}:sample_rate=24000",
                        "-t", f"{seconds:.2f}", "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},zoompan=z='1+0.0015*on':d=1:s={w}x{h}:fps={q['fps']}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)], check=True)
        return out

    def speech(self, text, out, lang="Hindi"):
        secs = max(1.5, len(text.split()) / 2.4)
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=520:sample_rate=24000",
                        "-t", f"{secs:.2f}", str(out)], check=True)
        return out

    def transcribe(self, media):
        return ""


def get_engines(cfg: dict):
    want = cfg.get("engine", "auto")
    if want == "mock":
        return MockEngines(cfg)
    if want == "real":
        return RealEngines(cfg)
    g = gpu_info()
    return RealEngines(cfg) if g["gpu"] and not g.get("mps") else MockEngines(cfg)
