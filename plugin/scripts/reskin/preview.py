"""Preview stills, on request only: the keyframe prompt tried on a few chosen source frames before Seedance runs.

Keyframes show the first frame of every shot. When an object appears later (a timed `materials` item, a colour that
changes during a shot) or after a `redo --prompt`, a preview of that moment shows on one still whether the texts the
video prompt uses there are right, for the price of an image instead of a render. Previews are only looked at; they
never go to Seedance. (The 1080p batch previewed every ~10 s of every segment and again after every edits change:
393 of its 427 images.)
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import images, subjects as SUBJECTS
from reskin.analyze import font
from reskin.common import digest, log, read, sha, write
from reskin.keyframes import grab
from reskin.plan import SUBJECT_SHEETS
from reskin.prompts import compose, subjects_clause, subjects_in

LIMIT = 4            # previews per call at most
WORKERS = 4          # GPT Image previews in flight at once


def segment_at(row: dict, moment: float) -> dict:
    """The segment that delivers the source time `moment`."""
    frame = moment * row["fps"]
    for seg in row["segments"]:
        if seg["frames"][0] <= frame < seg["frames"][1]:
            return seg
    return row["segments"][-1] if frame >= row["segments"][-1]["frames"][1] else row["segments"][0]


def image_path(job: Path, source_id: str, moment: float) -> Path:
    return job / "keyframes/preview" / f"{source_id}-{moment:07.2f}.png"


def corrections(job: Path, source_id: str, row: dict, moment: float) -> str:
    """The corrective sentences (`redo --prompt`) of the segment that delivers `moment`, told which second of the
    segment this frame is (the sentences name moments in segment seconds: "about 16 s into this segment...")."""
    from reskin.render import corrections as segment_corrections
    seg = segment_at(row, moment)
    text = segment_corrections(job, f"{source_id}:{seg['id']}", seg)
    if not text:
        return ""
    return (f"This picture is the frame {moment - seg['render_frames'][0] / row['fps']:.0f} s into its segment (the "
            f"times below count from the segment's start): {text}")


def make(job: Path, state: dict, source_id: str, row: dict, style: dict, moment: float, lock,
         redo: bool = False) -> dict:
    sources = {r["id"]: r for r in state["sources"]}
    folder = job / "keyframes/preview"
    name = f"{source_id}-{moment:07.2f}"
    frame = round(moment * row["fps"])
    if segment_at(row, moment).get("live"):
        raise ValueError(f"{moment:g} s lies in a live-action span: delivered from the original, nothing to preview")
    original = grab(Path(sources[source_id]["path"]), frame, row["fps"], folder / f"{name}-src.png")
    anchor = job / "keyframes" / f"{source_id}-anchor.png"
    shown = [s["id"] for s, _ in subjects_in(row, (moment, moment))][:SUBJECT_SHEETS]
    sheets = SUBJECTS.sheets_for(job, source_id, row, shown)
    clause = subjects_clause("keyframe", [(s["what"], "") for s, _ in sheets], first=3)
    prompt = compose(style, "keyframe", row, extra=corrections(job, source_id, row, moment), span=(moment, moment),
                     subjects=clause)
    references = [original, anchor, *(path for _, path in sheets)]
    inputs = {"images": [sha(p) for p in references], "prompt": prompt, "size": [row["width"], row["height"]]}
    output = folder / f"{name}.png"
    record_path = job / "state/preview.json"
    with lock:
        known = (read(record_path) if record_path.is_file() else {}).get(name, {})
    if output.is_file() and known.get("digest") == digest(inputs) and not redo:
        return {"key": name, "reused": True}
    log(f"{source_id}: preview at {moment:.2f} s")
    made = images.edit(references, prompt, output, width=row["width"], height=row["height"], job=job,
                       key=f"{source_id}@{moment:.2f}", purpose="preview")
    with lock:
        data = read(record_path) if record_path.is_file() else {}
        data[name] = {"digest": digest(inputs), "prompt": prompt, **made}
        write(record_path, data)
    return {"key": name, "reused": False}


def run_all(job: Path, state: dict, plan: dict, style: dict, source_id: str, at: list[float],
            workers: int = WORKERS, redo: bool = False) -> list[dict]:
    """Previews of one source at the source times `at` (at most LIMIT); `redo`: make them again even when current."""
    times = sorted({round(float(t), 2) for t in at})
    if not times:
        raise ValueError("preview needs --at <source seconds> (the moments to check)")
    if len(times) > LIMIT:
        raise ValueError(f"at most {LIMIT} previews per call ({len(times)} asked): each one is a paid image; "
                         "preview only the moments the keyframes do not show")
    row = plan["sources"].get(source_id)
    if row is None:
        raise ValueError(f"unknown or skipped source {source_id}")
    if not (job / "keyframes" / f"{source_id}-anchor.png").is_file():
        raise ValueError(f"{source_id}: no anchor keyframe yet: run keyframes first")
    lock = threading.Lock()

    def safe(moment):
        try:
            return make(job, state, source_id, row, style, moment, lock, redo=redo)
        except Exception as error:
            return {"key": f"{source_id}@{moment:.2f}", "error": f"{type(error).__name__}: {error}"}
    with ThreadPoolExecutor(max(1, workers)) as executor:
        results = list(executor.map(safe, times))
    sheet(job, source_id, row, times)
    return results


def sheet(job: Path, source_id: str, row: dict, times: list[float]) -> Path | None:
    """Original | preview pairs with their source time → `sheets/<id>-preview.jpg`."""
    folder = job / "keyframes/preview"
    pairs = [(t, folder / f"{source_id}-{t:07.2f}-src.png", folder / f"{source_id}-{t:07.2f}.png") for t in times]
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
