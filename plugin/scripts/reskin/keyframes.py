"""Style keyframes: one anchor per source (the look), then the first frame of every segment restyled after it."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import images
from reskin.analyze import font
from reskin.common import FFMPEG, digest, log, read, run, sha, write
from reskin.prompts import compose, reference


def grab(path: Path, frame: int, fps: float, output: Path) -> Path:
    """One source frame as PNG (frame-accurate)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    # Accurate input seeking returns the first frame at or after the time: aim half a frame early.
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, (frame - .5) / fps):.6f}", "-i", path, "-frames:v", "1", output])
    return output


def _record(job: Path, key: str) -> dict:
    path = job / "state/keyframes.json"
    data = read(path) if path.is_file() else {}
    return data.get(key, {})


def _remember(job: Path, key: str, value: dict, lock):
    with lock:
        path = job / "state/keyframes.json"
        data = read(path) if path.is_file() else {}
        data[key] = value
        write(path, data)


def tasks(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None, redo: set[str]) -> list[dict]:
    """Every keyframe to make, in dependency order: anchors first (style_from anchors after their source)."""
    sources = {row["id"]: row for row in state["sources"]}
    rows = []
    order = sorted(plan["sources"], key=lambda sid: (1 if plan["sources"][sid].get("style_from") else 0, sid))
    for source_id in order:
        row = plan["sources"][source_id]
        fps = row["fps"]
        wanted = lambda key: not only or source_id in only or key in only
        anchor_frame = round(row["anchor"] * fps)
        rows.append({"key": f"{source_id}:anchor", "source": source_id, "path": sources[source_id]["path"], "fps": fps,
                     "frame": anchor_frame, "kind": "anchor", "row": row, "selected": wanted(f"{source_id}:anchor")})
        for seg in row["segments"]:
            rows.append({"key": f"{source_id}:{seg['id']}", "source": source_id, "path": sources[source_id]["path"],
                         "fps": fps, "frame": seg["render_frames"][0], "kind": "segment", "row": row,
                         "selected": wanted(f"{source_id}:{seg['id']}")})
    for task in rows:
        task["redo"] = task["key"] in redo or task["source"] in redo
    return rows


def make(job: Path, task: dict, style: dict, lock) -> dict:
    row, source_id = task["row"], task["source"]
    folder = job / "keyframes"
    original = grab(Path(task["path"]), task["frame"], task["fps"], folder / f"{task['key'].replace(':', '-')}-src.png")
    output = folder / f"{task['key'].replace(':', '-')}.png"
    references = [original]
    moment = (task["frame"] / task["fps"],) * 2
    if task["kind"] == "anchor" and row.get("style_from"):
        references.append(folder / f"{row['style_from']}-anchor.png")
        prompt = compose(style, "keyframe", row, span=moment)
    elif task["kind"] == "anchor":
        # The style's reference picture (Image 2) sets the look; the anchor then carries it to every keyframe.
        look = reference(style)
        if look is not None:
            references.append(look)
        prompt = compose(style, "anchor", row, span=moment)
    else:
        references.append(folder / f"{source_id}-anchor.png")
        prompt = compose(style, "keyframe", row, span=moment)
    for ref in references[1:]:
        if not ref.is_file():
            raise RuntimeError(f"{task['key']}: needs {ref.name} first")
    known = _record(job, task["key"])
    # The pictures decide whether a keyframe is current; a changed prompt applies to new and redone keyframes.
    inputs = {"images": [sha(p) for p in references], "size": [row["width"], row["height"]],
              "attempt": known.get("attempt", 1) + (1 if task["redo"] else 0)}
    if output.is_file() and known.get("digest") == digest(inputs) and not task["redo"]:
        return {"key": task["key"], "reused": True, "output": str(output), **known,
                "prompt_changed": known.get("prompt") != prompt}
    log(f"{task['key']}: restyling frame {task['frame']} ({'anchor' if task['kind'] == 'anchor' else 'segment start'})")
    made = images.edit(references, prompt, output, width=row["width"], height=row["height"])
    with Image.open(output) as image:
        converted = image.convert("RGB") if image.format != "PNG" else None
    if converted is not None:
        converted.save(output, "PNG")
    value = {"digest": digest(inputs), "attempt": inputs["attempt"], "prompt": prompt, "frame": task["frame"], **made}
    _remember(job, task["key"], value, lock)
    return {"key": task["key"], "reused": False, "output": str(output), **value}


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None,
            redo: set[str] | None = None, workers: int = 3) -> list[dict]:
    lock = threading.Lock()
    rows = tasks(job, state, plan, style, only, redo or set())
    results = []
    anchors = [t for t in rows if t["kind"] == "anchor" and t["selected"]]
    # Anchors of sources styled after another source wait for that anchor.
    plain = [t for t in anchors if not t["row"].get("style_from")]
    dependent = [t for t in anchors if t["row"].get("style_from")]
    with ThreadPoolExecutor(workers) as executor:
        results += list(executor.map(lambda t: _safe(job, t, style, lock), plain))
        results += list(executor.map(lambda t: _safe(job, t, style, lock), dependent))
        segments = [t for t in rows if t["kind"] == "segment" and t["selected"]]
        results += list(executor.map(lambda t: _safe(job, t, style, lock), segments))
    for source_id in plan["sources"]:
        sheet(job, source_id, plan["sources"][source_id])
    return results


def _safe(job, task, style, lock) -> dict:
    try:
        return make(job, task, style, lock)
    except Exception as error:      # one failed keyframe must not stop the others; the summary lists it
        return {"key": task["key"], "error": f"{type(error).__name__}: {error}"}


def sheet(job: Path, source_id: str, row: dict) -> Path | None:
    """Original | styled pairs (anchor first) for the agent to judge."""
    folder = job / "keyframes"
    keys = ["anchor"] + [seg["id"] for seg in row["segments"]]
    pairs = [(key, folder / f"{source_id}-{key}-src.png", folder / f"{source_id}-{key}.png") for key in keys]
    pairs = [p for p in pairs if p[1].is_file() and p[2].is_file()]
    if not pairs:
        return None
    portrait = row["height"] >= row["width"]
    width = 240 if portrait else 400
    height = round(width * row["height"] / row["width"])
    columns = 3 if portrait else 2
    label = font(18)
    cell_w, cell_h = 2 * width + 12, height + 30
    rows = -(-len(pairs) // columns)
    canvas = Image.new("RGB", (columns * cell_w, rows * cell_h), "white")
    draw = ImageDraw.Draw(canvas)
    for number, (key, src, styled) in enumerate(pairs):
        x, y = (number % columns) * cell_w, (number // columns) * cell_h
        for offset, file in ((0, src), (width, styled)):
            with Image.open(file) as image:
                canvas.paste(image.convert("RGB").resize((width, height), Image.LANCZOS), (x + offset, y + 26))
        draw.text((x + 4, y + 3), f"{source_id} {key}: original | styled", fill="black", font=label)
    out = job / "sheets" / f"{source_id}-keyframes.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=88)
    return out
