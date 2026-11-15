"""The makers. Ghost Studio picks the best one this computer has, step by step:

  images   : NVIDIA GPU  ->  Mac image AI (if installed)  ->  video service (if a key is added)
             ->  your own photos  ->  simple colour cards
  video    : video service (if a key is added)
             ->  "photo motion" (the image + your camera moves; works on every computer)
  narrator : Natural voices add-on (Indic Parler-TTS: Malayalam, Hindi, Tamil… described voices)
             ->  Mac / Windows built-in voices  ->  test tone

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
STOP = {"flag": False}


class Cancelled(BaseException):
    """Raised when you press Pause: the current job stops at the next safe point.
    BaseException on purpose: no 'except Exception' fallback may swallow a Pause."""


def check_stop():
    if STOP["flag"]:
        raise Cancelled("Paused")


def _step_cb(pipe, i, t, kw):  # diffusers callback: lets Pause stop a picture or clip mid-way
    check_stop()
    return kw
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
        from .llm import _ram_gb
        if _ram_gb() < 31:  # small memory: one big model at a time. 32 GB+: keep them all ready (no reloading)
            free()
        _LOADED[key] = loader()
    return _LOADED[key]


# Apple MLX keeps its compute streams per thread: a model made in one thread fails in another
# ("There is no Stream(cpu, 0) in current thread"). So every MLX load and every MLX picture runs on ONE thread.
_MLX_POOL = None


def _mlx(fn):
    global _MLX_POOL
    if _MLX_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _MLX_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
    return _MLX_POOL.submit(fn).result()


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

    def music(self, *, caption: str, lyrics: str, seconds: float, language: str, out_dir: Path, takes: int = 1,
              thinking: bool = True, bpm: int | None = None) -> list[dict]:
        """ACE-Step 1.5 in the cloud: the same music model, about a minute instead of 5-10 on a Mac."""
        secs = max(10, min(600, float(seconds)))
        cost = 0.0003 * secs * (2 if thinking else 1) * takes
        self.check_budget(cost)
        body = {"prompt": caption[:500], "lyrics": (lyrics or "[Instrumental]")[:4000], "duration": secs, "vocal_language": language or "unknown",
                "num_outputs": max(1, min(4, takes)), "thinking": thinking}
        if bpm:
            body["bpm"] = int(bpm)
        r = self.run("fal-ai/ace-step-1.5", body)
        out_dir.mkdir(parents=True, exist_ok=True)
        items = r.get("audios") or ([{"audio": r["audio"]}] if r.get("audio") else [])
        out = []
        for n, it in enumerate(items, 1):
            url = (it.get("audio") or {}).get("url")
            if not url:
                continue
            ext = ".flac" if "flac" in ((it.get("audio") or {}).get("content_type") or "") else ".mp3"
            f = out_dir / f"track{int(time.time())}_{n}{ext}"
            Fal.download(url, f)
            out.append({"file": str(f), "bpm": r.get("bpm"), "key": r.get("key_scale"), "seconds": r.get("duration"), "lyrics": lyrics, "caption": caption})
        Spend.add(cost)
        if not out:
            raise RuntimeError("The music service returned no audio")
        return out

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
            "image": "service" if (self.fal.on and not self.mock) else "nvidia" if cuda else "local" if local_img else "photos",
            "video": "service" if self.fal.on and not self.mock else "photo_motion",
            "voice": "natural" if (not self.mock and _voices_ready()) else "mac" if shutil.which("say") else "windows" if platform.system() == "Windows" else "tone",
        }

    @property
    def name(self) -> str:
        return "mock" if self.mock else "real" if has_cuda() else "mac" if platform.system() == "Darwin" else "basic"

    @property
    def talks(self) -> bool:
        """Can the video itself speak with lip-sync?"""
        return self.plan()["video"] == "service"

    # ------------------------------------------------------------ images
    def image(self, prompt: str, out: Path, width: int, height: int, seed: int = 0,
              ref_images: list[Path] | None = None, steps: int | None = None, fallback: Path | None = None, best: bool = False) -> Path:
        from . import knowledge
        prompt = knowledge.apply(prompt)  # Indian words spelled out ("desi dog" = Indian pariah dog) + what you taught Ghost
        how = self.plan()["image"]
        if best and not ref_images and not self.mock and not self.fal.on and _realism_ready() and (_mflux_ok() or has_cuda()):
            try:
                return self._zimage(prompt, out, width, height, seed)
            except Exception as e:
                print("Best pictures (Z-Image) failed, using the standard model:", e)
                free()
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
            refs = [r for r in (ref_images or []) if r and Path(r).exists()][:4]
            if refs:  # same person + place from the main look
                r = self.fal.run(self.fal.image_model + "/edit", {"prompt": prompt, "seed": seed, "image_urls": [Fal.data_uri(x, 1024) for x in refs],
                                                                  "image_size": {"width": width, "height": height}, "output_format": "png"})
            else:
                r = self.fal.run(self.fal.image_model, {"prompt": prompt, "seed": seed, "num_images": 1, "output_format": "png",
                                                        "image_size": {"width": width, "height": height}})
            Fal.download(r["images"][0]["url"], out)
            Spend.add(0.005 * width * height / 1e6)
            return out
        return self._card(prompt, out, width, height, seed, fallback)

    EDIT_KEEP = ("Edit this photo. Change only this: {ins}. Keep everything else exactly the same: the same person and face "
                 "(unless asked), the same framing and camera angle, the same place, light and colours, the same clothes unless asked. "
                 "Photorealistic, natural skin, correct hands and eyes.")

    def can_edit(self) -> bool:
        return self.mock or self.plan()["image"] in ("service", "nvidia", "local")

    def edit_image(self, src: Path, instruction: str, out: Path, seed: int = 0) -> Path:
        """Change the SAME picture by a plain-words instruction ("shorter hair", "make it a man")."""
        from PIL import Image
        src = Path(src)
        w, h = Image.open(src).size
        if self.mock:  # test mode: a visible tint so the change is obvious
            from PIL import ImageEnhance
            im = Image.open(src).convert("RGB")
            ImageEnhance.Color(im).enhance(0.4 + (seed % 7) / 5).save(out)
            return out
        if not self.can_edit():
            raise RuntimeError("Editing needs the Pictures add-on (⚙ → Add-ons) or a video-service key.")
        sc = min(1.0, 1440 / max(w, h))
        w, h = max(256, int(w * sc) // 16 * 16), max(256, int(h * sc) // 16 * 16)
        return self.image(self.EDIT_KEEP.format(ins=instruction.strip().rstrip(".")), out, w, h, seed=seed, ref_images=[src])

    def _local_image(self, prompt, out, width, height, seed, ref_images, steps, mac=False):
        if mac and _mflux_ok():
            try:
                return _mlx(lambda: self._mflux_image(prompt, out, width, height, seed, ref_images))
            except Exception as e:  # fall back to the slower PyTorch path for this picture only
                print("Fast pictures (MLX) failed, using PyTorch:", e)
                _MFLUX["fails"] = _MFLUX.get("fails", 0) + (0 if "Stream" in str(e) else 1)
                if _MFLUX["fails"] >= 3:
                    _MFLUX["broken"] = True
                free()
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
        kw["callback_on_step_end"] = _step_cb
        pipe(**kw).images[0].save(out)
        return out

    def _mflux_image(self, prompt, out, width, height, seed, ref_images):
        """Apple-chip native (MLX) FLUX.2 klein: same model files, about 3-4x faster than PyTorch on a Mac."""
        from mflux.models.common.config.model_config import ModelConfig
        q = int(self.cfg["quality"].get("mac_quantize", 8)) or None
        s = min(1.0, 1024 / max(width, height))
        w, h = int(width * s) // 16 * 16, int(height * s) // 16 * 16
        refs = [str(p) for p in (ref_images or []) if p and Path(p).exists()][:3]
        if refs:
            from mflux.models.flux2.variants.edit.flux2_klein_edit import Flux2KleinEdit
            model = _pipe("mflux_edit", lambda: Flux2KleinEdit(quantize=q, model_config=ModelConfig.flux2_klein_4b()))
            res = model.generate_image(seed=int(seed) % 2**31, prompt=prompt, num_inference_steps=4, width=w, height=h,
                                       guidance=1.0, image_paths=refs)
        else:
            from mflux.models.flux2.variants.txt2img.flux2_klein import Flux2Klein
            model = _pipe("mflux", lambda: Flux2Klein(quantize=q, model_config=ModelConfig.flux2_klein_4b()))
            res = model.generate_image(seed=int(seed) % 2**31, prompt=prompt, num_inference_steps=4, width=w, height=h, guidance=1.0)
        (res.image if hasattr(res, "image") else res).save(out)
        return out

    def _zimage(self, prompt, out, width, height, seed):
        """Z-Image-Turbo (Apache 2.0): the most realistic free model Ghost can run; 8 steps."""
        long_side = int(self.cfg["quality"].get("best_long_side", 1344))
        s = min(1.0, long_side / max(width, height))
        w, h = int(width * s) // 16 * 16, int(height * s) // 16 * 16
        if _mflux_ok():
            def run():
                from mflux.models.common.config.model_config import ModelConfig
                from mflux.models.z_image.variants.z_image import ZImage
                q = int(self.cfg["quality"].get("mac_quantize", 8)) or None
                m = _pipe("zimage", lambda: ZImage(quantize=q, model_config=ModelConfig.z_image_turbo()))
                img = m.generate_image(seed=int(seed) % 2**31, prompt=prompt, num_inference_steps=8, width=w, height=h)
                (img.image if hasattr(img, "image") else img).save(out)
                return out
            return _mlx(run)
        import torch

        def load():
            from diffusers import ZImagePipeline
            p = ZImagePipeline.from_pretrained("Tongyi-MAI/Z-Image-Turbo", torch_dtype=torch.bfloat16)
            if _device() == "cuda":
                p.enable_model_cpu_offload()
            else:
                p.to(_device())
            return p
        pipe = _pipe("zimage", load)
        pipe(prompt=prompt, height=h, width=w, num_inference_steps=9, guidance_scale=0.0, callback_on_step_end=_step_cb,
             generator=torch.Generator("cpu").manual_seed(int(seed) % 2**31)).images[0].save(out)
        return out

    def warm(self):
        """Load the picture model ahead of time so the first picture starts straight away."""
        if self.mock or self.plan()["image"] != "local":
            return
        try:
            if _mflux_ok():
                def load():
                    from mflux.models.common.config.model_config import ModelConfig
                    from mflux.models.flux2.variants.txt2img.flux2_klein import Flux2Klein
                    q = int(self.cfg["quality"].get("mac_quantize", 8)) or None
                    _pipe("mflux", lambda: Flux2Klein(quantize=q, model_config=ModelConfig.flux2_klein_4b()))
                _mlx(load)
        except Exception as e:
            print("warm-up skipped:", e)

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
        return self.photo_clip(image, seconds, out, width, height)

    def _photo_only(self, image, seconds, out, width, height):
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

    # ------------------------------------------------------------ narrator
    MAC_VOICES = {"Hindi": ["Lekha"], "English": ["Rishi", "Veena", "Samantha"], "Tamil": ["Vani"],
                  "Telugu": ["Geeta"], "Kannada": ["Soumya"], "Marathi": ["Lekha"], "Bengali": ["Piya"]}

    def speech(self, text: str, out: Path, lang: str = "Hindi", voice: str = "female", style: str = "warm") -> Path:
        how = self.plan()["voice"]
        if how == "natural":
            try:
                return self._natural(text, out, lang, voice, style)
            except Exception as e:  # never lose the ad: fall back to the built-in voice
                print("Natural voice failed, using the built-in voice:", e)
                how = "mac" if shutil.which("say") else "windows" if platform.system() == "Windows" else "tone"
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
        if how == "windows" and not self.mock:
            # Windows built-in voices (Hindi: "Microsoft Kalpana"/"Hemant" after adding Hindi speech in Settings > Time & language > Speech)
            tf = out.with_suffix(".txt")
            tf.write_text(text, encoding="utf-8")
            want = (self.cfg.get("models", {}).get("windows_voice") or "").strip()
            culture = {"Hindi": "hi-IN", "English": "en-IN", "Tamil": "ta-IN", "Telugu": "te-IN", "Marathi": "mr-IN",
                       "Bengali": "bn-IN", "Gujarati": "gu-IN", "Kannada": "kn-IN", "Malayalam": "ml-IN", "Punjabi": "pa-IN"}.get(lang, "hi-IN")
            ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$v=$s.GetInstalledVoices()|%{{$_.VoiceInfo}}; $w='{want}'; "
                  f"$p=if($w){{$v|?{{$_.Name -eq $w}}}}else{{$v|?{{$_.Culture.Name -eq '{culture}'}}}}; "
                  "if($p){$s.SelectVoice(($p|Select -First 1).Name)}; $s.Rate=0; "
                  f"$s.SetOutputToWaveFile('{out}'); $s.Speak((Get-Content -Raw -Encoding UTF8 '{tf}')); $s.Dispose()")
            _run(["powershell", "-NoProfile", "-Command", ps])
            tf.unlink(missing_ok=True)
            return out
        secs = max(1.5, len(text.split()) / 2.4)
        _run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=520:sample_rate=24000",
              "-af", "volume=0.2", "-t", f"{secs:.2f}", str(out)])
        return out

    def _natural(self, text, out, lang, voice, style):
        from . import addons
        dev = "cuda" if has_cuda() else "mps" if gpu_info().get("mps") else "cpu"
        addons.speak_natural([{"text": text, "lang": lang, "voice": voice, "style": style, "out": str(out)}], dev)
        return out

    def speak_many(self, lines: list[dict], lang: str, voice: str = "female", style: str = "warm") -> None:
        """Many lines in one go (dubbing): with Natural voices the model loads once. lines: [{text, out}]"""
        if self.plan()["voice"] == "natural":
            from . import addons
            dev = "cuda" if has_cuda() else "mps" if gpu_info().get("mps") else "cpu"
            try:
                addons.speak_natural([{**ln, "lang": lang, "voice": voice, "style": style} for ln in lines], dev)
                return
            except Exception as e:
                print("Natural voices failed, using the built-in voice:", e)
        for ln in lines:
            check_stop()
            self.speech(ln["text"], Path(ln["out"]), lang, voice, style)

    def transcribe_segments(self, media: Path) -> tuple[list[dict], str]:
        """Speech to text with timings (for dubbing): [{start, end, text}], detected language."""
        if self.mock or not _has("faster_whisper"):
            return [], ""
        from faster_whisper import WhisperModel
        size = self.cfg["models"]["whisper"] if has_cuda() else "small"
        m = _pipe("whisper", lambda: WhisperModel(size, device="cuda" if has_cuda() else "cpu", compute_type="float16" if has_cuda() else "int8"))
        segs, info = m.transcribe(_pcm16k(media), language=None, vad_filter=True)
        return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs if s.text.strip()], info.language

    def transcribe(self, media: Path) -> str:
        if self.mock or not _has("faster_whisper"):
            return ""
        from faster_whisper import WhisperModel
        size = self.cfg["models"]["whisper"] if has_cuda() else "small"
        m = _pipe("whisper", lambda: WhisperModel(size, device="cuda" if has_cuda() else "cpu", compute_type="float16" if has_cuda() else "int8"))
        segs, _ = m.transcribe(_pcm16k(media), language=None, vad_filter=True)
        return " ".join(s.text.strip() for s in segs)


_MFLUX: dict = {}


def _mflux_ok() -> bool:
    if _MFLUX.get("broken") or platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    return _has("mflux") and _has("mlx")


def _realism_ready() -> bool:
    try:
        from . import addons
        return addons.realism_installed()
    except Exception:
        return False


def _pcm16k(media: Path):
    """Decode any audio/video with ffmpeg (the one Ghost already uses) into what Whisper reads: 16 kHz mono floats.
    Avoids depending on the PyAV version installed alongside."""
    import numpy as np
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(media), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def _voices_ready() -> bool:
    try:
        from . import addons
        return addons.voices_installed()
    except Exception:
        return False


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def get_engines(cfg: dict) -> Engines:
    return Engines(cfg, force_mock=cfg.get("engine") == "mock")
