"""Screen-fixed HUD (coin counters, timers) copied back from the original, so restyled videos never show wrong digits.

Seedance redraws every pixel and gets changing numbers wrong. Inside a box the agent names, the pixels that stay
still while the scene moves behind them are the HUD (icon, panel, and the digits enclosed by it); that matte is
taken from the original frame by frame. Translucent panels keep the original's see-through tint, a small price for
exact numbers.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from reskin.common import FFMPEG, run

MARGIN = 24          # pixels around the box that show the moving scene, for comparison
STILL = .55          # a pixel is HUD when it varies less than this share of the scene around it
SAMPLE_FPS = 4


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Pixels the background cannot reach from the edge (digits inside a panel) become part of the HUD."""
    height, width = mask.shape
    outside = np.zeros_like(mask)
    queue = deque()
    for y in range(height):
        for x in (0, width - 1):
            if not mask[y, x] and not outside[y, x]:
                outside[y, x] = True
                queue.append((y, x))
    for x in range(width):
        for y in (0, height - 1):
            if not mask[y, x] and not outside[y, x]:
                outside[y, x] = True
                queue.append((y, x))
    while queue:
        y, x = queue.popleft()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < height and 0 <= nx < width and not mask[ny, nx] and not outside[ny, nx]:
                outside[ny, nx] = True
                queue.append((ny, nx))
    return ~outside


def matte(source: Path, keep: list[float], item: dict, size: tuple[int, int], output: Path) -> dict:
    """Write the feathered matte (PNG, the crop's size) and return where it goes on the output frame."""
    width, height = size
    x0, y0 = int(item["box"][0] * width), int(item["box"][1] * height)
    x1, y1 = int(round(item["box"][2] * width)), int(round(item["box"][3] * height))
    cx0, cy0 = max(0, x0 - MARGIN), max(0, y0 - MARGIN)
    cx1, cy1 = min(width, x1 + MARGIN), min(height, y1 + MARGIN)
    cw, ch = cx1 - cx0, cy1 - cy0
    start = max(keep[0], item.get("start", keep[0]))
    end = min(keep[1], item.get("end", keep[1]))
    raw = run([FFMPEG, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{max(.5, end - start):.3f}", "-i", source, "-vf",
               f"fps={SAMPLE_FPS},scale={width}:{height}:flags=bicubic,crop={cw}:{ch}:{cx0}:{cy0},format=gray",
               "-f", "rawvideo", "-"], binary=True)
    frames = np.frombuffer(raw, np.uint8)
    count = frames.size // (cw * ch)
    frames = frames[: count * cw * ch].reshape(count, ch, cw).astype(np.float32)
    inner = np.zeros((ch, cw), bool)
    inner[y0 - cy0:y1 - cy0, x0 - cx0:x1 - cx0] = True
    spread = frames.std(axis=0) if count >= 4 else np.zeros((ch, cw), np.float32)
    scene = float(np.median(spread[~inner])) if (~inner).any() else 0.0
    if scene >= 8:
        mask = _fill_holes(spread < STILL * scene) & inner
        method = "still pixels"
    else:
        mask = np.zeros((ch, cw), bool)
    if mask.sum() < .05 * inner.sum():
        # The scene hardly moves (or nothing stood out): fall back to the box itself, with rounded corners.
        picture = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(picture).rounded_rectangle((x0 - cx0, y0 - cy0, x1 - cx0 - 1, y1 - cy0 - 1),
                                                  radius=max(2, min(x1 - x0, y1 - y0) // 4), fill=255)
        method = "box"
    else:
        picture = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
    picture = picture.filter(ImageFilter.GaussianBlur(1.5))
    output.parent.mkdir(parents=True, exist_ok=True)
    picture.save(output)
    coverage = float(np.asarray(picture, np.float32).mean() / 255 / max(1e-6, inner.mean()))
    return {"mask": str(output), "x": cx0, "y": cy0, "w": cw, "h": ch, "start": start, "end": end,
            "method": method, "scene_motion": round(scene, 1), "coverage": round(coverage, 2),
            "what": item.get("what", "")}
