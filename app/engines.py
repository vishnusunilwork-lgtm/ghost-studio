"""The makers. Ghost Studio picks the best one this computer has, step by step:

  images   : NVIDIA GPU  ->  Mac image AI (if installed)  ->  video service (if a key is added)
             ->  your own photos  ->  simple colour cards
  video    : video service (if a key is added)  ->  NVIDIA GPU (LTX-2)
             ->  "photo motion" (the image + your camera moves; works on every computer)
  narrator : Mac built-in voices (free, offline)  ->  Kokoro (if installed)  ->  test tone

No key is needed. A key only unlocks real moving video with speech.
"""
from __future__ import annotations

import base64
import gc
import io
import json
import platform
import random
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .editor import ffmpeg_bin

_LOADED: dict = {}
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- what this computer has
def gpu_info() -> dict:
    try:
        import torch
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            return {"gpu": True, "name": p.name, "vram_gb": round(p.total_memory / 1024**3, 1)}
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return {"gpu": True, "name": "Apple " + platform.machine(), "vram_gb": 0, "mps": True}
    except Exception:
        pass
    if platform.system() == "Darwin":
        return {"gpu": False, "name": "Mac", "vram_gb": 0, "mac": True}
    return {"gpu": False, "name": "No GPU found", "vram_gb": 0}


def has_cuda() -> bool:
    g = gpu_info()
    return bool(g["gpu"] and not g.get("mps"))


def image_ai_installed() -> bool:
    try:
        import diffusers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def free():
    _LOADED.clear()
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def _device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _pipe(key, loader):
    if key not in _LOADED:
        free()
        _LOADED[key] = loader()
    return _LOADED[key]


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])


# ---------------------------------------------------------------- optional video service (fal.ai)
class Spend:
    """Keeps this month's video-service spending on this computer, so it can stop at the limit."""
    f = ROOT / ".ghost" / "spend.json"

    @classmethod
    def month(cls) -> dict:
        try:
            d = json.loads(cls.f.read_text())
        except Exception:
            d = {}
        m = time.strftime("%Y-%m")
        return d if d.get("month") == m else {"month": m, "usd": 0.0, "items": 0}

    @classmethod
    def add(cls, usd: float):
        d = cls.month()
        d["usd"] = round(d["usd"] + usd, 4)
        d["items"] += 1
        cls.f.parent.mkdir(exist_ok=True)
        cls.f.write_text(json.dumps(d))


class Fal:
    def __init__(self, cfg: dict):
        a = cfg.get("api") or {}
        self.key = (a.get("fal_key") or "").strip()
        self.video_model = a.get("video_model") or "fal-ai/veo3.1/lite/image-to-video"
        self.image_model = a.get("image_model") or "fal-ai/flux-2/klein/4b"
        self.resolution = a.get("resolution") or "720p"
        self.price = float(a.get("price_per_second") or 0.05)
        self.limit = float(a.get("monthly_limit_usd") or 20)
        self.base = (a.get("base_url") or "https://queue.fal.run/").rstrip("/") + "/"

    @property
    def on(self) -> bool:
        return bool(self.key)

    def _req(self, url, body=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Key " + self.key, "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def run(self, model: str, body: dict, timeout: int = 900) -> dict:
        sub = self._req(self.base + model, body)
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self._req(sub["status_url"])
            if st.get("status") == "COMPLETED":
                if st.get("error"):
                    raise RuntimeError("Video service: " + str(st["error"]))
                return self._req(sub["response_url"])
            time.sleep(4)
        raise RuntimeError("Video service took too long")

    def check_budget(self, usd: float):
        m = Spend.month()
        if m["usd"] + usd > self.limit:
            raise RuntimeError(f"Monthly video limit reached (${m['usd']:.2f} of ${self.limit:.0f}). "
                               "Raise it under the gear, or switch to photo motion.")

    @staticmethod
    def data_uri(img: Path, long_side: int = 1280) -> str:
        from PIL import Image
        im = Image.open(img).convert("RGB")
        im.thumbnail((long_side, long_side))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()

    @staticmethod
    def download(url: str, out: Path):
        with urllib.request.urlopen(url, timeout=300) as r, open(out, "wb") as f:
            shutil.copyfileobj(r, f)


# ---------------------------------------------------------------- the engine
class Engines:
    def __init__(self, cfg: dict, force_mock: bool = False):
        self.cfg = cfg
        self.mock = force_mock
        self.fal = Fal(cfg)

    # which maker is used for each step (shown in the app)
    def plan(self) -> dict:
        cuda = not self.mock and has_cuda()
        local_img = not self.mock and image_ai_installed() and (cuda or gpu_info().get("mps"))
        return {
            "image": "nvidia" if cuda else "local" if local_img else "service" if self.fal.on else "photos",
            "video": "service" if self.fal.on and not self.mock else "nvidia" if cuda else "photo_motion",
            "voice": "mac" if shutil.which("say") else "kokoro" if _has("kokoro") else "tone",
        }

    @property
    def name(self) -> str:
        return "mock" if self.mock else "real" if has_cuda() else "mac" if platform.system() == "Darwin" else "basic"

    @property
    def talks(self) -> bool:
        """Can the video itself speak with lip-sync?"""
        return self.plan()["video"] in ("service", "nvidia")

    # ------------------------------------------------------------ images
    def image(self, prompt: str, out: Path, width: int, height: int, seed: int = 0,
              ref_images: list[Path] | None = None, steps: int | None = None, fallback: Path | None = None) -> Path:
        how = self.plan()["image"]
        if how in ("nvidia", "local"):
            try:
                return self._local_image(prompt, out, width, height, seed, ref_images, steps, mac=how == "local")
            except Exception as e:
                if not self.fal.on:
                    raise RuntimeError("Image AI failed on this computer (" + str(e)[:200] + "). "
                                       "Try Draft mode, or add your own photos.")
        if how == "service" or (how in ("nvidia", "local") and self.fal.on):
            sc = min(1.0, 1440 / max(width, height))
            width, height = int(width * sc) // 16 * 16, int(height * sc) // 16 * 16
            r = self.fal.run(self.fal.image_model, {"prompt": prompt, "seed": seed, "num_images": 1, "output_format": "png",
                                                    "image_size": {"width": width, "height": height}})
            Fal.download(r["images"][0]["url"], out)
            Spend.add(0.005 * width * height / 1e6)
            return out
        return self._card(prompt, out, width, height, seed, fallback)

    def _local_image(self, prompt, out, width, height, seed, ref_images, steps, mac=False):
        import torch
        from PIL import Image
        mid = self.cfg["models"]["image_mac" if mac else "image"]

        def load():
            import diffusers
            cls = diffusers.Flux2KleinPipeline if "klein" in mid.lower() else diffusers.AutoPipelineForText2Image
            p = cls.from_pretrained(mid, torch_dtype=torch.bfloat16)
            if _device() == "cuda":
                p.enable_model_cpu_offload()
            else:
                p.to(_device())
                if hasattr(p, "enable_attention_slicing"):
                    p.enable_attention_slicing()
            return p
        pipe = _pipe("image:" + mid, load)
        if mac:  # Macs: smaller frames, few steps (the distilled model needs only 4)
            s = min(1.0, 1024 / max(width, height))
            width, height = int(width * s) // 16 * 16, int(height * s) // 16 * 16
            steps = int(self.cfg["quality"].get("image_steps_mac", 4))
        kw = dict(prompt=prompt, width=width, height=height, generator=torch.Generator("cpu").manual_seed(seed),
                  num_inference_steps=int(steps or self.cfg["quality"]["image_steps"]))
        if ref_images and "klein" in mid.lower():
            kw["image"] = [Image.open(p).convert("RGB") for p in ref_images]
        pipe(**kw).images[0].save(out)
        return out

    def _card(self, prompt, out, width, height, seed, photo: Path | None):
        """No image AI: use the person's own photo, else a soft colour card (honest placeholder)."""
        from PIL import Image, ImageDraw, ImageFilter, ImageOps
        if photo and Path(photo).exists():
            ImageOps.fit(Image.open(photo).convert("RGB"), (width, height), Image.LANCZOS).save(out)
            return out
        rnd = random.Random(seed)
        base = [(48, 22, 70), (70, 30, 20), (16, 40, 66), (60, 16, 40), (24, 50, 40)][seed % 5]
        img = Image.new("RGB", (width, height), base)
        d = ImageDraw.Draw(img)
        for _ in range(9):
            r = rnd.randint(width // 6, width // 2)
            x, y = rnd.randint(0, width), rnd.randint(0, height)
            c = tuple(min(255, v + rnd.randint(40, 140)) for v in base)
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        img = img.filter(ImageFilter.GaussianBlur(width / 9))
        img.save(out)
        return out

    # ------------------------------------------------------------ video
    def video(self, image: Path, prompt: str, seconds: float, out: Path, seed: int = 0,
              width: int | None = None, height: int | None = None, steps: int | None = None, aspect: str = "9:16") -> Path:
        how = self.plan()["video"]
        if how == "service":
            dur = 4 if seconds <= 4 else 6 if seconds <= 6 else 8
            cost = dur * self.fal.price
            self.fal.check_budget(cost)
            r = self.fal.run(self.fal.video_model, {
                "prompt": prompt, "image_url": Fal.data_uri(image), "duration": f"{dur}s",
                "aspect_ratio": "16:9" if aspect == "16:9" else "9:16", "resolution": self.fal.resolution,
                "generate_audio": True, "seed": int(seed) % 2**31,
                "negative_prompt": "text, subtitles, watermark, logo, distorted face, extra fingers"})
            Fal.download(r["video"]["url"], out)
            Spend.add(cost)
            return out
        if how == "nvidia":
            return self._ltx(image, prompt, seconds, out, seed, width, height, steps)
        return self.photo_clip(image, seconds, out, width, height)

    def photo_clip(self, image, seconds, out, width=None, height=None):
        """'Photo motion': the still frame as a clip. The editor's camera move brings it to life."""
        q = self.cfg["quality"]
        w, h = int(width or q["video_width"]), int(height or q["video_height"])
        fps = int(q["fps"])
        _run([ffmpeg_bin(), "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(fps), "-i", str(image),
              "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.2f}",
              "-vf", f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * 2}:{h * 2},format=yuv420p",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-c:a", "aac", "-shortest", str(out)])
        return out

    def _ltx(self, image, prompt, seconds, out, seed, width, height, steps):
        import torch
        from diffusers.utils import encode_video, load_image
        q = self.cfg["quality"]
        mid = self.cfg["models"]["video"]

        def load():
            from diffusers import LTX2ImageToVideoPipeline
            p = LTX2ImageToVideoPipeline.from_pretrained(mid, torch_dtype=torch.bfloat16)
            p.enable_model_cpu_offload()
            return p
        pipe = _pipe("video", load)
        fps = float(q["fps"])
        frames = int(8 * round(seconds * fps / 8) + 1)
        video, audio = pipe(image=load_image(str(image)), prompt=prompt,
                            negative_prompt="worst quality, inconsistent motion, blurry, jittery, distorted face, extra fingers, text, subtitles, watermark, logo",
                            width=int(width or q["video_width"]), height=int(height or q["video_height"]), num_frames=frames,
                            frame_rate=fps, num_inference_steps=int(steps or q["video_steps"]), guidance_scale=3.0,
                            generator=torch.Generator("cpu").manual_seed(seed), output_type="np", return_dict=False)
        encode_video(video[0], fps=int(fps), audio=audio[0].float().cpu(),
                     audio_sample_rate=pipe.vocoder.config.output_sampling_rate, output_path=str(out))
        return out

    # ------------------------------------------------------------ narrator
    MAC_VOICES = {"Hindi": ["Lekha"], "English": ["Rishi", "Veena", "Samantha"], "Tamil": ["Vani"],
                  "Telugu": ["Geeta"], "Kannada": ["Soumya"], "Marathi": ["Lekha"], "Bengali": ["Piya"]}

    def speech(self, text: str, out: Path, lang: str = "Hindi") -> Path:
        how = self.plan()["voice"]
        if how == "mac" and not self.mock:
            have = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
            pick = (self.cfg.get("models", {}).get("mac_voice") or "").strip()
            if not pick:
                pick = next((v for v in self.MAC_VOICES.get(lang, []) if v + " " in have), "")
            aiff = out.with_suffix(".aiff")
            _run(["say"] + (["-v", pick] if pick else []) + ["-r", "175", "-o", str(aiff), text])
            _run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "48000", "-ac", "1", str(out)])
            aiff.unlink(missing_ok=True)
            return out
        if how == "kokoro" and not self.mock:
            import numpy as np
            import soundfile as sf
            from kokoro import KPipeline
            code = {"Hindi": "h", "English": "a"}.get(lang, "h")
            tts = _pipe("tts:" + code, lambda: KPipeline(lang_code=code))
            sf.write(str(out), np.concatenate([a for _, _, a in tts(text, voice=self.cfg["models"]["tts_voice"])]), 24000)
            return out
        secs = max(1.5, len(text.split()) / 2.4)
        _run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=520:sample_rate=24000",
              "-af", "volume=0.2", "-t", f"{secs:.2f}", str(out)])
        return out

    def transcribe(self, media: Path) -> str:
        if self.mock or not _has("faster_whisper"):
            return ""
        from faster_whisper import WhisperModel
        m = _pipe("whisper", lambda: WhisperModel(self.cfg["models"]["whisper"], device="cuda" if has_cuda() else "cpu"))
        segs, _ = m.transcribe(str(media), language=None, vad_filter=True)
        return " ".join(s.text.strip() for s in segs)


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def get_engines(cfg: dict) -> Engines:
    return Engines(cfg, force_mock=cfg.get("engine") == "mock")
