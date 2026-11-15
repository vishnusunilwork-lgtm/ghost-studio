"""Shrink old projects ("Ghost DNA"): keep the recipe, not the files.

Like DNA, which stores the recipe for a body rather than the body: every picture Ghost makes comes from a small
recipe (prompt + seed + size + model). Shrinking keeps that recipe (a few hundred bytes, already in project.json)
and a tiny preview, and removes the big files. 🧬 Regrow makes them again from the recipe.

What is kept exactly as it is:  the finished video (final.mp4), songs and their stems, narration, your uploads.
Pictures with a pure recipe     (made once from prompt + seed, never ✨ Changed, not a quality-check retake):
                                 recipe + a small preview; regrown on demand. Same model and settings -> the same
                                 picture; after a model update it can come back as a close variation (Ghost measures
                                 how close and shows it).
Other pictures (changed, retakes): kept as a high-quality JPEG (looks the same, about 5-10x smaller than PNG).
Working files of video ads       (shot pictures, shot clips, talking frames, drafts): removed; regrown on demand.
Undo history is cleared (it points at the removed files).
"""
from __future__ import annotations

import time
from pathlib import Path

PREVIEW = 384  # px, long side
AUDIO = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


def folder_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def usage(store) -> list[dict]:
    out = []
    for f in store.root.glob("*/project.json"):
        try:
            import json
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": p.get("id"), "title": p.get("title"), "kind": p.get("kind") or "video", "status": p.get("status"),
                    "updated": p.get("updated"), "bytes": folder_bytes(f.parent), "shrunk": bool(p.get("shrunk"))})
    return sorted(out, key=lambda x: -x["bytes"])


def _paths(o, acc: set):
    """Every file path mentioned anywhere in the project."""
    if isinstance(o, dict):
        for v in o.values():
            _paths(v, acc)
    elif isinstance(o, list):
        for v in o:
            _paths(v, acc)
    elif isinstance(o, str) and ("/" in o or "\\" in o) and len(o) < 600:
        acc.add(o)


def _preview(src: Path, out: Path) -> Path:
    from PIL import Image
    im = Image.open(src).convert("RGB")
    im.thumbnail((PREVIEW, PREVIEW), Image.LANCZOS)
    im.save(out, "JPEG", quality=82)
    return out


def _jpeg(src: Path) -> Path:
    from PIL import Image
    out = src.with_suffix(".jpg")
    Image.open(src).convert("RGB").save(out, "JPEG", quality=92, subsampling=0)
    return out


def similarity(a: Path, b: Path) -> int:
    """0-100: how alike two pictures look (100 = the same)."""
    from PIL import Image, ImageChops, ImageStat
    x = Image.open(a).convert("L").resize((64, 64), Image.LANCZOS)
    y = Image.open(b).convert("L").resize((64, 64), Image.LANCZOS)
    diff = ImageStat.Stat(ImageChops.difference(x, y)).mean[0]
    return max(0, round(100 - diff * 100 / 64))


def pure_still(p: dict, i: int) -> bool:
    s = p["stills"][i]
    img = Path(s.get("image") or "")
    draft = p.get("quality") == "draft"
    return (s.get("status") == "done" and img.suffix == ".png" and img.exists() and not s.get("edits")
            and img.name == f"image{i + 1:02d}_{s['seed']}{'_draft' if draft else ''}.png")


def shrink(p: dict, d: Path) -> dict:
    """Shrink one project in place. Returns {"freed": bytes, "dna": n, "jpeg": n}. The caller saves p."""
    before = folder_bytes(d)
    n_dna = n_jpg = 0
    keep: set[str] = set()
    # 1. pictures of an Images project
    stills = p.get("stills") or []
    if stills:
        ai = p.get("anchor_i")
        anchor_pure = ai is not None and ai < len(stills) and pure_still(p, ai)
        for i, s in enumerate(stills):
            img = Path(s.get("image") or "")
            if not img.exists() or s.get("dna"):
                continue
            needs_anchor = s.get("look") == "same" and i != ai
            if pure_still(p, i) and (not needs_anchor or anchor_pure):
                prev = _preview(img, d / f"dna_{i + 1:02d}_{s['seed']}.jpg")
                s["dna"] = {"name": img.name, "preview": str(prev), "anchor": ai if needs_anchor else None, "at": time.time()}
                s["image"] = str(prev)
                n_dna += 1
            elif img.suffix == ".png":
                s["image"] = str(_jpeg(img))
                n_jpg += 1
        if p.get("anchor") and ai is not None and ai < len(stills):
            p["anchor"] = stills[ai]["image"]
    # 2. working files of a video ad: shots and clips are regrown on demand; the finished video stays
    clips = p.get("clips") or []
    if clips and p.get("final") and Path(p["final"]).exists():
        for i, c in enumerate(clips):
            src = c.get("image") if c.get("image") and Path(c["image"]).exists() else None
            if src and not c.get("dna"):
                c["dna"] = {"preview": str(_preview(Path(src), d / f"dna_shot{i + 1:02d}_{c.get('seed', 0)}.jpg")), "at": time.time()}
            c["image"] = (c.get("dna") or {}).get("preview")
            c["video"] = None
            c["status"] = "waiting"
        if p.get("image") and Path(p["image"]).exists() and not p.get("dna_main"):
            p["dna_main"] = str(_preview(Path(p["image"]), d / "dna_main.jpg"))
        p["image"] = p.get("dna_main") or p.get("image")
        p["person_images"] = {}
    # 3. undo history points at old files: it goes
    p.pop("undo", None)
    for x in stills + clips:
        x.pop("history", None)
    # 4. remove every picture/video file the project no longer mentions (finished video, audio and uploads stay)
    _paths(p, keep)
    keep = {str(Path(k).resolve()) for k in keep}
    for f in d.rglob("*"):
        if not f.is_file() or f.name.startswith("project.json") or f.suffix.lower() in AUDIO:
            continue
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".txt") and str(f.resolve()) not in keep:
            if "refs" in f.parts or f.name.startswith("ref"):
                continue  # your uploaded reference pictures
            f.unlink(missing_ok=True)
    freed = max(0, before - folder_bytes(d))
    p["shrunk"] = {"at": time.time(), "freed": freed, "dna": n_dna, "jpeg": n_jpg}
    p.setdefault("log", []).append(time.strftime("%H:%M:%S ") + f"🧬 Shrunk: {freed / 1e6:.0f} MB freed "
                                   f"({n_dna} picture recipe(s), {n_jpg} picture(s) kept as JPEG). Regrow brings the full files back.")
    return {"freed": freed, "dna": n_dna, "jpeg": n_jpg}
