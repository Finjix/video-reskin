"""Survey: Gemini watches each whole source once and names (1) the moments whose frames together show every part of
the location the camera travels across (they go into one big scene map, refs) and (2) the characters, animals,
vehicles, objects and props that come into view during the video, each with a moment it is seen clearly and its box
then (each gets a three-view sheet, refs). The frames and crops are cut here; the agent checks them on
`catalog/<id>-survey.jpg` and corrects `scenes` / `subjects` in the edits file when Gemini picked badly.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import winky
from reskin.analyze import font
from reskin.common import FFMPEG, log, read, run, video_info, write
from reskin.plan import MAX_SUBJECTS, SUBJECT_KINDS

PROXY_SHORT = 720          # short side of the clip Gemini watches (it judges content and boxes, not texture)
PROXY_FPS = 24
CROP_MARGIN = .12          # share of the box added on each side of a subject crop
MIN_CROP = 512             # crops are scaled up to at least this on the short side for GPT Image

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "scenes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
            "description": {"type": "STRING"},
            "views": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "time": {"type": "NUMBER"}, "shows": {"type": "STRING"}}, "required": ["time", "shows"]}}},
            "required": ["name", "start", "end", "description", "views"]}},
        "subjects": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "what": {"type": "STRING"}, "kind": {"type": "STRING", "enum": list(SUBJECT_KINDS)},
            "appears": {"type": "STRING", "enum": ["from_start", "later"]},
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "at": {"type": "NUMBER"},
            "box_2d": {"type": "ARRAY", "items": {"type": "NUMBER"}},
            "description": {"type": "STRING"}},
            "required": ["what", "kind", "appears", "start", "end", "at", "box_2d", "description"]}}},
    "required": ["scenes", "subjects"]}


def prompt(duration: float, end_card: float | None) -> str:
    end = (f" The video's closing card (logo, store badges) starts at about {end_card:.1f} s: ignore everything from "
           "there on." if end_card else "")
    return " ".join(f"""
This {duration:.1f}-second video is a mobile game ad rendered in 2.5D (a top-down / isometric camera that travels
over the game world). It will be restyled in segments that share reference pictures: one map of each location and
one sheet of each subject. Watch the whole video and answer two things.{end} Write every text in English.

1. scenes: the distinct locations the camera shows (usually one; a new one only after a camera cut to a different
place). For each: a short `name`, its time span, a one-sentence `description`, and 3 to 8 `views`: moments whose
frames, put together, show every part of that location the camera ever passes over (where the camera starts, the far
ends of its travel, every area revealed later), each with what part it `shows`. Take each area as early as it is
clearly in view: when the place fills up or changes during the video (things are built, piled up, delivered, unlocked
or lit), the map must show it as it first looks; what comes later belongs to the subjects. Pick moments where the
picture is sharp (the camera momentarily slow, no motion blur), the full frame shows the world (no pop-up, menu,
transition or text card covering it) and as few characters as possible stand in front.

2. subjects: every character, animal, vehicle, object or prop that is not visible at the start and comes into view
during the video (walks in, spawns, is built, pops up, is delivered), and the main characters present from the start
that move around (the player's character, workers). At most {MAX_SUBJECTS}, the most prominent first. Many identical
copies (a crowd of the same workers, stacks of the same fish) are one subject; different forms of one thing (a
whole fish and the steaks cut from it, goods and the cash paid for them) are separate subjects, and so are items that
are produced, carried, piled up or handed over. Leave out UI, HUD, numbers, icons,
buttons, logos, text and closing-card content, and fixed scenery (that belongs to the scene). For each: a short
`what` naming it as itself (`lumberjack in a blue winter coat`, `whole tuna`), its `kind`, whether it `appears`
from_start or later, the span it is on screen (`start`, `end`), a moment `at` where it is fully in view, large,
sharp, unoccluded and not moving fast, and `box_2d` = [ymin, xmin, ymax, xmax] of it at that moment on a 0-1000
scale of the frame; a one-sentence `description` of its look (shape, colours, clothing, parts).

All times are seconds from the start of the video.""".split())


def proxy(source: Path, info: dict, output: Path) -> Path:
    """The source at PROXY_SHORT on the short side, 24 fps, no audio (small enough to send inline)."""
    scale = min(1.0, PROXY_SHORT / min(info["width"], info["height"]))
    width, height = round(info["width"] * scale / 2) * 2, round(info["height"] * scale / 2) * 2
    output.parent.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-i", source, "-vf", f"fps={PROXY_FPS},scale={width}:{height}:flags=lanczos",
         "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", output])
    return output


def frame(source: Path, t: float, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, t):.4f}", "-i", source, "-frames:v", "1", output])
    return output


def crop(full: Path, box: list[float], output: Path) -> Path:
    """The subject's box (fractions x0, y0, x1, y1) with a margin, scaled up to MIN_CROP on the short side."""
    with Image.open(full) as image:
        image = image.convert("RGB")
        w, h = image.size
        x0, y0, x1, y1 = box
        mx, my = (x1 - x0) * CROP_MARGIN, (y1 - y0) * CROP_MARGIN
        region = (max(0, round((x0 - mx) * w)), max(0, round((y0 - my) * h)),
                  min(w, round((x1 + mx) * w)), min(h, round((y1 + my) * h)))
        part = image.crop(region)
        short = min(part.size)
        if short < MIN_CROP:
            factor = MIN_CROP / max(1, short)
            part = part.resize((round(part.width * factor), round(part.height * factor)), Image.LANCZOS)
        output.parent.mkdir(parents=True, exist_ok=True)
        part.save(output)
    return output


def normalise(answer: dict, duration: float) -> dict:
    """Gemini's answer in the edits format: views as times, boxes as [x0, y0, x1, y1] fractions, times clamped."""
    clamp = lambda t: round(min(max(float(t), 0.0), max(0.0, duration - .05)), 2)
    scenes = []
    for scene in answer.get("scenes") or []:
        views = sorted({clamp(v["time"]) for v in scene.get("views") or [] if "time" in v})[:12]
        if views:
            scenes.append({"name": scene.get("name") or "scene", "start": clamp(scene.get("start", 0)),
                           "end": round(min(float(scene.get("end", duration)), duration), 2), "views": views,
                           "description": scene.get("description", ""),
                           "shows": {f"{clamp(v['time']):g}": v.get("shows", "") for v in scene.get("views") or []
                                     if "time" in v}})
    subjects = []
    for item in answer.get("subjects") or []:
        box = item.get("box_2d") or []
        if len(box) != 4:
            continue
        y0, x0, y1, x1 = (min(max(float(v) / 1000, 0.0), 1.0) for v in box)
        if x1 - x0 < .01 or y1 - y0 < .01:
            continue
        subjects.append({"what": item["what"], "kind": item.get("kind", "object"), "appears": item.get("appears", "later"),
                         "start": clamp(item.get("start", 0)), "end": round(min(float(item.get("end", duration)), duration), 2),
                         "at": clamp(item.get("at", item.get("start", 0))),
                         "box": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                         "description": item.get("description", "")})
    return {"scenes": scenes, "subjects": subjects[:MAX_SUBJECTS]}


BOX_SCHEMA = {"type": "OBJECT", "properties": {"found": {"type": "BOOLEAN"},
                                               "box_2d": {"type": "ARRAY", "items": {"type": "NUMBER"}}},
              "required": ["found", "box_2d"]}


def refine_box(still: Path, subject: dict) -> list[float] | None:
    """Gemini finds the subject again on the still itself: boxes read off a video are often off by a body width,
    boxes on a still image are tight."""
    text = (f"Find {subject['what']} ({subject.get('description', '')}) in this frame of a 2.5D game video. If there "
            "are several, take the single most complete, largest and least covered one. found = false if it is not "
            "in the frame. box_2d = [ymin, xmin, ymax, xmax] of the whole of it on a 0-1000 scale, tight around it.")
    try:
        answer = winky.gemini([winky.inline(still, "image/png"), {"text": text}], schema=BOX_SCHEMA, timeout=300)
    except (RuntimeError, ValueError) as error:
        log(f"box check of {subject['what']} failed ({error}); the box from the video stays")
        return None
    box = answer.get("box_2d") or []
    if not answer.get("found") or len(box) != 4:
        return None
    y0, x0, y1, x1 = (min(max(float(v) / 1000, 0.0), 1.0) for v in box)
    return [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)] if x1 - x0 > .01 and y1 - y0 > .01 else None


def survey_source(job: Path, row: dict, catalog_row: dict) -> dict:
    source = Path(row["path"])
    info = video_info(source)
    clip = proxy(source, info, job / "catalog/survey" / f"{row['id']}-proxy.mp4")
    card = (catalog_row.get("end_card") or {}).get("start")
    log(f"{row['id']}: Gemini watches the whole source ({clip.stat().st_size / 1e6:.1f} MB) for scenes and subjects")
    rate = winky.sample_fps(catalog_row["duration"])
    part = winky.inline(clip, "video/mp4", fps=rate)
    answer = winky.gemini([part, {"text": prompt(catalog_row["duration"], card)}], schema=SCHEMA)
    result = normalise(answer, catalog_row["duration"])
    for subject in result["subjects"]:      # the moment Gemini saw: its sampled frames lie on a 1/rate grid
        subject["at"] = round(round(subject["at"] * rate) / rate, 2)
    folder = job / "catalog/survey" / row["id"]
    stills = [frame(source, s["at"], folder / f"P{k:02d}-frame.png") for k, s in enumerate(result["subjects"], 1)]
    with ThreadPoolExecutor(4) as executor:
        boxes = list(executor.map(lambda pair: refine_box(*pair), zip(stills, result["subjects"])))
    for subject, box in zip(result["subjects"], boxes):
        if box:
            subject["video_box"], subject["box"] = subject["box"], box
    result["raw"] = answer
    write(job / "catalog/survey" / f"{row['id']}.json", result)
    pictures(job, row["id"], source, result, stills)
    return result


def pictures(job: Path, source_id: str, source: Path, result: dict, stills: list[Path]) -> Path:
    """Frames of every view and crops of every subject (from its still) in catalog/survey/<id>/, and the sheet the
    agent checks."""
    folder = job / "catalog/survey" / source_id
    tiles = []
    for k, scene in enumerate(result["scenes"], 1):
        for t in scene["views"]:
            path = frame(source, t, folder / f"scene{k}-{t:07.2f}.png")
            tiles.append((path, f"scene {k} {scene['name']} view {t:.2f} s"))
    for k, (subject, still) in enumerate(zip(result["subjects"], stills), 1):
        path = crop(still, subject["box"], folder / f"P{k:02d}.png")
        tiles.append((path, f"P{k:02d} {subject['what']} at {subject['at']:.2f} s"))
    return sheet(job / "catalog" / f"{source_id}-survey.jpg", tiles)


def sheet(out: Path, tiles: list[tuple[Path, str]], cell: int = 360) -> Path:
    columns = 5
    rows = max(1, -(-len(tiles) // columns))
    label = font(16)
    canvas = Image.new("RGB", (columns * cell, rows * (cell + 24)), "white")
    draw = ImageDraw.Draw(canvas)
    for number, (path, text) in enumerate(tiles):
        x, y = (number % columns) * cell, (number // columns) * (cell + 24)
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((cell - 6, cell - 6), Image.LANCZOS)
            canvas.paste(image, (x + (cell - image.width) // 2, y + 22 + (cell - 6 - image.height) // 2))
        draw.text((x + 4, y + 3), text[:44], fill="black", font=label)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=88)
    return out


def run_all(job: Path, state: dict, catalog: dict, only: set[str] | None = None, redo: bool = False) -> list[dict]:
    rows = [r for r in state["sources"] if not only or r["id"] in only]
    known = {r["id"]: r for r in catalog["sources"]}

    def one(row):
        path = job / "catalog/survey" / f"{row['id']}.json"
        if path.is_file() and not redo:
            return {"id": row["id"], "reused": True, **read(path)}
        try:
            return {"id": row["id"], **survey_source(job, row, known[row["id"]])}
        except (RuntimeError, ValueError) as error:
            return {"id": row["id"], "error": str(error)}
    with ThreadPoolExecutor(max(1, min(4, len(rows)))) as executor:
        return list(executor.map(one, rows))
