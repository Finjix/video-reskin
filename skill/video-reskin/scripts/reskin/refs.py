"""Reference pictures every segment of a source shares (they replace per-segment keyframes and chained frames):

- one scene map per scene: GPT Image joins the survey's view frames into one big overview of the whole location in
  realistic materials (4K, quality high), with the look taken from the style's reference picture (or, for a sister
  cut, `style_from`, from that source's scene map);
- one three-view sheet per subject (front, side, back; 1K, quality medium) for the characters, objects and props
  that come into view during the video, drawn at the scene map's realism.

Every H3 segment gets the scene map(s) it shows and the sheets of the subjects on screen (render), so segments
rendered apart look alike and render side by side.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import images
from reskin.analyze import font
from reskin.common import digest, log, read, sha, write
from reskin.prompts import compose, reference
from reskin.survey import crop, frame

# Lumos fetches every reference picture from storage with a short timeout: a 20 MB 4K PNG timed out (2026-11-09),
# so larger pictures go as JPEG, and a scene map that only sets the look of a subject sheet goes reduced.
UPLOAD_LIMIT = 8_000_000     # bytes
LOOK_SIZE = 2048             # long side of a scene map sent as a look reference


def scene_key(source_id: str, k: int) -> str:
    return f"{source_id}:scene{k}"


def subject_key(source_id: str, k: int) -> str:
    return f"{source_id}:P{k:02d}"


def path_of(job: Path, key: str) -> Path:
    return job / "refs" / f"{key.replace(':', '-')}.png"


def upload_copy(path: Path, longest: int | None = None) -> Path:
    """The picture as uploaded: the PNG itself, or a JPEG (quality 95) when the PNG is too large; `longest` also
    scales it down to that long side."""
    if path.stat().st_size <= UPLOAD_LIMIT and not longest:
        return path
    out = path.with_name(path.stem + (f"-{longest}" if longest else "") + ".jpg")
    if not out.is_file() or out.stat().st_mtime < path.stat().st_mtime:
        with Image.open(path) as image:
            image = image.convert("RGB")
            if longest and max(image.size) > longest:
                image.thumbnail((longest, longest), Image.LANCZOS)
            image.save(out, quality=95, subsampling=0)
    return out


def scene_aspect(row: dict, scene: dict, camera: list[list[float]]) -> str:
    """The GPT Image aspect of the area the camera covers in this scene: one frame plus the camera's travel
    (summed screen speeds over the scene's span, at most 3 frames each way)."""
    fps = row["fps"]
    a = round(max(scene.get("start", 0), row["keep"][0]) * fps)
    b = round(min(scene.get("end", 1e9), row["keep"][1]) * fps)
    x = y = 0.0
    xs, ys = [0.0], [0.0]
    for vx, vy in camera[a:b]:
        x, y = x + vx / fps, y + vy / fps
        xs.append(x); ys.append(y)
    span_x, span_y = min(3.0, max(xs) - min(xs)), min(3.0, max(ys) - min(ys))
    width, height = row["width"] * (1 + span_x), row["height"] * (1 + span_y)
    return images.aspect_for(width, height)


def tasks(job: Path, state: dict, plan: dict, only: set[str] | None, redo: set[str]) -> list[dict]:
    """Every picture to make, in dependency order: scene maps of plain sources, of `style_from` sources, then the
    subject sheets."""
    sources = {row["id"]: row for row in state["sources"]}
    rows = []
    order = sorted(plan["sources"], key=lambda sid: (1 if plan["sources"][sid].get("style_from") else 0, sid))
    for source_id in order:
        row = plan["sources"][source_id]
        wanted = lambda key: not only or source_id in only or key in only
        for k, scene in enumerate(row["scenes"], 1):
            key = scene_key(source_id, k)
            rows.append({"key": key, "kind": "scene", "source": source_id, "path": sources[source_id]["path"],
                         "row": row, "index": k, "item": scene, "selected": wanted(key)})
    for source_id in order:
        row = plan["sources"][source_id]
        wanted = lambda key: not only or source_id in only or key in only
        used = {k for seg in row["segments"] for k in seg.get("subjects") or []}
        for k, subject in enumerate(row["subjects"], 1):
            if k not in used:       # no segment takes its sheet
                continue
            key = subject_key(source_id, k)
            rows.append({"key": key, "kind": "subject", "source": source_id, "path": sources[source_id]["path"],
                         "row": row, "index": k, "item": subject, "selected": wanted(key)})
    for task in rows:
        task["redo"] = task["key"] in redo or task["source"] in redo
    return rows


def scene_of(row: dict, t: float) -> int:
    for k, scene in enumerate(row["scenes"], 1):
        if scene.get("start", 0) <= t <= scene.get("end", float("inf")):
            return k
    return 1


def _record(job: Path, key: str) -> dict:
    path = job / "state/refs.json"
    return (read(path) if path.is_file() else {}).get(key, {})


def _remember(job: Path, key: str, value: dict, lock) -> None:
    with lock:
        path = job / "state/refs.json"
        data = read(path) if path.is_file() else {}
        data[key] = value
        write(path, data)


def make(job: Path, task: dict, style: dict, lock) -> dict:
    row, source_id, item = task["row"], task["source"], task["item"]
    source = Path(task["path"])
    folder = job / "refs"
    output = path_of(job, task["key"])
    stem = task["key"].replace(":", "-")
    if task["kind"] == "scene":
        views = [frame(source, t, folder / "inputs" / f"{stem}-view{n}.png") for n, t in enumerate(item["views"], 1)]
        look = None
        if row.get("style_from"):
            other = path_of(job, scene_key(row["style_from"], 1))
            if not other.is_file():
                raise RuntimeError(f"{task['key']}: needs {other.name} (style_from) first")
            look = other
        else:
            look = reference(style)
        references = views + ([look] if look else [])
        camera_path = job / "catalog/motion" / f"{source_id}-camera.json"
        aspect = scene_aspect(row, item, read(camera_path) if camera_path.is_file() else [])
        span = (item.get("start", 0), item.get("end", row["keep"][1]))
        prompt = compose(style, "scene", row, span=span, views=len(views), look_image=len(views) + 1)
        settings = images.SCENE
    else:
        full = frame(source, item["at"], folder / "inputs" / f"{stem}-frame.png")
        close = crop(full, item["box"], folder / "inputs" / f"{stem}-crop.png")
        scene = path_of(job, scene_key(source_id, scene_of(row, item["at"])))
        if not scene.is_file():
            raise RuntimeError(f"{task['key']}: needs the scene map {scene.name} first")
        references = [close, full, upload_copy(scene, LOOK_SIZE)]
        aspect = "16:9"
        prompt = compose(style, "subject", row, span=(item["at"], item["at"]), what=item["what"],
                         description=item.get("description") or item.get("kind", "object"))
        settings = images.SUBJECT
    known = _record(job, task["key"])
    # The pictures decide whether a reference is current; a changed prompt applies to new and redone ones.
    inputs = {"images": [sha(p) for p in references], "aspect": aspect, **settings,
              "attempt": known.get("attempt", 1) + (1 if task["redo"] else 0)}
    if output.is_file() and known.get("digest") == digest(inputs) and not task["redo"]:
        return {"key": task["key"], "reused": True, "output": str(output), **known,
                "prompt_changed": known.get("prompt") != prompt}
    log(f"{task['key']}: GPT Image {task['kind']} ({settings['resolution']}, {settings['quality']}, {aspect}, "
        f"{len(references)} pictures)")
    made = images.edit(references, prompt, output, aspect=aspect, **settings)
    with Image.open(output) as image:
        size = list(image.size)
        converted = image.convert("RGB") if image.format != "PNG" else None
    if converted is not None:
        converted.save(output, "PNG")
    value = {"digest": digest(inputs), "attempt": inputs["attempt"], "prompt": prompt, "size": size, **made}
    _remember(job, task["key"], value, lock)
    return {"key": task["key"], "reused": False, "output": str(output), **value}


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None,
            redo: set[str] | None = None, workers: int = 3) -> list[dict]:
    lock = threading.Lock()
    rows = tasks(job, state, plan, only, redo or set())
    results = []
    safe = lambda t: _safe(job, t, style, lock)
    with ThreadPoolExecutor(workers) as executor:
        scenes = [t for t in rows if t["kind"] == "scene" and t["selected"]]
        results += list(executor.map(safe, [t for t in scenes if not t["row"].get("style_from")]))
        results += list(executor.map(safe, [t for t in scenes if t["row"].get("style_from")]))
        results += list(executor.map(safe, [t for t in rows if t["kind"] == "subject" and t["selected"]]))
    for source_id in plan["sources"]:
        sheet(job, source_id, plan["sources"][source_id])
    return results


def _safe(job, task, style, lock) -> dict:
    from reskin import lumos
    for attempt in range(2):
        try:
            return make(job, task, style, lock)
        except lumos.GenerationFailed as error:      # Lumos refuses now and then (fetch timeouts): once more
            if attempt == 0:
                log(f"{task['key']}: {str(error)[:200]}; trying once more")
                continue
            return {"key": task["key"], "error": f"GenerationFailed: {error}"}
        except Exception as error:      # one failed picture must not stop the others; the summary lists it
            return {"key": task["key"], "error": f"{type(error).__name__}: {error}"}


def sheet(job: Path, source_id: str, row: dict) -> Path | None:
    """What the agent judges: per scene its view frames and the scene map, per subject its crop and its sheet."""
    folder = job / "refs"
    blocks = []
    for k, scene in enumerate(row["scenes"], 1):
        made = path_of(job, scene_key(source_id, k))
        views = sorted((folder / "inputs").glob(f"{source_id}-scene{k}-view*.png"))
        if made.is_file():
            blocks.append((f"scene {k} {scene['name']}: views | scene map", views, made))
    for k, subject in enumerate(row["subjects"], 1):
        made = path_of(job, subject_key(source_id, k))
        close = folder / "inputs" / f"{source_id}-P{k:02d}-crop.png"
        if made.is_file() and close.is_file():
            blocks.append((f"P{k:02d} {subject['what']}: crop | three views", [close], made))
    if not blocks:
        return None
    width, band = 1800, 28
    label = font(20)
    rendered = []
    for title, inputs, made in blocks:
        height = 420 if len(inputs) <= 1 else 520
        strip = Image.new("RGB", (width, height + band), "white")
        draw = ImageDraw.Draw(strip)
        draw.text((6, 4), title, fill="black", font=label)
        x = 0
        share = .38 if len(inputs) <= 1 else .5
        cell = int(width * share / max(1, len(inputs))) if len(inputs) > 1 else int(width * share)
        for path in inputs:
            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail((cell - 6, height - 6), Image.LANCZOS)
                strip.paste(image, (x + 3, band + 3))
            x += cell
        with Image.open(made) as image:
            image = image.convert("RGB")
            image.thumbnail((width - x - 6, height - 6), Image.LANCZOS)
            strip.paste(image, (x + 3, band + 3))
        rendered.append(strip)
    canvas = Image.new("RGB", (width, sum(s.height for s in rendered)), "white")
    y = 0
    for strip in rendered:
        canvas.paste(strip, (0, y))
        y += strip.height
    out = job / "sheets" / f"{source_id}-refs.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, compress_level=1)
    return out
