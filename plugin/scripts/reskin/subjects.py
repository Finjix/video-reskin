"""Subject reference sheets: one multi-view picture per subject that must keep one look (the protagonist, recurring
characters and vehicles, props that come into view mid-shot), restyled after the source's anchor.

A keyframe shows only what is on screen at its first frame; whatever comes into view later, and whatever is seen
from other sides as it moves, Seedance used to invent from the text alone (unreal props, a protagonist that changed
between segments). Each subject is cut from 1-3 moments of the original (`from`), and GPT Image draws it in the
anchor's realistic materials as several views on a plain background, from the angles the video's camera shows
(`view`: 2d side views, 2.5d high three-quarter views, 3d turnaround). The sheets then go with every keyframe and
every Seedance segment the subject is on screen in (plan: `keyframe_subjects`, `subjects`). One picture per
subject, all views in it: a sheet per view would multiply the images paid for. A `like` source uses its twin's
sheets (`subjects_owner`).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from reskin import images
from reskin.analyze import font
from reskin.common import PROMPTS, digest, log, read, sha

SHEET_SIZE = (1920, 1080)     # 16:9: the views side by side
CROP_MARGIN = .15             # of the box, added on every side so the whole subject and a little air are in the crop
CROP_MIN = 768                # px: crops are enlarged to at least this on the long side (GPT Image sees small crops badly)
CROP_MAX = 1536


def owner(source_id: str, row: dict) -> str:
    """The source whose sheets a source uses: its `like` twin's when it takes the twin's subjects."""
    return row.get("subjects_owner") or source_id


def sheet_path(job: Path, owner_id: str, subject_id: str) -> Path:
    return job / "keyframes" / f"{owner_id}-subject-{subject_id}.png"


def crop_path(job: Path, owner_id: str, subject_id: str, number: int) -> Path:
    return job / "keyframes" / f"{owner_id}-subject-{subject_id}-src{number}.png"


def crop(source: Path, at: float, box: list[float], output: Path) -> Path:
    """The subject cut from the original at `at` (box widened by CROP_MARGIN), enlarged to CROP_MIN at least.
    Rewritten only when it changes, so the sheet's inputs stay put."""
    from reskin.common import FFMPEG, run
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = output.with_name(output.stem + "-raw.png")
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, at):.4f}", "-i", source, "-frames:v", "1", raw])
    with Image.open(raw) as image:
        frame = image.convert("RGB")
    raw.unlink(missing_ok=True)
    w, h = frame.size
    x0, y0, x1, y1 = box
    mx, my = (x1 - x0) * CROP_MARGIN, (y1 - y0) * CROP_MARGIN
    area = (round(max(0.0, x0 - mx) * w), round(max(0.0, y0 - my) * h),
            round(min(1.0, x1 + mx) * w), round(min(1.0, y1 + my) * h))
    picture = frame.crop(area)
    long_side = max(picture.size)
    scale = CROP_MIN / long_side if long_side < CROP_MIN else min(1.0, CROP_MAX / long_side)
    if scale != 1:
        picture = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
                                 Image.LANCZOS)
    if output.is_file():
        with Image.open(output) as old:
            if old.size == picture.size and old.convert("RGB").tobytes() == picture.tobytes():
                return output
    picture.save(output, "PNG")
    return output


def views_text(subject: dict) -> str:
    return subject.get("views") or read(PROMPTS)["subject_views"][subject.get("view", "3d")]


def prompt(style: dict, row: dict, subject: dict, crops: int) -> str:
    texts = read(PROMPTS)
    spaced = lambda value: f"{value} " if value else ""
    text = texts["subject"].format(crops=crops, anchor=crops + 1, what=subject["what"].strip().rstrip("."),
                                   views=views_text(subject), look=spaced(style.get("look", "")),
                                   extra=row.get("extra") or "")
    return " ".join(text.split())


def tasks(plan: dict, only: set[str] | None, redo: set[str]) -> list[dict]:
    """A sheet per subject of every source that owns its subjects (a `like` source uses its twin's)."""
    rows = []
    for source_id, row in plan["sources"].items():
        if row.get("subjects_owner"):
            continue
        for subject in row.get("subjects") or []:
            key = f"{source_id}:{subject['id']}"
            rows.append({"key": key, "source": source_id, "row": row, "subject": subject, "kind": "subject",
                         "selected": not only or source_id in only or key in only,
                         "redo": key in redo or source_id in redo})
    return rows


def make(job: Path, state: dict, task: dict, style: dict, lock, record, remember) -> dict:
    """Make (or reuse) one subject sheet; `record`/`remember` read and store the keyframe records."""
    source_id, row, subject = task["source"], task["row"], task["subject"]
    source = Path(next(r for r in state["sources"] if r["id"] == source_id)["path"])
    crops = [crop(source, float(c["at"]), c["box"], crop_path(job, source_id, subject["id"], n))
             for n, c in enumerate(subject["from"], 1)]
    anchor = job / "keyframes" / f"{source_id}-anchor.png"
    if not anchor.is_file():
        raise RuntimeError(f"{task['key']}: needs {anchor.name} first")
    references = [*crops, anchor]
    text = prompt(style, row, subject, len(crops))
    known = record(job, task["key"])
    inputs = {"images": [sha(p) for p in references], "views": views_text(subject),
              "attempt": known.get("attempt", 1) + (1 if task["redo"] else 0)}
    output = sheet_path(job, source_id, subject["id"])
    if output.is_file() and known.get("digest") == digest(inputs) and not task["redo"]:
        return {"key": task["key"], "reused": True, "output": str(output), **known,
                "prompt_changed": known.get("prompt") != text}
    log(f"{task['key']}: drawing the reference sheet of {subject['id']} ({subject.get('view', '3d')})")
    made = images.edit(references, text, output, width=SHEET_SIZE[0], height=SHEET_SIZE[1], job=job,
                       key=task["key"], purpose="subject")
    with Image.open(output) as image:
        converted = image.convert("RGB") if image.format != "PNG" else None
    if converted is not None:
        converted.save(output, "PNG")
    value = {"digest": digest(inputs), "attempt": inputs["attempt"], "prompt": text, **made}
    remember(job, task["key"], value, lock)
    return {"key": task["key"], "reused": False, "output": str(output), **value}


def sheets_for(job: Path, source_id: str, row: dict, ids: list[str]) -> list[tuple[dict, Path]]:
    """The subjects named by `ids` with their sheets, in that order; a missing sheet is an error (keyframes first)."""
    by_id = {s["id"]: s for s in row.get("subjects") or []}
    found = []
    for sid in ids:
        if sid not in by_id:
            continue
        path = sheet_path(job, owner(source_id, row), sid)
        if not path.is_file():
            raise ValueError(f"{source_id}: the reference sheet of subject {sid} ({path.name}) is missing; run "
                             "keyframes first")
        found.append((by_id[sid], path))
    return found


def contact_sheet(job: Path, source_id: str, row: dict) -> Path | None:
    """Per subject: its crops from the original, then the generated sheet, for the agent to judge (same design,
    proportions and colours as the original; real materials; one subject, plain background, no text)."""
    if row.get("subjects_owner") or not row.get("subjects"):
        return None
    lines = []
    for subject in row["subjects"]:
        crops = [crop_path(job, source_id, subject["id"], n) for n in range(1, len(subject["from"]) + 1)]
        made = sheet_path(job, source_id, subject["id"])
        if made.is_file() and all(p.is_file() for p in crops):
            lines.append((subject, crops, made))
    if not lines:
        return None
    height = 360
    label = font(18)
    widths = []
    for _, crops, made in lines:
        width = 0
        for path in [*crops, made]:
            with Image.open(path) as image:
                width += round(image.width * height / image.height) + 8
        widths.append(width)
    canvas = Image.new("RGB", (max(widths), len(lines) * (height + 34)), "white")
    draw = ImageDraw.Draw(canvas)
    for number, (subject, crops, made) in enumerate(lines):
        x, y = 0, number * (height + 34)
        draw.text((4, y + 6), f"{source_id} {subject['id']} ({subject.get('view', '3d')}): original crops | "
                              f"reference sheet", fill="black", font=label)
        for path in [*crops, made]:
            with Image.open(path) as image:
                tile = image.convert("RGB").resize((round(image.width * height / image.height), height), Image.LANCZOS)
            canvas.paste(tile, (x, y + 30))
            x += tile.width + 8
    out = job / "sheets" / f"{source_id}-subjects.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=92)
    return out
