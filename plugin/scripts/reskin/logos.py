"""Screen-fixed logos and warning lines wiped from the clip Seedance gets: only their own pixels, filled from the
scene showing around and between the letters.

A logo stays put while the scene moves behind it. Over the item's time its pixels match their median in most
frames, and the median shows it crisp where the scene averages out. Those pixels, grown a little, are filled frame
by frame from the scene around them (push-pull: averages over coarser and coarser neighbourhoods of the known
pixels). Every earlier fill of the whole box came back from Seedance as something else: an interpolation (delogo) as
streaks, a mirrored neighbouring strip as upside-down people, doubled icons and fence crosses, a smooth fill as a
blurred rectangle. The scene between the letters it renders as scene; over plain water or snow it kept the fill of
dense letters as a blurred patch (the 1080p batch: every corner of S01 and S09), so the video prompt names where the
wiped patches are (prompts.wiped_clause) and `quality` measures every wiped corner of a rendered video: the detail
left inside the mask against the scene around it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from reskin.common import FFMPEG, digest, run
from reskin.prompts import is_wiped

TOL = 14.0            # grey levels: a frame matches the median at a pixel within this
SHARE = .5            # a logo pixel matches its median in at least this share of the item's frames
EDGE = 8.0            # detail of the median (grey levels) that marks a logo edge, at least
NEAR = 8              # px: still pixels count only this close to such an edge (flat snow away from the letters stays)
GROW = 4              # px the mask grows by (antialiasing, outline, glow)
SOFT = 2.0            # px: soft edge of the mask
MARGIN = 24           # px of scene around the box the fill draws on
SAMPLES = 80          # frames sampled over the item's time
VERSION = 2           # raise when the mask method changes (the masks, and the renders using them, are made again)


def blur(values: np.ndarray, sigma: float) -> np.ndarray:
    """About Gaussian (three box passes), edges clamped; HxW or HxWxC float32."""
    radius = max(1, int(round((4 * sigma * sigma + 1) ** .5 / 2)))
    out = values.astype(np.float32)
    for _ in range(3):
        for axis in (0, 1):
            pad = [(0, 0)] * out.ndim
            pad[axis] = (radius + 1, radius)
            summed = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis, dtype=np.float64)
            n = out.shape[axis]
            out = ((np.take(summed, np.arange(2 * radius + 1, 2 * radius + 1 + n), axis=axis)
                    - np.take(summed, np.arange(n), axis=axis)) / (2 * radius + 1)).astype(np.float32)
    return out


def pushpull(values: np.ndarray, known: np.ndarray) -> np.ndarray:
    """Fill where `known` is 0 (weights in [0, 1]) from coarser and coarser averages of the known pixels."""
    if min(known.shape) <= 2 or float(known.min()) >= .999:
        return values
    h, w = known.shape
    ph, pw = h % 2, w % 2
    v = np.pad(values * known[..., None], ((0, ph), (0, pw), (0, 0)), mode="edge")
    k = np.pad(known, ((0, ph), (0, pw)), mode="edge")
    v2 = v[0::2, 0::2] + v[1::2, 0::2] + v[0::2, 1::2] + v[1::2, 1::2]
    k2 = k[0::2, 0::2] + k[1::2, 0::2] + k[0::2, 1::2] + k[1::2, 1::2]
    coarse = pushpull(v2 / np.maximum(k2[..., None], 1e-6), np.minimum(k2, 1.0))
    up = blur(np.repeat(np.repeat(coarse, 2, 0), 2, 1)[:h, :w], 1.0)
    return values * known[..., None] + up * (1 - known[..., None])


def _grow(mask: np.ndarray, pixels: int) -> np.ndarray:
    from PIL import Image, ImageFilter
    if pixels <= 0:
        return mask
    picture = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * pixels + 1))
    return np.asarray(picture) > 127


def region(box: list[float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The box plus MARGIN, in pixels of a frame of `size`: (x0, y0, x1, y1)."""
    width, height = size
    return (max(0, round(box[0] * width) - MARGIN), max(0, round(box[1] * height) - MARGIN),
            min(width, round(box[2] * width) + MARGIN), min(height, round(box[3] * height) + MARGIN))


def measure(source: Path, box: list[float], start: float, end: float, size: tuple[int, int],
            others: list[list[float]] | None = None) -> np.ndarray:
    """The logo's soft mask (float, the region's size) from the original over [start, end) seconds. `others`:
    boxes of other screen-fixed items on screen meanwhile, left out of the scene around (their crisp, still letters
    would pass for scene and raise the bar for this logo's edges)."""
    width, height = size
    x0, y0, x1, y1 = region(box, size)
    w, h = x1 - x0, y1 - y0
    count = int(min(SAMPLES, max(16, (end - start) * 4)))
    rate = count / max(.5, end - start)
    raw = run([FFMPEG, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{max(.5, end - start):.3f}", "-i", source, "-vf",
               f"fps={rate:.4f},scale={width}:{height},crop={w}:{h}:{x0}:{y0}", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-"], binary=True)
    frames = np.frombuffer(raw, np.uint8)
    n = frames.size // (w * h * 3)
    if n < 3:
        raise ValueError(f"too few frames to find the logo in {box} ({n})")
    frames = frames[: n * w * h * 3].reshape(n, h, w, 3).astype(np.float32)
    median = np.median(frames, axis=0)
    match = (np.abs(frames - median).mean(axis=3) < TOL).mean(axis=0)
    detail = np.abs(median - blur(median, 2)).mean(axis=2)
    inner = np.zeros((h, w), bool)
    bx0, by0 = round(box[0] * width) - x0, round(box[1] * height) - y0
    bx1, by1 = round(box[2] * width) - x0, round(box[3] * height) - y0
    inner[by0:by1, bx0:bx1] = True
    ring = ~inner
    for other in others or []:
        ox0, oy0 = max(0, round(other[0] * width) - x0), max(0, round(other[1] * height) - y0)
        ox1, oy1 = min(w, round(other[2] * width) - x0), min(h, round(other[3] * height) - y0)
        if ox1 > ox0 and oy1 > oy0:
            ring[oy0:oy1, ox0:ox1] = False
    scene_match = float(np.median(match[ring])) if ring.any() else 0.0
    scene_detail = float(np.percentile(detail[ring], 90)) if ring.any() else 0.0
    edges = (detail > max(EDGE, 1.5 * scene_detail)) & inner
    # Opaque letters: still pixels near the letters' edges. Translucent ones (they change with the scene behind):
    # the edges the median keeps crisp, grown over the strokes.
    still = (match > max(SHARE, scene_match + .25)) & _grow(edges, NEAR) & inner
    mask = _grow(still | edges, GROW) & _grow(inner, GROW)
    return np.clip(blur(mask.astype(np.float32), SOFT) * 1.5, 0, 1)


def masks(job: Path, source_id: str, source: Path, row: dict, size: tuple[int, int]) -> dict[int, dict]:
    """Per `remove` item with a box (index in the list): its mask PNG, made once per item and cached, with the
    content hash the render inputs carry."""
    from PIL import Image
    folder = job / "work" / "logos"
    found = {}
    keep_start, keep_end = row["keep"]
    for n, item in enumerate(row.get("remove") or []):
        if not is_wiped(item):
            continue
        start = max(keep_start, float(item.get("start", keep_start)))
        end = min(keep_end, float(item.get("end", keep_end)))
        if end - start <= 0:
            continue
        others = [o["box"] for o in row.get("remove") or [] if isinstance(o, dict) and o.get("box") and o is not item
                  and float(o.get("start", keep_start)) < end and float(o.get("end", keep_end)) > start]
        stamp = {"box": item["box"], "start": start, "end": end, "size": list(size), "version": VERSION,
                 "source": str(source), "others": others}
        key = digest(stamp)[:12]
        path = folder / f"{source_id}-{n}-{key}.png"
        if not path.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            soft = measure(source, item["box"], start, end, size, others)
            partial = path.with_name(path.stem + ".partial.png")
            Image.fromarray(np.clip(soft * 255 + .5, 0, 255).astype(np.uint8)).save(partial)
            partial.replace(path)
        found[n] = {"path": str(path), "hash": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
                    "region": list(region(item["box"], size))}
    return found


def load(path: str) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path), np.float32) / 255


def wipe(frame: np.ndarray, rect: list[int], soft: np.ndarray) -> None:
    """Fill the masked pixels of `frame` (RGB uint8, in place) inside `rect` from the scene around them.

    (Filling them first from a plate of the scene seen in other frames, the camera motion tracked around the box,
    rebuilt icons and fences under the letters in calm stretches, but pasted stale or misaligned scene wherever
    people walked past, shadows moved or the motion was misread: letters of wrong scene, worse than the soft fill,
    which Seedance mostly renders over as scene.)"""
    x0, y0, x1, y1 = rect
    part = frame[y0:y1, x0:x1].astype(np.float32)
    known = 1 - np.clip(soft * 2, 0, 1)
    filled = pushpull(part, known)
    frame[y0:y1, x0:x1] = np.clip(frame[y0:y1, x0:x1] * (1 - soft[..., None]) + filled * soft[..., None] + .5,
                                  0, 255).astype(np.uint8)


SMEARED = .4          # fine detail inside a wiped mask below this share of the scene's around it: a blurred patch
SOFT_SHARE = .6       # below this: soft, a viewer may notice it
PLAIN = 1.5           # grey levels of fine detail around the mask below which a frame cannot show a smear (sky, fog)
QUALITY_SAMPLES = 8   # moments measured per wiped item


def fine_detail(pixels: np.ndarray) -> np.ndarray:
    """Per pixel, how far the grey picture differs from its own blur (sigma 1.5 px): texture, edges, grain."""
    grey = pixels.astype(np.float32).mean(axis=-1)
    return np.abs(grey - blur(grey, 1.5))


def detail_share(pixels: np.ndarray, soft: np.ndarray) -> float | None:
    """Fine detail inside the wiped pixels of one restyled region (HxWx3) against the scene around and between them:
    1 = as detailed as around, about .2 = a smooth patch. None when the scene around is too plain to tell."""
    inside = soft > .5
    around = ~_grow(soft > .05, 6)
    if inside.sum() < 40 or around.sum() < 40:
        return None
    detail = fine_detail(pixels)
    scene = float(detail[around].mean())
    if scene < PLAIN:
        return None
    return round(float(detail[inside].mean()) / scene, 3)


def verdict(share: float | None) -> str:
    if share is None:
        return "plain"
    return "smeared" if share < SMEARED else "soft" if share < SOFT_SHARE else "clean"


def quality(video: Path, items: list[dict], samples: int = QUALITY_SAMPLES) -> list[dict]:
    """How every wiped corner came out in a restyled `video`. `items`: {"what", "start", "end" (seconds of the
    video), "mask" (PNG at `size`), "region" (its rect at that size), "size"}. Per item the median detail share over
    `samples` moments and the verdict: clean, soft, smeared, or plain (nothing to measure against)."""
    results = []
    for item in items:
        soft = load(item["mask"])
        x0, y0, x1, y1 = item["region"]
        width, height = item["size"]
        a, b = float(item["start"]), float(item["end"])
        shares, moments = [], []
        for k in range(samples):
            moment = a + (k + .5) * (b - a) / samples
            raw = run([FFMPEG, "-v", "error", "-ss", f"{moment:.3f}", "-i", video, "-frames:v", "1", "-vf",
                       f"scale={width}:{height},crop={x1 - x0}:{y1 - y0}:{x0}:{y0}", "-f", "rawvideo",
                       "-pix_fmt", "rgb24", "-"], binary=True)
            if len(raw) < (x1 - x0) * (y1 - y0) * 3:
                continue
            pixels = np.frombuffer(raw[: (x1 - x0) * (y1 - y0) * 3], np.uint8).reshape(y1 - y0, x1 - x0, 3)
            share = detail_share(pixels, soft)
            if share is not None:
                shares.append(share)
                moments.append(round(moment, 2))
        share = float(np.median(shares)) if shares else None
        worst = [m for m, s in zip(moments, shares) if s < SMEARED]
        results.append({"what": item.get("what", ""), "start": round(a, 2), "end": round(b, 2),
                        "share": None if share is None else round(share, 2), "verdict": verdict(share),
                        "smeared_at": worst, "measured": len(shares)})
    return results
