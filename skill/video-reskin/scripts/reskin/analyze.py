"""Source analysis: media facts, camera cuts, the end-card candidate, static overlay candidates and contact sheets."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from reskin.common import FFMPEG, log, read, run, timestamp, video_info, write, write_text

SMALL = 48            # width of the frames used for cut detection
OVERLAY = 160         # width of the frames used for overlay detection
END_WINDOW = 10.0     # an end card starts within this many seconds of the end


CJK_FONTS = ("msyh.ttc", "msyhl.ttc", "simhei.ttf", "simsun.ttc")     # labels carry Chinese subject names


def font(size: int):
    folder = Path(__import__("os").environ.get("SystemRoot", "C:/Windows")) / "Fonts"
    for name in CJK_FONTS:
        if (folder / name).is_file():
            try:
                return ImageFont.truetype(str(folder / name), size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):
        return ImageFont.load_default()


def decode(path: Path, width: int, *, fps: float | None = None, start: float | None = None,
           duration: float | None = None, gray: bool = False) -> np.ndarray:
    """All frames (or `fps` samples) of a range as a uint8 array (frames, height, width[, 3])."""
    info = video_info(path)
    height = max(2, round(width * info["height"] / info["width"] / 2) * 2)
    filters = ([f"fps={fps}"] if fps else []) + [f"scale={width}:{height}:flags=area", "format=" + ("gray" if gray else "rgb24")]
    command = [FFMPEG, "-v", "error"]
    if start is not None:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", path]
    if duration is not None:
        command += ["-t", f"{duration:.3f}"]
    command += ["-vf", ",".join(filters), "-f", "rawvideo", "-"]
    raw = run(command, binary=True)
    channels = 1 if gray else 3
    frames = np.frombuffer(raw, np.uint8)
    count = frames.size // (height * width * channels)
    shape = (count, height, width) if gray else (count, height, width, 3)
    return frames[: count * height * width * channels].reshape(shape)


def detect_cuts(frames: np.ndarray, fps: float) -> tuple[list[int], np.ndarray, np.ndarray]:
    """Frame indices where a new camera shot starts, the grey frames and the per-frame change."""
    gray = frames.astype(np.float32).mean(axis=3)
    diff = np.zeros(len(frames), np.float32)
    diff[1:] = np.abs(gray[1:] - gray[:-1]).mean(axis=(1, 2))
    bins = (frames // 32).astype(np.int32)
    codes = bins[..., 0] * 64 + bins[..., 1] * 8 + bins[..., 2]
    hist = np.stack([np.bincount(c.ravel(), minlength=512) for c in codes]).astype(np.float32)
    hist /= hist.sum(axis=1, keepdims=True)
    hdiff = np.zeros(len(frames), np.float32)
    hdiff[1:] = np.abs(hist[1:] - hist[:-1]).sum(axis=1) / 2
    window = max(3, int(fps))
    cuts = []
    for i in range(1, len(frames)):
        lo, hi = max(1, i - window), min(len(frames), i + window + 1)
        local = np.median(np.concatenate([diff[lo:i], diff[i + 1:hi]])) if hi - lo > 1 else 0
        if diff[i] > max(18.0, 5 * local) and hdiff[i] > .2:
            # A flash or a one-frame glitch returns to the old picture within three frames.
            if i + 3 < len(frames) and np.abs(gray[i + 3] - gray[i - 1]).mean() < diff[i] / 3:
                continue
            if cuts and i - cuts[-1] < .3 * fps:
                continue
            cuts.append(i)
    return cuts, gray, diff


def _shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """How far the picture moved from `a` to `b` (pixels, x and y) by phase correlation, to a tenth of a pixel."""
    window = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2((a - a.mean()) * window), np.fft.fft2((b - b.mean()) * window)
    cross = fb * np.conj(fa)
    surface = np.fft.ifft2(cross / (np.abs(cross) + 1e-6)).real
    y, x = np.unravel_index(int(surface.argmax()), surface.shape)

    def refine(values, index, size):
        left, mid, right = values[(index - 1) % size], values[index], values[(index + 1) % size]
        bend = left - 2 * mid + right
        offset = .5 * (left - right) / bend if bend < 0 else 0.0
        position = index + max(-.5, min(.5, offset))
        return position - size if position > size / 2 else position
    return refine(surface[y, :], x, surface.shape[1]), refine(surface[:, x], y, surface.shape[0])


def camera_motion(gray: np.ndarray, fps: float, cuts: list[int]) -> list[list[float]]:
    """Per frame, the camera's speed on screen: [x, y] in frame widths / heights per second (0 at camera cuts).
    Seams go where the camera moves fast (plan): motion hides a switch best."""
    step = max(1, round(fps / 12))          # compare frames 1/12 s apart: slow pans move a pixel or more
    height, width = gray.shape[1:]
    starts = sorted(set(cuts))
    speeds = [[0.0, 0.0] for _ in range(len(gray))]
    for i in range(step, len(gray)):
        if any(i - step < c <= i for c in starts):
            continue
        dx, dy = _shift(gray[i - step], gray[i])
        speeds[i] = [round(dx / width * fps / step, 4), round(dy / height * fps / step, 4)]
    return speeds


def overlay_candidates(path: Path, info: dict, shots: list[dict]) -> list[dict]:
    """Small regions that stay still while the scene moves (logos, watermarks, HUD counters, warning lines).

    Windows of 3 s inside one shot: an overlay that moves to another corner later shows up as a second candidate.
    """
    rate, span = 4, 12
    samples = decode(path, OVERLAY, fps=rate, gray=True).astype(np.float32)
    height, width = samples.shape[1:]
    found = []
    for shot in shots:
        a, b = int(shot["start"] * rate), min(len(samples), int(shot["end"] * rate))
        for start in range(a, max(a + 1, b - span + 1), span // 2):
            clip = samples[start:start + span]
            if len(clip) < span:
                continue
            spread = clip.std(axis=0)
            if np.median(spread) < 8:       # the camera barely moves: stillness says nothing
                continue
            mean = clip.mean(axis=0)
            gy, gx = np.gradient(mean)
            mask = _dilate((spread < 3.5) & (np.hypot(gx, gy) > 8), 2)
            for x0, y0, x1, y1 in _components(mask, min_area=int(.001 * width * height),
                                              max_area=int(.04 * width * height)):
                found.append({"box": [round(x0 / width, 3), round(y0 / height, 3), round(x1 / width, 3), round(y1 / height, 3)],
                              "start": round(start / rate, 2), "end": round(min(b, start + span) / rate, 2)})
    merged: list[dict] = []
    for row in found:
        for known in merged:
            if _iou(known["box"], row["box"]) > .3 and row["start"] - known["end"] <= 1.6:
                known["end"] = max(known["end"], row["end"])
                known["box"] = [min(known["box"][0], row["box"][0]), min(known["box"][1], row["box"][1]),
                                max(known["box"][2], row["box"][2]), max(known["box"][3], row["box"][3])]
                known["windows"] += 1
                break
        else:
            merged.append({**row, "windows": 1})
    # Scene objects pause too: keep boxes that stay put for 6 s or more near the frame edges, where overlays sit.
    def near_edge(box):
        return box[0] < .2 or box[2] > .8 or box[1] < .15 or box[3] > .85
    kept = [row for row in merged if row["windows"] >= 3 and near_edge(row["box"])]
    for number, row in enumerate(kept, 1):
        row["id"] = f"O{number}"
        row["where"] = _where(row["box"])
        del row["windows"]
    return kept


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


def _components(mask: np.ndarray, min_area: int, max_area: int) -> list[tuple[int, int, int, int]]:
    seen = np.zeros_like(mask, bool)
    boxes = []
    height, width = mask.shape
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        stack = [(y, x)]
        seen[y, x] = True
        area, x0, y0, x1, y1 = 0, x, y, x, y
        while stack:
            cy, cx = stack.pop()
            area += 1
            x0, y0, x1, y1 = min(x0, cx), min(y0, cy), max(x1, cx), max(y1, cy)
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if min_area <= area and (x1 - x0 + 1) * (y1 - y0 + 1) <= max_area:
            boxes.append((int(x0), int(y0), int(x1) + 1, int(y1) + 1))
    return boxes


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _where(box) -> str:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    vertical = "top" if cy < 1 / 3 else ("bottom" if cy > 2 / 3 else "middle")
    horizontal = "left" if cx < 1 / 3 else ("right" if cx > 2 / 3 else "centre")
    return f"{vertical}-{horizontal}"


def end_card(shots: list[dict], gray: np.ndarray, fps: float, duration: float) -> dict | None:
    """Where the closing card (logo, store badges, CTA) most likely begins: the last hard cut near the end, or the
    start of the strongest gradual transition (wipe, dissolve, blur-in) there, whichever comes first."""
    candidates = []
    late = [s for s in shots[1:] if duration - s["start"] <= END_WINDOW]
    if late:
        shot = late[0] if len(late) > 1 and late[-1]["end"] - late[-1]["start"] < 1.0 else late[-1]
        candidates.append({"start": shot["start"], "kind": "cut",
                           "reason": f"hard cut {duration - shot['start']:.1f} s before the end"})
    lag = max(1, int(round(fps / 2)))
    if len(gray) > 4 * lag:
        change = np.abs(gray[lag:] - gray[:-lag]).mean(axis=(1, 2))
        times = (np.arange(len(change)) + lag) / fps
        body = change[times < duration - END_WINDOW]
        tail = np.nonzero(times >= duration - END_WINDOW)[0]
        if len(body) and len(tail):
            typical = float(np.median(body))
            peak = int(tail[np.argmax(change[tail])])
            if change[peak] > max(3 * typical, 14.0):
                half = (typical + change[peak]) / 2
                first = peak
                while first > tail[0] and change[first - 1] > half:
                    first -= 1
                start = max(0.0, times[first] - lag / fps)
                candidates.append({"start": round(start, 3), "kind": "transition",
                                   "reason": f"picture change {change[peak]:.0f} vs typical {typical:.0f} "
                                             f"{duration - start:.1f} s before the end"})
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["start"])


def sheet(path: Path, info: dict, times: list[float], out: Path, *, columns: int, thumb: int,
          marks: dict[float, str] | None = None, boxes: list[dict] | None = None, title: str = "") -> Path:
    """Contact sheet of frames at `times` (thumb = width), labelled with time; overlay boxes drawn in red."""
    width = thumb
    height = round(thumb * info["height"] / info["width"])
    frames = []
    for t in times:
        raw = run([FFMPEG, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
                   "-vf", f"scale={width}:{height}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
        if len(raw) < width * height * 3:
            raw = bytes(width * height * 3)
        frames.append(Image.frombytes("RGB", (width, height), raw[: width * height * 3]))
    rows = -(-len(frames) // columns)
    size = max(12, min(20, thumb // 12))
    label = font(size)
    band = size + 10
    head = 34 if title else 0
    canvas = Image.new("RGB", (columns * width, head + rows * (height + band)), "white")
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((8, 6), title, fill="black", font=font(20))
    for number, (t, frame) in enumerate(zip(times, frames)):
        x, y = (number % columns) * width, head + (number // columns) * (height + band)
        canvas.paste(frame, (x, y))
        tile = ImageDraw.Draw(canvas)
        for row in boxes or []:
            if row["start"] - .01 <= t <= row["end"] + .01:
                bx = [x + row["box"][0] * width, y + row["box"][1] * height, x + row["box"][2] * width, y + row["box"][3] * height]
                tile.rectangle(bx, outline=(255, 0, 0), width=2)
                tile.text((bx[0] + 2, bx[1] + 1), row["id"], fill=(255, 0, 0), font=label)
        text = timestamp(t) + (f"  {marks[t]}" if marks and t in marks else "")
        draw.text((x + 4, y + height + 3), text, fill="black", font=label)
        draw.line([(x, y), (x, y + height)], fill="white")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=85)
    return out


def analyze_source(job: Path, row: dict) -> dict:
    path = Path(row["path"])
    info = video_info(path)
    log(f"{row['id']}: {info['width']}x{info['height']} {info['fps']:.2f} fps {info['duration']:.1f} s, detecting cuts")
    frames = decode(path, SMALL)
    fps = info["fps"]
    info["frames"] = len(frames)
    duration = len(frames) / fps
    cuts, gray, diff = detect_cuts(frames, fps)
    write(job / "catalog/motion" / f"{row['id']}.json", [round(float(x), 2) for x in diff])
    write(job / "catalog/motion" / f"{row['id']}-camera.json", camera_motion(gray, fps, cuts))
    bounds = [0] + cuts + [len(frames)]
    shots = [{"n": n + 1, "start": round(a / fps, 3), "end": round(b / fps, 3), "frames": [a, b]}
             for n, (a, b) in enumerate(zip(bounds[:-1], bounds[1:]))]
    overlays = overlay_candidates(path, info, shots)
    card = end_card(shots, gray, fps, duration)
    step = 2.0 if duration > 40 else 1.0
    times = [round(t, 2) for t in np.arange(0.25, duration - .05, step)]
    marks = {}
    for shot in shots[1:]:
        nearest = min(times, key=lambda t: abs(t - shot["start"])) if times else None
        if nearest is not None and abs(nearest - shot["start"]) < step / 2:
            marks[nearest] = f"cut {shot['start']:.2f}"
    portrait = info["height"] >= info["width"]
    sheets = job / "catalog"
    overview = sheet(path, info, times, sheets / f"{row['id']}.jpg", columns=10 if portrait else 6,
                     thumb=192 if portrait else 300, marks=marks, boxes=overlays,
                     title=f"{row['id']} {row['name']}  (every {step:g} s; red boxes: still overlays)")
    tail_times = [round(t, 2) for t in np.arange(max(0.0, duration - 12), duration - .02, .5)]
    tail = sheet(path, info, tail_times, sheets / f"{row['id']}-end.jpg", columns=8 if portrait else 6,
                 thumb=180 if portrait else 280, boxes=overlays, title=f"{row['id']} last 12 s (every 0.5 s)")
    return {"id": row["id"], "name": row["name"], "path": row["path"], "info": info, "duration": round(duration, 3),
            "shots": shots, "end_card": card, "overlays": overlays, "sheets": [str(overview), str(tail)]}


def catalog_markdown(rows: list[dict]) -> str:
    lines = ["# Catalog", "",
             "Per source: media facts, camera shots, the end-card candidate (`end_card.start` is where the kept part",
             "would stop) and still overlays (red boxes on the sheets: logos, watermarks and warning lines to remove, or",
             "HUD counters to keep). Look at both sheets of every source before writing the edits file.", ""]
    for row in rows:
        info = row["info"]
        lines.append(f"## {row['id']} {row['name']}")
        lines.append(f"{info['width']}x{info['height']}, {info['fps']:.3f} fps, {row['duration']:.2f} s, "
                     f"audio: {'yes' if info['audio'] else 'no'}; sheets: {', '.join(Path(s).name for s in row['sheets'])}")
        lines.append("shots: " + ", ".join(f"#{s['n']} {s['start']:.2f}-{s['end']:.2f}" for s in row["shots"]))
        card = row["end_card"]
        lines.append(f"end card candidate: {card['start']:.2f} s ({card['kind']}: {card['reason']})" if card
                     else "end card candidate: none found (check the -end sheet)")
        if row["overlays"]:
            lines.append("still overlays: " + "; ".join(f"{o['id']} {o['where']} box {o['box']} {o['start']:.1f}-{o['end']:.1f} s"
                                                       for o in row["overlays"]))
        else:
            lines.append("still overlays: none detected")
        lines.append("")
    return "\n".join(lines)


def run_all(job: Path, state: dict, only: set[str] | None = None) -> list[dict]:
    rows = [r for r in state["sources"] if not only or r["id"] in only]
    with ThreadPoolExecutor(min(4, len(rows))) as executor:
        results = list(executor.map(lambda r: analyze_source(job, r), rows))
    existing = {}
    catalog_path = job / "catalog/catalog.json"
    if catalog_path.is_file():
        existing = {r["id"]: r for r in read(catalog_path)["sources"]}
    existing.update({r["id"]: r for r in results})
    ordered = [existing[r["id"]] for r in state["sources"] if r["id"] in existing]
    write(catalog_path, {"sources": ordered})
    write_text(job / "catalog/catalog.md", catalog_markdown(ordered))
    return ordered
