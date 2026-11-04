"""Preview stills: the keyframe prompt tried on source frames across every segment, before Seedance runs.

A wrong `materials` or `remove` list goes into every segment's video prompt (tuna drawn as salmon steaks, a truck in
the opening seconds, garbled text where a warning line was named but not shown); keyframes only test the first frame
of each shot. Previews restyle a frame every ~10 s and right after each timed item starts, with the same texts the
video prompt uses at that moment, so such mistakes show on one sheet for the price of a few images. They are only
looked at, never used as references.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import images
from reskin.analyze import font
from reskin.common import digest, log, read, sha, write
from reskin.keyframes import grab
from reskin.prompts import compose

STEP = 10.0          # seconds between previews inside a segment
LIMIT = 12           # previews per source at most


def moments(row: dict) -> list[float]:
    """Source times to preview: every STEP seconds inside each segment (not at its first frame, which a keyframe or
    the chain covers) and half a second after each timed material, removal or overlay starts."""
    keep = row["keep"]
    fps = row["fps"]
    heads = [seg["render_frames"][0] / fps for seg in row["segments"]]
    times = []
    for seg in row["segments"]:
        start, end = seg["frames"][0] / fps, seg["frames"][1] / fps
        count = max(1, round((end - start) / STEP))
        times += [start + (i + .5) * (end - start) / count for i in range(count)]
    for field in ("materials", "remove", "overlays"):
        items = row.get(field)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and "start" in item and keep[0] < item["start"] + .5 < keep[1]:
                    times.append(item["start"] + .5)
    chosen = []
    for t in sorted(times):
        if all(abs(t - other) > 1.5 for other in chosen + heads):
            chosen.append(round(t, 2))
    if len(chosen) > LIMIT:
        step = len(chosen) / LIMIT
        chosen = [chosen[int(i * step)] for i in range(LIMIT)]
    return chosen


def make(job: Path, state: dict, source_id: str, row: dict, style: dict, moment: float, lock) -> dict:
    sources = {r["id"]: r for r in state["sources"]}
    folder = job / "keyframes/preview"
    name = f"{source_id}-{moment:07.2f}"
    frame = round(moment * row["fps"])
    original = grab(Path(sources[source_id]["path"]), frame, row["fps"], folder / f"{name}-src.png")
    anchor = job / "keyframes" / f"{source_id}-anchor.png"
    prompt = compose(style, "keyframe", row, span=(moment, moment))
    inputs = {"images": [sha(original), sha(anchor)], "prompt": prompt, "size": [row["width"], row["height"]]}
    output = folder / f"{name}.png"
    record_path = job / "state/preview.json"
    with lock:
        known = (read(record_path) if record_path.is_file() else {}).get(name, {})
    if output.is_file() and known.get("digest") == digest(inputs):
        return {"key": name, "reused": True}
    log(f"{source_id}: preview at {moment:.2f} s")
    made = images.edit([original, anchor], prompt, output, width=row["width"], height=row["height"], job=job,
                       key=f"{source_id}@{moment:.2f}", purpose="preview")
    with lock:
        data = read(record_path) if record_path.is_file() else {}
        data[name] = {"digest": digest(inputs), "prompt": prompt, **made}
        write(record_path, data)
    return {"key": name, "reused": False}


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None, workers: int = 3) -> list[dict]:
    lock = threading.Lock()
    tasks = []
    for source_id, row in plan["sources"].items():
        if only and source_id not in only:
            continue
        if not (job / "keyframes" / f"{source_id}-anchor.png").is_file():
            tasks.append((source_id, None))
            continue
        tasks += [(source_id, t) for t in moments(row)]

    def safe(task):
        source_id, moment = task
        if moment is None:
            return {"key": source_id, "error": "no anchor keyframe yet: run keyframes first"}
        try:
            return make(job, state, source_id, plan["sources"][source_id], style, moment, lock)
        except Exception as error:
            return {"key": f"{source_id}@{moment:.2f}", "error": f"{type(error).__name__}: {error}"}
    with ThreadPoolExecutor(workers) as executor:
        results = list(executor.map(safe, tasks))
    for source_id, row in plan["sources"].items():
        if not only or source_id in only:
            sheet(job, source_id, row)
    return results


def sheet(job: Path, source_id: str, row: dict) -> Path | None:
    """Original | preview pairs with their source time."""
    folder = job / "keyframes/preview"
    pairs = [(t, folder / f"{source_id}-{t:07.2f}-src.png", folder / f"{source_id}-{t:07.2f}.png") for t in moments(row)]
    pairs = [p for p in pairs if p[1].is_file() and p[2].is_file()]
    if not pairs:
        return None
    portrait = row["height"] >= row["width"]
    width = 200 if portrait else 360
    height = round(width * row["height"] / row["width"])
    columns = 3 if portrait else 2
    label = font(18)
    cell_w, cell_h = 2 * width + 12, height + 30
    rows = -(-len(pairs) // columns)
    canvas = Image.new("RGB", (columns * cell_w, rows * cell_h), "white")
    draw = ImageDraw.Draw(canvas)
    for number, (moment, src, styled) in enumerate(pairs):
        x, y = (number % columns) * cell_w, (number // columns) * cell_h
        for offset, file in ((0, src), (width, styled)):
            with Image.open(file) as image:
                canvas.paste(image.convert("RGB").resize((width, height), Image.LANCZOS), (x + offset, y + 26))
        draw.text((x + 4, y + 3), f"{source_id} {moment:.2f} s: original | preview", fill="black", font=label)
    out = job / "sheets" / f"{source_id}-preview.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=88)
    return out
