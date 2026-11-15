"""AI motion worker (runs in its own Python 3.12, see addons.make_motion).

Real moving video on an Apple-chip Mac with FastMetal-5B-QAD (Hao AI Lab's FastVideo, Apache 2.0):
Wan2.2-TI2V-5B (Apache 2.0) distilled to 3 denoising steps and quantised to INT8 for MLX.
Text to video, about 5 s of 24 fps video per shot, no sound (Ghost adds voice and music).

The same steps as FastVideo's examples/inference/basic/mlx_wan22_generate.py, but the text encoder and the video
model load ONCE for every shot of the ad instead of once per shot.

stdin:  {"src": FastVideo checkout, "model": FastMetal-5B-QAD folder,
         "shots": [{"prompt", "out", "seconds", "seed"}], "width", "height", "fast": bool}
        or {"download": true, "model": folder}
stdout: one JSON line per event: {"progress": "...", "i": n} ... and last {"ok": true, "files": [...]} or {"error": "..."}
"""
import json
import sys
import time
import traceback
from pathlib import Path

REPO = "FastVideo/FastMetal-5B-QAD"


def say(**kw):
    print(json.dumps(kw), flush=True)


def encode_all(model_root: Path, prompts: list[str]):
    """UMT5 text encoder on the CPU (the safe choice beside the MLX video model), loaded once for all prompts."""
    import torch
    from transformers import AutoTokenizer, UMT5EncoderModel
    tok = AutoTokenizer.from_pretrained(model_root / "tokenizer", local_files_only=True)
    enc = UMT5EncoderModel.from_pretrained(model_root / "text_encoder", torch_dtype=torch.float16,
                                           low_cpu_mem_usage=True, local_files_only=True).to("cpu").eval()
    out = []
    for p in prompts:
        t = tok([p], padding="max_length", max_length=512, truncation=True, add_special_tokens=True,
                return_attention_mask=True, return_tensors="pt")
        with torch.no_grad():
            e = enc(t.input_ids, t.attention_mask).last_hidden_state.to(torch.float16)
        n = int(t.attention_mask.gt(0).sum())
        e = torch.cat([e[0, :n], e.new_zeros(512 - n, e.size(2))])[None]
        out.append(e.float().numpy())
    del enc, tok
    return out


def main():
    job = json.loads(sys.stdin.read() or "{}")
    model = Path(job["model"])
    if job.get("download"):
        from huggingface_hub import snapshot_download
        snapshot_download(REPO, local_dir=str(model))
        say(ok=True)
        return
    sys.path.insert(0, str(Path(job["src"])))
    import mlx.core as mx
    import numpy as np
    import torch

    from examples.inference.basic.mlx_wan_prompt_to_video import _postprocess_video, make_rotary_embeddings
    from fastvideo.mlx_runtime.memory import cleanup_mlx
    from fastvideo.mlx_runtime.refine import plan_refine_resolutions
    from fastvideo.mlx_runtime.rife_interp import aligned_keyframe_count
    from fastvideo.mlx_runtime.wan22 import mlx_wan22_dit_from_mlx_checkpoint
    from fastvideo.mlx_runtime.wan22_sample import sample_wan22_dmd
    from fastvideo.mlx_runtime.wan_vae import decode_latents_to_video

    shots = job["shots"]
    w, h, fast = int(job["width"]), int(job["height"]), bool(job.get("fast"))
    t0 = time.time()
    say(progress="Reading the shot descriptions", i=0)
    embeds = encode_all(model, [s["prompt"] for s in shots])
    say(progress="Loading the motion model", i=0, encode_s=round(time.time() - t0, 1))
    config = json.loads((model / "mlx_dit.json").read_text())["config"]
    dit = mlx_wan22_dit_from_mlx_checkpoint(model, compile=False)
    in_ch = int(config["in_channels"])
    files = []
    for i, s in enumerate(shots):
        st = time.time()
        target = max(25, min(121, int(float(s.get("seconds", 5)) * 24) + 1))
        target = (target - 1 + 3) // 4 * 4 + 1  # the video model works in steps of 4 frames
        frames = aligned_keyframe_count(target, 2) if fast else target
        plan = plan_refine_resolutions(height=h, width=w, num_frames=frames, spatial_scale=1, vae_spatial_compression=16,
                                       vae_temporal_compression=4, patch_size=tuple(config.get("patch_size", (1, 2, 2))), enabled=False)
        lt, lh, lw = plan.latent_frames, plan.stage1_latent_height, plan.stage1_latent_width
        say(progress=f"Shot {i + 1} of {len(shots)}: moving video", i=i)
        freqs = make_rotary_embeddings(config, latent_frames=lt, latent_height=lh, latent_width=lw)
        gen = torch.Generator().manual_seed(int(s.get("seed", 1234)) % 2**31)
        noise = mx.array(torch.randn(1, in_ch, lt, lh, lw, generator=gen, dtype=torch.float32).numpy()).astype(mx.float16)
        lat = sample_wan22_dmd(dit, mx.array(embeds[i]).astype(mx.float16), noise, freqs,
                               dmd_denoising_steps=[1000, 757, 522], flow_shift=5.0, warp_denoising_step=True, seed=i)
        lat_np = np.array(lat.astype(mx.float32))
        del lat, noise, freqs
        out = Path(s["out"])
        decode_latents_to_video(lat_np, out, fps=24, backend="taehv", z_dim=in_ch)
        if fast:
            _postprocess_video(video_path=out, fps=24, rife={"factor": 2, "target_frames": target, "sharpen": 0.6}, spatial=None)
        files.append(str(out))
        say(progress=f"Shot {i + 1} of {len(shots)} ready", i=i + 1, seconds=round(time.time() - st, 1))
    del dit
    cleanup_mlx()
    say(ok=True, files=files, total_s=round(time.time() - t0, 1))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        say(error=f"{type(e).__name__}: {e}"[:500])
        sys.exit(1)
