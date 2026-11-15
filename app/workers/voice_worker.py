"""Natural voices worker (runs in its own Python, see addons.run_worker).
Indic Parler-TTS by AI4Bharat (Apache 2.0): the voice is described in words, like "voice design".
stdin: {"lines": [{"text", "lang", "voice": female|male, "style", "out"}], "device": "mps|cuda|cpu"} or {"download": true}
stdout (last line): {"ok": true, "files": [...]} or {"error": "..."}"""
import json
import re
import sys

REPO = "ai4bharat/indic-parler-tts"  # Ghost passes the exact model in each job ("repo")
# Named speakers the model card recommends per language (female, male)
SPEAKERS = {"Malayalam": ("Anjali", "Harish"), "Hindi": ("Divya", "Rohit"), "Tamil": ("Jaya", ""), "English": ("Mary", "Thoma")}
STYLES = {"warm": "a warm, friendly and expressive tone", "excited": "an excited, energetic and happy tone",
          "calm": "a calm, soothing and gentle tone", "news": "a confident, clear, professional announcer tone",
          "devotional": "a soft, devotional and reverent tone"}


def main():
    job = json.loads(sys.stdin.read() or "{}")
    global REPO
    REPO = job.get("repo") or REPO
    if job.get("download"):
        from huggingface_hub import snapshot_download
        snapshot_download(REPO)
        print(json.dumps({"ok": True}))
        return
    import numpy as np
    import soundfile as sf
    import torch
    from parler_tts import ParlerTTSForConditionalGeneration
    from transformers import AutoTokenizer
    dev = job.get("device") or "cpu"
    if dev == "mps" and not torch.backends.mps.is_available():
        dev = "cpu"
    model = ParlerTTSForConditionalGeneration.from_pretrained(REPO).to(dev)
    tok = AutoTokenizer.from_pretrained(REPO)
    dtok = AutoTokenizer.from_pretrained(model.config.text_encoder._name_or_path)
    sr = model.config.sampling_rate
    files = []
    for ln in job.get("lines", []):
        female, male = SPEAKERS.get(ln.get("lang", ""), ("", ""))
        male_wanted = ln.get("voice") == "male"
        name = (male if male_wanted else female) or ("A male speaker" if male_wanted else "A female speaker")
        desc = (f"{name} speaks in {STYLES.get(ln.get('style'), STYLES['warm'])} at a moderate pace, "
                "with a very clear, close-sounding studio recording and no background noise.")
        d = dtok(desc, return_tensors="pt").to(dev)
        parts = []
        for c in [c.strip() for c in re.split(r"(?<=[.!?।॥])\s+", ln["text"].strip()) if c.strip()] or [ln["text"]]:
            t = tok(c, return_tensors="pt").to(dev)
            with torch.no_grad():
                a = model.generate(input_ids=d.input_ids, attention_mask=d.attention_mask,
                                   prompt_input_ids=t.input_ids, prompt_attention_mask=t.attention_mask)
            parts.append(a.cpu().float().numpy().squeeze())
            parts.append(np.zeros(int(sr * 0.18), dtype=np.float32))
        sf.write(ln["out"], np.concatenate(parts), sr)
        files.append(ln["out"])
    print(json.dumps({"ok": True, "files": files}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # report, don't crash silently
        print(json.dumps({"error": f"{type(e).__name__}: {e}"[:500]}))
        sys.exit(1)
