"""Screen-fixed, flat 2D text and UI burned in by the program after the AI restyle, so they are always exact.

Seedance and GPT Image redraw every pixel: captions, labels, banners and buttons come back garbled or changed. Each
`overlays` item names a region fixed on the screen and how it comes back:

- from the original (default): the region's pixels, frame by frame, through a matte: the item's `shape` (box,
  ellipse, or a `poly`gon), narrowed to the listed `colors` when given (white letters over the scene, say);
- drawn (`text`): the given text, fitted into the box, in `color` with an optional `stroke` and `fill` plate.

The models are told to leave every item out (its `what` joins the removals of the keyframe and video prompts, timed
by `start`/`end`), so no garbled copy shows next to the exact one; `"ai": "keep"` lets them draw it underneath
(colour-keyed glyphs on a plate the model draws, say).

Only screen-fixed, flat elements: UI standing in the scene (floor price tags, bubbles over characters, signs in
perspective) and text with complex motion stay with the models; a copy pasted over the moving scene does not keep up
with it (tested). Screen-fixed counters whose surroundings move are better served by `hud` (a matte of their still
pixels).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from reskin.common import FFMPEG, log

KEYS = {"what", "ai", "box", "poly", "shape", "start", "end", "colors", "tolerance", "text", "font", "color",
        "stroke", "fill"}
FONTS = {"bold": ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/arialbd.ttf"],
         "regular": ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"],
         "heavy": ["C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyhbd.ttc"]}


def _colour(value: str) -> tuple[int, ...]:
    """#rgb, #rrggbb or #rrggbbaa -> RGBA."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(f"colour {value!r} must be #rrggbb or #rrggbbaa")
    rgba = [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]
    return tuple(rgba + [255] * (4 - len(rgba)))


def problem(items) -> str | None:
    """Validation message for an `overlays` list, or None."""
    if not isinstance(items, list):
        return "overlays must be a list"
    for item in items:
        if isinstance(item, dict) and ("follow" in item or "at" in item):
            return (f"overlay {item.get('what')!r}: only screen-fixed elements are burned in; UI that moves with the "
                    "scene stays with the models (leave it out of overlays)")
        if not isinstance(item, dict) or set(item) - KEYS:
            extra = sorted(set(item) - KEYS) if isinstance(item, dict) else []
            return f"overlay {item!r}: unknown keys {extra}; known: {sorted(KEYS)}"
        if item.get("ai", "remove") not in ("remove", "keep"):
            return f"overlay {item.get('what')!r}: ai must be remove (the models leave it out) or keep"
        if not isinstance(item.get("what"), str) or not item["what"].strip():
            return f"overlay {item!r}: needs what, the element in English (the models are told to leave it out)"
        poly = item.get("poly")
        if poly is not None:
            if (not isinstance(poly, list) or len(poly) < 3
                    or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1
                                                                          for v in p) for p in poly)):
                return f"overlay {item.get('what')!r}: poly must be at least 3 [x, y] points (fractions of the frame)"
        box = item.get("box")
        if box is None and poly is None:
            return f"overlay {item.get('what')!r}: needs a box [x0, y0, x1, y1] or a poly"
        if box is not None and (not isinstance(box, list) or len(box) != 4
                                or not all(isinstance(v, (int, float)) for v in box)
                                or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
            return f"overlay {item.get('what')!r}: box must be [x0, y0, x1, y1], fractions of the frame"
        x0, y0, x1, y1 = region(item)
        if (x1 - x0) * (y1 - y0) > .3:
            return f"overlay {item.get('what')!r}: covers more than 30% of the frame"
        for key in ("start", "end"):
            if key in item and not isinstance(item[key], (int, float)):
                return f"overlay {item.get('what')!r}: {key} must be seconds"
        if item.get("start", 0) > item.get("end", float("inf")):
            return f"overlay {item.get('what')!r}: start is after end"
        if item.get("shape", "box") not in ("box", "ellipse"):
            return f"overlay {item.get('what')!r}: shape must be box or ellipse (or give a poly)"
        if "text" in item and (not isinstance(item["text"], str) or not item["text"].strip()):
            return f"overlay {item.get('what')!r}: text must be the words to draw"
        if "text" in item and "colors" in item:
            return f"overlay {item.get('what')!r}: colors key the original's pixels; a text item draws its own"
        if "font" in item and item["font"] not in FONTS and not Path(item["font"]).is_file():
            return f"overlay {item.get('what')!r}: font must be one of {sorted(FONTS)} or a font file"
        try:
            for key in ("color", "stroke", "fill"):
                if key in item:
                    _colour(item[key])
            for value in item.get("colors") or []:
                _colour(value)
        except (ValueError, AttributeError) as error:
            return f"overlay {item.get('what')!r}: {error}"
        if "colors" in item and (not isinstance(item["colors"], list) or not item["colors"]):
            return f"overlay {item.get('what')!r}: colors must be a list like [\"#ffffff\"]"
        if "tolerance" in item and not (isinstance(item["tolerance"], (int, float)) and 5 <= item["tolerance"] <= 200):
            return f"overlay {item.get('what')!r}: tolerance must be 5-200 (RGB distance)"
    return None


def region(item: dict) -> tuple[float, float, float, float]:
    """The item's bounding box, as fractions."""
    if item.get("poly"):
        xs, ys = [p[0] for p in item["poly"]], [p[1] for p in item["poly"]]
        return min(xs), min(ys), max(xs), max(ys)
    return tuple(item["box"])


# --- mattes and drawn text --------------------------------------------------------------------------------------

def _font(name: str, size: int):
    for path in ([name] if Path(name).is_file() else FONTS.get(name, FONTS["bold"])):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_text(item: dict, width: int, height: int) -> np.ndarray:
    """RGBA picture (height x width) of the item's text, as large as fits, centred; a `fill` plate behind it."""
    picture = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(picture)
    if "fill" in item:
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=max(2, min(width, height) // 5),
                               fill=_colour(item["fill"]))
    text = item["text"]
    stroke = _colour(item["stroke"]) if "stroke" in item else None
    pad = max(1, round(min(width, height) * (.12 if "fill" in item else .04)))
    best = None
    for size in range(max(6, height * 2), 5, -1):
        font = _font(item.get("font", "bold"), size)
        stroke_width = max(1, round(size / 14)) if stroke else 0
        left, top, right, bottom = draw.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke_width,
                                                           align="center", spacing=size // 6)
        if right - left <= width - 2 * pad and bottom - top <= height - 2 * pad:
            best = (font, stroke_width, (left, top, right, bottom), size)
            break
    if best is None:
        return np.asarray(picture)
    font, stroke_width, (left, top, right, bottom), size = best
    x = (width - (right - left)) / 2 - left
    y = (height - (bottom - top)) / 2 - top
    draw.multiline_text((x, y), text, font=font, fill=_colour(item.get("color", "#ffffff")), align="center",
                        spacing=size // 6, stroke_width=stroke_width, stroke_fill=stroke)
    return np.asarray(picture)


def shape_matte(item: dict, size: tuple[int, int], origin: tuple[int, int], crop: tuple[int, int]) -> np.ndarray:
    """The item's shape (feathered, 0-1) inside a crop whose top-left sits at `origin` (output pixels)."""
    width, height = size
    picture = Image.new("L", crop, 0)
    draw = ImageDraw.Draw(picture)
    if item.get("poly"):
        draw.polygon([(p[0] * width - origin[0], p[1] * height - origin[1]) for p in item["poly"]], fill=255)
    else:
        x0, y0, x1, y1 = (item["box"][0] * width - origin[0], item["box"][1] * height - origin[1],
                          item["box"][2] * width - origin[0] - 1, item["box"][3] * height - origin[1] - 1)
        if item.get("shape") == "ellipse":
            draw.ellipse((x0, y0, x1, y1), fill=255)
        else:
            draw.rounded_rectangle((x0, y0, x1, y1), radius=max(1, round(min(x1 - x0, y1 - y0) * .12)), fill=255)
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(1.0)), np.float32) / 255


def colour_key(crop: np.ndarray, colours: list[tuple[int, ...]], tolerance: float) -> np.ndarray:
    """0-1 per pixel: how close the original's pixel is to one of the colours (soft edge for anti-aliasing)."""
    rgb = crop.astype(np.float32)
    distance = np.min([np.sqrt(((rgb - np.array(c[:3], np.float32)) ** 2).sum(axis=2)) for c in colours], axis=0)
    key = np.clip((tolerance * 1.6 - distance) / (tolerance * .6), 0, 1)
    # Grow by a pixel so the glyphs' anti-aliased rims come along.
    grown = np.asarray(Image.fromarray((key * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3)), np.float32) / 255
    return np.maximum(key, grown * .6)


# --- the pass -----------------------------------------------------------------------------------------------------

def prepare(items: list[dict], keep: tuple[float, float], fps: float, size: tuple[int, int],
            frame_count: int) -> list[dict]:
    """Everything the per-frame pass needs: frame ranges, positions, mattes or drawn pictures."""
    width, height = size
    prepared = []
    for item in items:
        start = max(keep[0], item.get("start", keep[0]))
        end = min(keep[1], item.get("end", keep[1]))
        if end <= start:
            continue
        first = max(0, round((start - keep[0]) * fps))
        last = min(frame_count, round((end - keep[0]) * fps))
        x0, y0, x1, y1 = region(item)
        pad = 2
        origin = (int(np.floor(x0 * width)) - pad, int(np.floor(y0 * height)) - pad)
        crop = (int(np.ceil(x1 * width)) - origin[0] + pad, int(np.ceil(y1 * height)) - origin[1] + pad)
        entry = {"what": item.get("what", ""), "first": first, "last": last, "origin": origin, "crop": crop}
        matte = shape_matte(item, size, origin, crop)
        if "text" in item:
            drawn = draw_text(item, crop[0], crop[1]).astype(np.float32)
            entry["picture"] = drawn[..., :3]
            entry["alpha"] = drawn[..., 3] / 255 * (matte if item.get("poly") or item.get("shape") == "ellipse" else 1)
            entry["kind"] = "text"
        else:
            entry["alpha"] = matte
            entry["colours"] = [_colour(c) for c in item.get("colors") or []]
            entry["tolerance"] = float(item.get("tolerance", 60))
            entry["kind"] = "original"
        prepared.append(entry)
    return prepared


def composite(frame: np.ndarray, original: np.ndarray | None, index: int, entries: list[dict]) -> np.ndarray:
    """Burn every item active at output frame `index` into `frame` (H x W x 3 uint8, modified copy)."""
    out = None
    height, width = frame.shape[:2]
    for entry in entries:
        if not entry["first"] <= index < entry["last"]:
            continue
        x, y = entry["origin"]
        cw, ch = entry["crop"]
        fx0, fy0, fx1, fy1 = max(0, x), max(0, y), min(width, x + cw), min(height, y + ch)
        if fx1 <= fx0 or fy1 <= fy0:
            continue
        sx0, sy0 = fx0 - x, fy0 - y
        sx1, sy1 = sx0 + fx1 - fx0, sy0 + fy1 - fy0
        alpha = entry["alpha"][sy0:sy1, sx0:sx1]
        if entry["kind"] == "text":
            picture = entry["picture"][sy0:sy1, sx0:sx1]
        else:
            picture = original[fy0:fy1, fx0:fx1].astype(np.float32)
            if entry["colours"]:
                alpha = alpha * colour_key(picture, entry["colours"], entry["tolerance"])
        if out is None:
            out = frame.astype(np.float32)
        region_ = out[fy0:fy1, fx0:fx1]
        out[fy0:fy1, fx0:fx1] = region_ + (picture - region_) * alpha[..., None]
    return frame if out is None else np.clip(out + .5, 0, 255).astype(np.uint8)


def burn(base: list, source: Path, keep: tuple[float, float], fps: float, size: tuple[int, int], frame_count: int,
         entries: list[dict], output: list, extra_inputs: list | None = None) -> None:
    """Decode the assembled picture (`base`: ffmpeg input arguments), burn the overlays in, encode with `output`
    (ffmpeg output arguments, the burned video being input 0 and `extra_inputs` following it)."""
    width, height = size
    frame_bytes = width * height * 3
    flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    need_original = any(entry["kind"] == "original" for entry in entries)
    reader = subprocess.Popen([str(p) for p in [FFMPEG, "-v", "error", *base, "-f", "rawvideo", "-pix_fmt", "rgb24",
                                                "-"]],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
    original = None
    if need_original:
        original = subprocess.Popen(
            [str(p) for p in [FFMPEG, "-v", "error", "-i", source, "-vf",
                              f"trim=start={keep[0]:.6f}:end={keep[1]:.6f},setpts=PTS-STARTPTS,fps={fps},"
                              f"scale={width}:{height}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
    log_path = Path(str(output[-1]) + ".log")
    with open(log_path, "wb") as errors:
        writer = subprocess.Popen(
            [str(p) for p in [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                              f"{width}x{height}", "-r", str(fps), "-i", "-", *(extra_inputs or []), *output]],
            stdin=subprocess.PIPE, stderr=errors, creationflags=flags)
        written = 0
        try:
            last_original = None
            for index in range(frame_count):
                data = reader.stdout.read(frame_bytes)
                if len(data) < frame_bytes:
                    break
                frame = np.frombuffer(data, np.uint8).reshape(height, width, 3)
                if original is not None:
                    other = original.stdout.read(frame_bytes)
                    if len(other) == frame_bytes:
                        last_original = np.frombuffer(other, np.uint8).reshape(height, width, 3)
                writer.stdin.write(composite(frame, last_original, index, entries).tobytes())
                written += 1
        finally:
            writer.stdin.close()
            code = writer.wait()
            for process in (reader, original):
                if process is not None:
                    process.stdout.close()
                    process.kill()
                    process.wait()
    detail = log_path.read_text("utf-8", errors="replace").strip()
    log_path.unlink(missing_ok=True)
    if code or not written:
        raise RuntimeError(f"burning overlays failed ({code}, {written} frames): {detail[-2000:]}")
    if written < frame_count:
        log(f"overlays: the assembled picture ended after {written} of {frame_count} frames")


def summary(entries: list[dict]) -> list[dict]:
    return [{"what": e["what"], "kind": e["kind"], "first": e["first"], "last": e["last"]} for e in entries]
