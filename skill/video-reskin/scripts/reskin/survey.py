"""Survey: Gemini watches each whole source once and names (1) the moments whose frames together show every part of
the location the camera travels across (they go into one big scene map, refs) and (2) the characters, animals,
vehicles, objects and props that come into view during the video, each with a moment it is seen clearly and its box
then (each gets a three-view sheet, refs), (3) where the closing card starts, (4) every logo, watermark, caption
and warning line drawn over the picture (to remove) and the HUD (to keep) and (5) the stretches that are not gameplay
(live action, real people, filmed hands or phones, non-game animation: `passthrough`, kept as the original and put
back after the gameplay is restyled). The frames and crops are cut here, and
Gemini checks them once more on the stills (sharp, uncovered, early enough, the whole subject). The agent reads the
result as text and looks only at the stills Gemini flagged, the end-card strip and one still per removal.
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
OVERLAY_KINDS = ("logo", "warning", "watermark", "caption", "hud")
REMOVED_KINDS = {"logo", "warning", "watermark", "caption"}     # the user's standing request; the HUD stays
OVERLAY_PLACES = ("top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right")
CHECK_SIZE = 1024          # long side of the stills Gemini checks

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
            "required": ["what", "kind", "appears", "start", "end", "at", "box_2d", "description"]}},
        "end_card": {"type": "OBJECT", "properties": {
            "present": {"type": "BOOLEAN"}, "start": {"type": "NUMBER"}, "how": {"type": "STRING"}},
            "required": ["present", "start", "how"]},
        "overlays": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "kind": {"type": "STRING", "enum": list(OVERLAY_KINDS)}, "text": {"type": "STRING"},
            "what": {"type": "STRING"}, "where": {"type": "STRING", "enum": list(OVERLAY_PLACES)},
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "at": {"type": "NUMBER"},
            "box_2d": {"type": "ARRAY", "items": {"type": "NUMBER"}}},
            "required": ["kind", "text", "what", "where", "start", "end", "at", "box_2d"]}},
        "non_game": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "what": {"type": "STRING"}},
            "required": ["start", "end", "what"]}}},
    "required": ["scenes", "subjects", "end_card", "overlays", "non_game"]}
CHECK_SCHEMA = {"type": "OBJECT", "properties": {"pictures": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "picture": {"type": "STRING"}, "ok": {"type": "BOOLEAN"},
    "problem": {"type": "STRING", "enum": ["none", "blurred", "covered", "late", "wrong_subject", "partial", "other"]},
    "note": {"type": "STRING"}}, "required": ["picture", "ok", "problem", "note"]}}}, "required": ["pictures"]}


def prompt(duration: float, end_card: float | None, overlays: list[dict] | None = None) -> str:
    end = (f" A measure found a picture change at about {end_card:.1f} s that may be where the closing card (big logo, "
           "store badges, call to action) begins; for 1 and 2 ignore everything from the closing card on." if end_card
           else " For 1 and 2 ignore the closing card (big logo, store badges, call to action) if there is one.")
    measured = "; ".join(f"{o['where']} box {o['box']} {o['start']:.1f}-{o['end']:.1f} s" for o in overlays or [])
    hint = (f" A measure found boxes that stay still on screen (each may be a logo, a warning line, a HUD counter or "
            f"just scenery standing still; x0, y0, x1, y1 fractions of the frame): {measured}." if measured else "")
    return " ".join(f"""
This {duration:.1f}-second video is a mobile game ad rendered in 2.5D (a top-down / isometric camera that travels
over the game world). It will be restyled in segments that share reference pictures: one map of each location and
one sheet of each subject. Watch the whole video and answer the five parts below.{end} Ignore the non-game
stretches (part 5) for parts 1 and 2. Write every text in English.

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

3. overlays: everything drawn over the game picture rather than part of the world: the game's logo or title,
watermarks, captions, the small legal or warning lines along an edge (`warning`), and the HUD (counters, prices,
buttons, icons, progress bars: `hud`).{hint} One item per thing and per place: a logo that moves to another corner
is one item for each corner, with its own span. For each: `kind`, `text` = the characters it shows, exactly as on
screen in their own script (empty for a picture-only logo), `what` = an English phrase that names it for a video
model, quoting its text, e.g. `the game logo "TITLE" in the top-left corner` or `the two lines of small white
warning text along the bottom`, `where`, the span it is on screen (`start`, `end`), a moment `at` inside the span, and
`box_2d` at that moment. Leave out what is part of the world (signs on buildings, labels on goods).

4. end_card: `present` = whether the video ends on a closing card, `start` = the time of the last frame before its
transition (wipe, flash, blur, logo zoom) begins, or the video's length when there is none, and `how` = how the game
picture gives way to it.

5. non_game: every stretch before the closing card that is not the game's own rendered picture: live-action footage
(real people, a presenter or player talking to the camera, filmed hands holding a phone, real places), filmed or
screen-recorded footage of something else, or animation that is not the game's world. A real person's face or hands
in a small picture-in-picture over gameplay does not count; the gameplay stays. For each: `start` = its first frame,
`end` = the first frame of gameplay after it, and `what` = a short phrase of what it shows. An empty list when the
whole video is gameplay.

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
    overlays = []
    for item in answer.get("overlays") or []:
        box = item.get("box_2d") or []
        fractions = None
        if len(box) == 4:
            y0, x0, y1, x1 = (min(max(float(v) / 1000, 0.0), 1.0) for v in box)
            fractions = [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)] if x1 > x0 and y1 > y0 else None
        start, end = clamp(item.get("start", 0)), round(min(float(item.get("end", duration)), duration), 2)
        overlays.append({"id": f"O{len(overlays) + 1}", "kind": item.get("kind", "hud"), "text": item.get("text", ""),
                         "what": item.get("what", ""), "where": item.get("where", ""), "start": start, "end": end,
                         "at": round(min(max(clamp(item.get("at", start)), start), end), 2), "box": fractions,
                         "remove": item.get("kind") in REMOVED_KINDS})
    card = answer.get("end_card") or {}
    end_card = ({"start": clamp(card.get("start", duration)), "how": card.get("how", "")}
                if card.get("present") and float(card.get("start", duration)) < duration - .1 else None)
    non_game = []
    for item in answer.get("non_game") or []:
        start, end = clamp(item.get("start", 0)), round(min(float(item.get("end", duration)), duration), 2)
        if end - start >= .1:
            non_game.append({"start": start, "end": end, "what": item.get("what", "")})
    return {"scenes": scenes, "subjects": subjects[:MAX_SUBJECTS], "end_card": end_card, "overlays": overlays,
            "non_game": sorted(non_game, key=lambda x: x["start"])}


def suggested_edits(result: dict, duration: float) -> dict:
    """The edits entry the survey points to: kept up to the end card, the non-game stretches as passthrough (kept as
    the original), the overlays to remove as timed items (a timed item stops the video model from drawing text where
    it is not on screen; one only over passthrough footage is left out, that footage is not restyled)."""
    keep_end = result["end_card"]["start"] if result.get("end_card") else round(duration, 2)
    passthrough = [[x["start"], min(x["end"], keep_end)] for x in result.get("non_game") or [] if x["start"] < keep_end]
    covered = lambda o: any(a <= o["start"] and min(o["end"], keep_end) <= b for a, b in passthrough)
    remove = [{"what": o["what"], "start": o["start"], "end": min(o["end"], keep_end)}
              for o in result.get("overlays") or [] if o["remove"] and o["what"] and o["start"] < keep_end
              and not covered(o)]
    return {"keep": [0, keep_end], **({"passthrough": passthrough} if passthrough else {}), "remove": remove}


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
    log(f"{row['id']}: Gemini watches the whole source ({clip.stat().st_size / 1e6:.1f} MB) for scenes, subjects, "
        "overlays and the end card")
    rate = winky.sample_fps()
    part = winky.inline(clip, "video/mp4", fps=rate)
    answer = winky.gemini([part, {"text": prompt(catalog_row["duration"], card, catalog_row.get("overlays"))}],
                          schema=SCHEMA)
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
    views, crops = pictures(job, row["id"], source, result, stills)
    result["checks"] = check_pictures(result, views, crops)
    measured_spans(result, catalog_row.get("overlays"))
    result["overlay_stills"] = overlay_stills(job, row["id"], source, result)
    result["non_game_stills"] = [str(boundary_strip(source, x, folder / f"non-game-{x['start']:07.2f}.png",
                                                    catalog_row["duration"]))
                                 for x in result["non_game"]]
    if result["end_card"]:
        result["end_strip"] = str(end_strip(source, result["end_card"]["start"],
                                            folder / f"end-{result['end_card']['start']:07.2f}.png"))
    result["suggested"] = suggested_edits(result, catalog_row["duration"])
    write(job / "catalog/survey" / f"{row['id']}.json", result)
    return result


def scaled(path: Path, output: Path, longest: int = CHECK_SIZE) -> Path:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((longest, longest), Image.LANCZOS)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, quality=92)
    return output


def check_pictures(result: dict, views: list[dict], crops: list[dict]) -> list[dict]:
    """Gemini looks at every view frame and subject crop once more, as stills: a blurred, covered or late view, or a
    crop that misses its subject, would be copied into the scene map or a sheet. Returns only the flagged ones."""
    later = "; ".join(f"{s['what']} (from {s['start']:g} s)" for s in result["subjects"] if s["appears"] == "later")
    parts, labels = [], {}
    for item in views + crops:
        labels[item["label"]] = item
        small = scaled(Path(item["path"]), Path(item["path"]).with_suffix(".check.jpg"))
        parts += [{"text": f"Picture {item['label']}: {item['about']}"}, winky.inline(small, "image/jpeg")]
    parts.append({"text": " ".join(f"""
These pictures were picked from a 2.5D game ad as references for restyling it. Views are frames that will be joined
into one map of the location; crops are cut around a subject and will become its three-view sheet. Check each picture
and answer for every one: ok, or the problem: `blurred` (motion blur, out of focus), `covered` (a pop-up, menu, text
card, transition or a crowd hides much of the world), `late` (the place itself already looks as it only does later
in the video: something built, piled up, delivered, unlocked or lit that is not there at first, so the map would
show it from the start; characters, animals and carried or flying items standing in a view are fine, the map leaves
them out; subjects coming later: {later or 'none listed'}),
`wrong_subject` (the crop shows something else than its subject), `partial` (the crop cuts off a large part of its
subject) or `other`. `note`: one short sentence in Chinese. Use the picture names as given.""".split())})
    try:
        answer = winky.gemini(parts, schema=CHECK_SCHEMA, timeout=600)
    except (RuntimeError, ValueError) as error:
        log(f"survey picture check failed ({error}); look at the survey sheet instead")
        return [{"picture": "all", "problem": "unchecked", "note": str(error)[:200], "path": ""}]
    flagged = []
    for item in answer.get("pictures") or []:
        known = labels.get(item.get("picture"))
        if known and not item.get("ok") and item.get("problem") != "none":
            flagged.append({"picture": item["picture"], "problem": item.get("problem", "other"),
                            "note": item.get("note", ""), "path": known["path"]})
    return flagged


def marked(still: Path, box: list[float] | None, label: str, output: Path, width: int = 540) -> Path:
    """A frame at `width` with the box drawn in red and a label, for the agent to confirm one item."""
    with Image.open(still) as image:
        image = image.convert("RGB")
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    canvas = Image.new("RGB", (image.width, image.height + 26), "white")
    canvas.paste(image, (0, 26))
    draw = ImageDraw.Draw(canvas)
    draw.text((4, 4), label[:70], fill="black", font=font(16))
    if box:
        x0, y0, x1, y1 = box
        draw.rectangle([x0 * image.width, 26 + y0 * image.height, x1 * image.width, 26 + y1 * image.height],
                       outline="red", width=3)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def overlay_stills(job: Path, source_id: str, source: Path, result: dict) -> dict:
    """One strip per overlay to remove, at the start, middle and end of its span with its box in red: the agent
    confirms the text, the place and the span on it (a logo that moves to another corner shows on it)."""
    folder = job / "catalog/survey" / source_id
    paths = {}
    for overlay in result["overlays"]:
        if not overlay["remove"]:
            continue
        a, b = overlay["start"], max(overlay["start"], overlay["end"] - .1)
        tiles = []
        for k, moment in enumerate((a, (a + b) / 2, b)):
            full = frame(source, moment, folder / f"{overlay['id']}-{k}.png")
            tiles.append(marked(full, overlay["box"], f"{overlay['id']} {moment:.2f} s",
                                folder / f"{overlay['id']}-{k}m.png", 360))
        paths[overlay["id"]] = str(join_tiles(tiles, folder / f"{overlay['id']}.png"))
    return paths


def measured_spans(result: dict, measured: list[dict]) -> None:
    """Beside each removal Gemini named, the still box analyze measured at the same place (`measured`), so a span
    that differs (a logo that leaves its corner) shows in the text."""
    for overlay in result["overlays"]:
        box = overlay.get("box")
        if not overlay["remove"] or not box:
            continue
        for candidate in measured or []:
            x0, y0, x1, y1 = candidate["box"]
            inter = max(0.0, min(x1, box[2]) - max(x0, box[0])) * max(0.0, min(y1, box[3]) - max(y0, box[1]))
            if inter > .3 * min((x1 - x0) * (y1 - y0), (box[2] - box[0]) * (box[3] - box[1])):
                overlay["measured"] = f"{candidate['start']:g}-{candidate['end']:g} s"
                break


def join_tiles(tiles: list[Path], output: Path) -> Path:
    images = [Image.open(path) for path in tiles]
    canvas = Image.new("RGB", (sum(i.width for i in images), max(i.height for i in images)), "white")
    x = 0
    for image in images:
        canvas.paste(image, (x, 0))
        x += image.width
        image.close()
    canvas.save(output)
    return output


def end_strip(source: Path, t: float, output: Path, width: int = 360) -> Path:
    """The last kept moment between half a second before and after it: the agent confirms the end card's start."""
    tiles = []
    for k, moment in enumerate((t - .5, t, t + .5)):
        full = frame(source, moment, output.with_name(f"{output.stem}-{k}.png"))
        tiles.append(marked(full, None, f"{moment:.2f} s{' (kept up to here)' if k == 1 else ''}",
                            output.with_name(f"{output.stem}-{k}m.png"), width))
    return join_tiles(tiles, output)


def boundary_strip(source: Path, item: dict, output: Path, duration: float, width: int = 300) -> Path:
    """A non-game stretch's two boundaries, each with a frame just outside and just inside: the agent confirms what it
    shows and where it begins and ends."""
    a, b = item["start"], item["end"]
    moments = [(a - .3, "before"), (a + .05, "starts"), (b - .1, "ends"), (b + .1, "after")]
    tiles = []
    for k, (moment, label) in enumerate(moments):
        moment = min(max(0.0, moment), max(0.0, duration - .05))
        full = frame(source, moment, output.with_name(f"{output.stem}-{k}.png"))
        tiles.append(marked(full, None, f"{moment:.2f} s {label}", output.with_name(f"{output.stem}-{k}m.png"), width))
    return join_tiles(tiles, output)


def pictures(job: Path, source_id: str, source: Path, result: dict,
             stills: list[Path]) -> tuple[list[dict], list[dict]]:
    """Frames of every view and crops of every subject (from its still) in catalog/survey/<id>/, and the sheet of
    them all (`catalog/<id>-survey.jpg`, for a look when the check flags much)."""
    folder = job / "catalog/survey" / source_id
    views, crops = [], []
    for k, scene in enumerate(result["scenes"], 1):
        for t in scene["views"]:
            path = frame(source, t, folder / f"scene{k}-{t:07.2f}.png")
            views.append({"label": f"scene{k}@{t:.2f}", "path": str(path),
                          "about": f"view of the location {scene['name']} at {t:.2f} s"})
    for k, (subject, still) in enumerate(zip(result["subjects"], stills), 1):
        path = crop(still, subject["box"], folder / f"P{k:02d}.png")
        crops.append({"label": f"P{k:02d}", "path": str(path),
                      "about": f"crop of {subject['what']} ({subject.get('description', '')}) at {subject['at']:.2f} s"})
    sheet(job / "catalog" / f"{source_id}-survey.jpg",
          [(Path(v["path"]), f"{v['label']} view") for v in views]
          + [(Path(c["path"]), f"{c['label']} {s['what']}") for c, s in zip(crops, result["subjects"])])
    return views, crops


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
