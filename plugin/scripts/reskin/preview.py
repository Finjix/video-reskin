"""Preview stills: the keyframe prompt tried on source frames across every segment, before Seedance runs.

A wrong `materials` or `remove` list goes into every segment's video prompt (tuna drawn as salmon steaks, a truck in
the opening seconds, garbled text where a warning line was named but not shown); keyframes only test the first frame
of each shot. Previews restyle a frame every ~10 s and right after each timed item starts, with the same texts the
video prompt uses at that moment, so such mistakes show on one sheet for the price of a few images. They are only
looked at; the ones named in the edits' `midframes` also go to Seedance as references for the realism of their
moment (see render). A preview inside a segment that was sent back with corrective sentences (`redo --prompt`)
carries those sentences too, so a wrong correction shows on a still before it costs a render.
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
WORKERS = 6          # GPT Image previews in flight at once
MIDFRAME_LEAD = 2.0  # a midframe this close to its segment's first frame adds nothing (the keyframe or chain covers it)
MIDFRAME_LIMIT = 3   # midframes per segment at most (Seedance gets them as extra reference pictures)


def segment_at(row: dict, moment: float) -> dict:
    """The segment that delivers the source time `moment`."""
    frame = moment * row["fps"]
    for seg in row["segments"]:
        if seg["frames"][0] <= frame < seg["frames"][1]:
            return seg
    return row["segments"][-1] if frame >= row["segments"][-1]["frames"][1] else row["segments"][0]


def midframes(row: dict, seg: dict) -> list[float]:
    """Preview times inside `seg` that go to Seedance as realism references: the edits' `midframes` times (or, with
    "auto", the segment's regular preview moments), at least MIDFRAME_LEAD s after its first rendered frame."""
    wanted = row.get("midframes")
    if not wanted:
        return []
    lo, hi = seg["render_frames"][0] / row["fps"] + MIDFRAME_LEAD, seg["render_frames"][1] / row["fps"]
    times = _regular(row) if wanted == "auto" else [float(t) for t in wanted]
    times = sorted(t for t in times if lo <= t < hi)
    if len(times) > MIDFRAME_LIMIT:
        step = len(times) / MIDFRAME_LIMIT
        times = [times[int((i + .5) * step)] for i in range(MIDFRAME_LIMIT)]
    return [round(t, 2) for t in times]


def moments(row: dict) -> list[float]:
    """Source times to preview: the regular ones (see _regular) and every midframe."""
    chosen = set(_regular(row))
    for seg in row["segments"]:
        chosen.update(midframes(row, seg))
    return sorted(chosen)


def _regular(row: dict) -> list[float]:
    """Every STEP seconds inside each segment (not at its first frame, which a keyframe or the chain covers) and half
    a second after each timed material, removal or overlay starts."""
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


def image_path(job: Path, source_id: str, moment: float) -> Path:
    return job / "keyframes/preview" / f"{source_id}-{moment:07.2f}.png"


def corrections(job: Path, source_id: str, row: dict, moment: float) -> str:
    """The corrective sentences (`redo --prompt`) of the segment that delivers `moment`, told which second of the
    segment this frame is (the sentences name moments in segment seconds: "本段约16秒处...")."""
    path = job / "config/redo.json"
    redo = read(path) if path.is_file() else {}
    seg = segment_at(row, moment)
    text = redo.get(f"{source_id}:{seg['id']}", {}).get("prompt", "")
    if not text:
        return ""
    return f"这张画面是本段第{moment - seg['render_frames'][0] / row['fps']:.0f}秒的画面（下面说的时间都按本段计）：{text}"


def make(job: Path, state: dict, source_id: str, row: dict, style: dict, moment: float, lock,
         redo: bool = False) -> dict:
    sources = {r["id"]: r for r in state["sources"]}
    folder = job / "keyframes/preview"
    name = f"{source_id}-{moment:07.2f}"
    frame = round(moment * row["fps"])
    original = grab(Path(sources[source_id]["path"]), frame, row["fps"], folder / f"{name}-src.png")
    anchor = job / "keyframes" / f"{source_id}-anchor.png"
    prompt = compose(style, "keyframe", row, extra=corrections(job, source_id, row, moment), span=(moment, moment))
    inputs = {"images": [sha(original), sha(anchor)], "prompt": prompt, "size": [row["width"], row["height"]]}
    output = folder / f"{name}.png"
    record_path = job / "state/preview.json"
    with lock:
        known = (read(record_path) if record_path.is_file() else {}).get(name, {})
    if output.is_file() and known.get("digest") == digest(inputs) and not redo:
        return {"key": name, "reused": True}
    log(f"{source_id}: preview at {moment:.2f} s")
    made = images.edit([original, anchor], prompt, output, width=row["width"], height=row["height"], job=job,
                       key=f"{source_id}@{moment:.2f}", purpose="preview")
    with lock:
        data = read(record_path) if record_path.is_file() else {}
        data[name] = {"digest": digest(inputs), "prompt": prompt, **made}
        write(record_path, data)
    return {"key": name, "reused": False}


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None, workers: int = WORKERS,
            at: list[float] | None = None, midframes_only: bool = False, redo: bool = False) -> list[dict]:
    """Previews of every source (or of the sources and segments in `only`: `S01`, `S01:K02`); `at`: these source
    times instead; `midframes_only`: just the previews Seedance needs as references; `redo`: make the chosen ones
    again even when current (a preview that drew something wrong). When previews of a source are new, their identity
    against the styled keyframes is checked (see `identity`): entries {"key": "S01:identity", ...}."""
    lock = threading.Lock()
    tasks = []
    for source_id, row in plan["sources"].items():
        keys = {k.partition(":")[2] for k in only or () if k.startswith(f"{source_id}:")}
        if only and source_id not in only and not keys:
            continue
        if not (job / "keyframes" / f"{source_id}-anchor.png").is_file():
            tasks.append((source_id, None))
            continue
        if at:
            times = [round(float(t), 2) for t in at]
        elif midframes_only:
            times = sorted({t for seg in row["segments"] for t in midframes(row, seg)})
        else:
            times = moments(row)
        if keys:
            times = [t for t in times if segment_at(row, t)["id"] in keys]
        tasks += [(source_id, t) for t in times]

    def safe(task):
        source_id, moment = task
        if moment is None:
            return {"key": source_id, "error": "no anchor keyframe yet: run keyframes first"}
        try:
            return make(job, state, source_id, plan["sources"][source_id], style, moment, lock, redo=redo)
        except Exception as error:
            return {"key": f"{source_id}@{moment:.2f}", "error": f"{type(error).__name__}: {error}"}
    with ThreadPoolExecutor(workers) as executor:
        results = list(executor.map(safe, tasks))
    for source_id, row in plan["sources"].items():
        if any(task[0] == source_id for task in tasks):
            sheet(job, source_id, row)
            if at:
                sheet(job, source_id, row, times=[round(float(t), 2) for t in at])
            fresh = [r for r in results if not r.get("error") and not r.get("reused")
                     and r["key"].startswith(f"{source_id}-")]
            if fresh:
                results.append(identity(job, source_id, row))
    return results


def identity(job: Path, source_id: str, row: dict) -> dict:
    """Gemini compares every preview with the styled keyframes: does each recurring character and object keep its
    look? Each preview is restyled on its own, so one may draw the protagonist as another person; a midframe like
    that would carry it into the video. Needs the Winky key (skipped without it)."""
    from reskin import credentials, review
    key = f"{source_id}:identity"
    try:
        credentials.winky_key()
    except credentials.MissingCredential:
        return {"key": key, "kind": "identity", "skipped": "no Winky key", "reused": True}
    folder = job / "keyframes/preview"
    mids = {t for seg in row["segments"] for t in midframes(row, seg)}
    stills = [(f"{t:.1f} s" + (" midframe" if t in mids else ""), folder / f"{source_id}-{t:07.2f}-src.png",
               image_path(job, source_id, t)) for t in moments(row)]
    size = (row["width"], row["height"])
    frames = review.identity_frames(review.identity_references(job, source_id, row), size, stills=stills)
    try:
        subjects, issues, sheets = review.identity_check(job, key, frames, row, "GPT preview stills of one video "
                                                         "(each restyled on its own)", size,
                                                         job / "sheets" / f"{source_id}-preview-identity")
    except (RuntimeError, ValueError) as error:
        return {"key": key, "kind": "identity", "skipped": f"check failed ({error})", "reused": True}
    return {"key": key, "kind": "identity", "reused": True, "sheet": ", ".join(str(p) for p in sheets),
            "issues": issues, "subjects": subjects}


def sheet(job: Path, source_id: str, row: dict, times: list[float] | None = None) -> Path | None:
    """Original | preview pairs with their source time (`times`: those previews only, as `<id>-preview-at.jpg`);
    midframes (Seedance references) are marked."""
    folder = job / "keyframes/preview"
    mids = {t for seg in row["segments"] for t in midframes(row, seg)}
    pairs = [(t, folder / f"{source_id}-{t:07.2f}-src.png", folder / f"{source_id}-{t:07.2f}.png")
             for t in (times or moments(row))]
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
        draw.text((x + 4, y + 3), f"{source_id} {moment:.2f} s: original | preview"
                  + (" (Seedance reference)" if moment in mids else ""), fill="black", font=label)
    out = job / "sheets" / f"{source_id}-preview{'-at' if times else ''}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=88)
    return out
