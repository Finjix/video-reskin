"""The agent's edits (kept range, materials, what to remove, scenes and subjects) and the segment plan built from them.

Seedance 2.5 edits clips of 2-30 s, so each kept range is cut into segments of at most 30 s: at camera cuts where
possible (a hard cut hides the seam), otherwise inside a shot at the moment the camera moves fastest (or the picture
jumps anyway: a flash, a burst, a whip pan), where `assemble` joins them with a plain cut (or a very light dissolve
where a cut would show). Every segment takes the scene map of its location and at most MAX_SHEETS three-view sheets
(refs); a segment that continues a shot also takes the previous result's frame where it begins (render).

Around a seam inside a shot both neighbours render OVERLAP_SECONDS more (half on either side); a render shorter than
MIN_RENDER is padded to it; `assemble` trims the padding.
"""
from __future__ import annotations

import math
from pathlib import Path

from reskin.common import read, write, write_text
from reskin.prompts import describe

MAX_SECONDS = 30.0        # Seedance 2.5 video edit: reference videos of 2-30 s, the result as long as the input
MIN_RENDER = 4            # seconds a render lasts at least (shorter clips are padded)
MIN_SECONDS = 2.0         # delivered pieces shorter than this are merged into a neighbour
OVERLAP_SECONDS = .6      # rendered by both segments around a seam inside a shot (half before, half after)
SEARCH_SECONDS = 7.0      # a seam inside a shot is searched in the last this-many seconds a segment can reach
SMOOTH_SECONDS = .15      # camera speed is averaged over this much on either side of a candidate moment
EDIT_KEYS = {"keep", "materials", "remove", "extra", "skip", "style_from", "split_at", "max_segment_seconds",
             "scenes", "subjects", "sheets", "motion"}
SUBJECT_KINDS = ("character", "animal", "vehicle", "object", "prop", "building")
MAX_SUBJECTS = 8          # subjects one source lists at most
# The more three-view sheets a segment takes, the more the video model features its subjects (close-ups of its own,
# dev/VALIDATION.md). A segment takes at most MAX_SHEETS (assign_refs picks them); only sheets some segment takes are
# made (refs).
MAX_SHEETS = 2


def items_problem(items, name: str) -> str | None:
    """`remove` (and `materials` when not one text) is a list of descriptions, each a string or
    {"what": ..., "start": s, "end": s} for something on screen only part of the time."""
    if not isinstance(items, list):
        return f"{name} must be a list"
    for item in items:
        if isinstance(item, str) and item.strip():
            continue
        if (isinstance(item, dict) and isinstance(item.get("what"), str) and item["what"].strip()
                and set(item) <= {"what", "start", "end"}
                and all(isinstance(item.get(k, 0), (int, float)) for k in ("start", "end"))
                and item.get("start", 0) <= item.get("end", float("inf"))):
            continue
        return f"{name} item {item!r} must be text or {{\"what\": text, \"start\": seconds, \"end\": seconds}}"
    return None


def scenes_problem(scenes, duration: float) -> str | None:
    """`scenes`: [{"name": text, "start": s, "end": s, "views": [seconds, ...]}], views inside the source."""
    if not isinstance(scenes, list) or not scenes:
        return "scenes must be a non-empty list"
    for scene in scenes:
        if not (isinstance(scene, dict) and isinstance(scene.get("name"), str) and scene["name"].strip()
                and set(scene) <= {"name", "start", "end", "views", "description", "shows"}
                and isinstance(scene.get("views"), list) and 1 <= len(scene["views"]) <= 12
                and all(isinstance(t, (int, float)) and 0 <= t < duration for t in scene["views"])
                and all(isinstance(scene.get(k, 0), (int, float)) for k in ("start", "end"))):
            return (f"scene {scene!r} must be {{\"name\": text, \"start\": s, \"end\": s, \"views\": [1-12 times "
                    "inside the source]}")
    return None


def subjects_problem(subjects, duration: float) -> str | None:
    """`subjects`: [{"what": text, "kind": ..., "start": s, "end": s, "at": s, "box": [x0, y0, x1, y1]}], box in
    fractions of the frame at `at`."""
    if not isinstance(subjects, list):
        return "subjects must be a list"
    if len(subjects) > MAX_SUBJECTS:
        return f"subjects: at most {MAX_SUBJECTS} per source (keep the ones that matter most)"
    for item in subjects:
        box = item.get("box") if isinstance(item, dict) else None
        if not (isinstance(item, dict) and isinstance(item.get("what"), str) and item["what"].strip()
                and set(item) <= {"what", "kind", "start", "end", "at", "box", "video_box", "appears", "description"}
                and item.get("kind", "object") in SUBJECT_KINDS
                and isinstance(item.get("at"), (int, float)) and 0 <= item["at"] < duration
                and all(isinstance(item.get(k, 0), (int, float)) for k in ("start", "end"))
                and isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
                and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
            return (f"subject {item!r} must be {{\"what\": text, \"kind\": one of {', '.join(SUBJECT_KINDS)}, "
                    "\"start\": s, \"end\": s, \"at\": s, \"box\": [x0, y0, x1, y1] fractions of the frame}")
    return None


def check_edits(value: dict, catalog: dict) -> dict:
    """Validate the edits file against the catalog; returns it normalised."""
    if not isinstance(value, dict) or not isinstance(value.get("sources", {}), dict):
        raise ValueError('edits must look like {"defaults": {...}, "sources": {"S01": {...}}}')
    known = {row["id"]: row for row in catalog["sources"]}
    defaults = value.get("defaults") or {}
    problems = []
    for key in set(defaults) - EDIT_KEYS:
        problems.append(f"defaults: unknown key {key}")
    for key in ("scenes", "subjects", "keep", "split_at", "sheets", "motion"):
        if key in defaults:
            problems.append(f"defaults: {key} belongs to a source")
    if "remove" in defaults and items_problem(defaults["remove"], "remove"):
        problems.append(f"defaults: {items_problem(defaults['remove'], 'remove')}")
    out = {"defaults": defaults, "sources": {}}
    for source_id, row in (value.get("sources") or {}).items():
        if source_id not in known:
            problems.append(f"{source_id}: not a source of this job")
            continue
        for key in set(row) - EDIT_KEYS:
            problems.append(f"{source_id}: unknown key {key}")
        duration = known[source_id]["duration"]
        keep = row.get("keep")
        if keep is not None:
            if (not isinstance(keep, list) or len(keep) != 2 or not all(isinstance(x, (int, float)) for x in keep)
                    or not 0 <= keep[0] < keep[1] <= duration + .05):
                problems.append(f"{source_id}: keep must be [start, end] seconds inside 0-{duration:.2f}")
            elif keep[1] - keep[0] < MIN_RENDER:
                problems.append(f"{source_id}: keep must be at least {MIN_RENDER} s (the shortest render)")
        if "extra" in row and not isinstance(row["extra"], str):
            problems.append(f"{source_id}: extra must be text")
        if "materials" in row and not isinstance(row["materials"], str) and items_problem(row["materials"], "materials"):
            problems.append(f"{source_id}: {items_problem(row['materials'], 'materials')}")
        if "remove" in row and items_problem(row["remove"], "remove"):
            problems.append(f"{source_id}: {items_problem(row['remove'], 'remove')}")
        if "scenes" in row and scenes_problem(row["scenes"], duration):
            problems.append(f"{source_id}: {scenes_problem(row['scenes'], duration)}")
        if "subjects" in row and subjects_problem(row["subjects"], duration):
            problems.append(f"{source_id}: {subjects_problem(row['subjects'], duration)}")
        if row.get("style_from") and (row["style_from"] not in known or row["style_from"] == source_id):
            problems.append(f"{source_id}: style_from must name another source")
        if "split_at" in row and not (isinstance(row["split_at"], list) and all(isinstance(x, (int, float)) for x in row["split_at"])):
            problems.append(f"{source_id}: split_at must be a list of times in seconds")
        if "max_segment_seconds" in row and not (isinstance(row["max_segment_seconds"], (int, float))
                                                 and MIN_RENDER <= row["max_segment_seconds"] <= MAX_SECONDS):
            problems.append(f"{source_id}: max_segment_seconds must be {MIN_RENDER}-{MAX_SECONDS:g}")
        sheets = row.get("sheets")
        if sheets is not None and not (isinstance(sheets, dict) and all(
                isinstance(k, str) and isinstance(v, list) and len(v) <= MAX_SHEETS
                and all(isinstance(x, int) and 1 <= x <= MAX_SUBJECTS for x in v) for k, v in sheets.items())):
            problems.append(f"{source_id}: sheets must look like {{\"K03\": [8, 1]}}: per segment at most "
                            f"{MAX_SHEETS} subject numbers (P08, P01 as in reports/plan.md)")
        motion = row.get("motion")
        if motion is not None and not (isinstance(motion, dict) and all(
                isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("what"), str)
                and isinstance(v.get("at"), (int, float)) and isinstance(v.get("box"), list) and len(v["box"]) == 4
                and isinstance(v.get("step", .1), (int, float)) and .02 <= v.get("step", .1) <= 1
                for k, v in motion.items())):
            problems.append(f"{source_id}: motion must look like {{\"K04\": {{\"what\": \"whole tuna on the "
                            f"conveyor belt\", \"at\": 53.0, \"box\": [x0, y0, x1, y1], \"step\": 0.1}}}}")
        out["sources"][source_id] = row
    chains = {sid: row.get("style_from") for sid, row in out["sources"].items() if row.get("style_from")}
    for sid in chains:
        seen, current = {sid}, chains.get(sid)
        while current:
            if current in seen:
                problems.append(f"{sid}: style_from forms a loop")
                break
            seen.add(current)
            current = chains.get(current)
    if problems:
        raise ValueError("edits problems:\n  " + "\n  ".join(problems))
    return out


def source_edits(edits: dict, source_id: str, catalog_row: dict, style: dict, survey: dict | None = None) -> dict:
    """Edits for one source with defaults and the survey (scenes, subjects) filled in."""
    defaults = edits.get("defaults") or {}
    own = (edits.get("sources") or {}).get(source_id, {})
    row = {**{k: v for k, v in defaults.items() if k not in ("keep", "split_at")}, **own}
    card = catalog_row.get("end_card")
    keep = row.get("keep") or [0.0, card["start"] if card else catalog_row["duration"]]
    remove = []
    for item in [*(defaults.get("remove") or []), *(own.get("remove") or [])]:
        if item not in remove:
            remove.append(item)
    survey = survey or {}
    scenes = row.get("scenes") or survey.get("scenes") or [
        {"name": "scene", "start": 0.0, "end": catalog_row["duration"], "views": [round((keep[0] + keep[1]) / 2, 2)]}]
    subjects = row.get("subjects") if "subjects" in row else (survey.get("subjects") or [])
    # only what the kept range shows: views and sheets of the rest cost money and add places nobody sees
    inside = lambda item: overlaps(item, keep[0], keep[1])
    trimmed = []
    for scene in filter(inside, scenes):
        views = [t for t in scene["views"] if keep[0] <= t <= keep[1]] or scene["views"]
        trimmed.append({**scene, "views": views})
    scenes = trimmed or scenes[:1]
    subjects = [s for s in subjects if inside(s)]
    return {"keep": [float(keep[0]), float(keep[1])], "materials": row.get("materials") or style["default_materials"],
            "remove": remove, "extra": row.get("extra", ""), "skip": bool(row.get("skip")),
            "style_from": row.get("style_from"), "split_at": row.get("split_at") or [],
            "max_segment_seconds": float(row.get("max_segment_seconds") or 0) or None,
            "scenes": scenes, "subjects": subjects[:MAX_SUBJECTS], "sheets": row.get("sheets") or {},
            "motion": row.get("motion") or {}}


def seam_scores(motion: list[float], camera: list[list[float]], fps: float, frames: int) -> list[float]:
    """How well each frame hides a seam: the camera's speed on screen, or a jump of the whole picture (flash,
    burst), each against what is usual in this source; averaged over SMOOTH_SECONDS on either side."""
    speed = [math.hypot(*v) for v in camera] if camera else []
    speed += [0.0] * (frames - len(speed))
    change = list(motion) + [0.0] * (frames - len(motion))

    def scale(values):
        ordered = sorted(v for v in values if v > 0)
        return ordered[int(.9 * (len(ordered) - 1))] if ordered else 1.0
    s_speed, s_change = max(scale(speed), 1e-3), max(scale(change), 1e-3)
    raw = [max(speed[i] / s_speed, change[i] / s_change) for i in range(frames)]
    half = max(1, round(SMOOTH_SECONDS * fps))
    prefix = [0.0]
    for value in raw:
        prefix.append(prefix[-1] + value)
    return [(prefix[min(frames, i + half + 1)] - prefix[max(0, i - half)]) / (min(frames, i + half + 1) - max(0, i - half))
            for i in range(frames)]


def segment(catalog_row: dict, keep: list[float], max_seconds: float, motion: list[float],
            camera: list[list[float]], split_at: list[float]) -> list[dict]:
    fps = catalog_row["info"]["fps"]
    total = catalog_row["info"]["frames"]
    first, last = round(keep[0] * fps), min(total, round(keep[1] * fps))
    shot_starts = {s["frames"][0] for s in catalog_row["shots"]}
    cuts = sorted(c for c in shot_starts | {round(t * fps) for t in split_at} if first < c < last)
    forced = sorted({round(t * fps) for t in split_at if first < round(t * fps) < last})
    minimum = round(MIN_SECONDS * fps)
    half = round(OVERLAP_SECONDS * fps / 2)
    maximum = int(max_seconds * fps)
    search = round(SEARCH_SECONDS * fps)
    scores = seam_scores(motion, camera, fps, total)
    # Walk forward: end each segment at the latest camera cut that fits, else at the moment the camera moves
    # fastest in the last SEARCH_SECONDS the segment can reach (leaving room for the overlap on both sides).
    spans, position, seam = [], first, "start"
    while True:
        lead = half if seam == "continuous" else 0
        # every split_at moment in reach is a boundary, the earliest first, also when the rest would fit in one
        must = [c for c in forced if position + minimum <= c <= last - minimum
                and c <= position + maximum - lead - (0 if c in shot_starts else half)]
        if must:
            spans.append((position, must[0], seam))
            position, seam = must[0], "cut" if must[0] in shot_starts else "continuous"
            continue
        if last - position + lead <= maximum:
            spans.append((position, last, seam))
            break
        options = [c for c in cuts if position + minimum <= c <= position + maximum - lead and last - c >= minimum]
        if options:
            end = options[-1]
            following = "cut" if end in shot_starts else "continuous"
            if following == "continuous" and end > position + maximum - lead - half:
                end = None      # a split_at moment too late for its overlap: search instead
        else:
            end = None
        if end is None:
            hi = min(position + maximum - lead - half, last - minimum)
            lo = max(position + minimum, hi - search)
            end = max(range(lo, hi + 1), key=lambda i: (round(scores[i], 3), i)) if lo <= hi else hi
            following = "continuous"
        spans.append((position, end, seam))
        position, seam = end, following
    segments = [{"frames": [a, b], "seam": kind, "inner_cuts": [c for c in cuts if a < c < b and c in shot_starts]}
                for a, b, kind in spans]
    for number, row in enumerate(segments):
        row["id"] = f"K{number + 1:02d}"
        after = segments[number + 1]["seam"] if number + 1 < len(segments) else None
        a = row["frames"][0] - (half if row["seam"] == "continuous" else 0)
        b = row["frames"][1] + (half if after == "continuous" else 0)
        # at least MIN_RENDER seconds: pad a short clip, later first, earlier where the source ends
        want = min(maximum, max(round(MIN_RENDER * fps), b - a))
        b = min(total, a + want)
        a = max(0, b - want)
        row["render_frames"] = [a, b]
        row["render_seconds"] = round((b - a) / fps, 3)
        row["start"], row["end"] = round(row["frames"][0] / fps, 3), round(row["frames"][1] / fps, 3)
        row["seconds"] = round((b - a) / fps, 3)
        if row["seam"] == "continuous":
            row["seam_score"] = round(scores[row["frames"][0]], 2)
            v = camera[row["frames"][0]] if row["frames"][0] < len(camera) else [0.0, 0.0]
            row["seam_camera"] = [round(float(x), 4) for x in v]
    return segments


MIN_SHARED = 0.5   # seconds a scene or subject must share with a segment to send its picture along


def overlaps(item: dict, start: float, end: float) -> bool:
    """Whether a scene or subject (with start/end seconds) is on screen during start-end, for at least MIN_SHARED
    seconds (or the whole segment when shorter): Gemini's scene and subject times are a few frames off at cuts, and a
    picture sent for a sliver of a segment makes the video model draw that location or subject all through it."""
    shared = min(end, item.get("end", float("inf"))) - max(start, item.get("start", 0))
    return shared >= min(MIN_SHARED, end - start)


def presence(subject: dict, start: float, end: float) -> float:
    """How much of a segment a subject fills: its seconds on screen during start-end times the share of the frame
    its box covers (at its `at` moment)."""
    seconds = min(end, subject.get("end", end)) - max(start, subject.get("start", 0))
    box = subject.get("box") or [0, 0, 1, 1]
    return max(seconds, 0) * (box[2] - box[0]) * (box[3] - box[1])


def assign_refs(row: dict) -> None:
    """Per segment: the scenes it shows (their scene maps go along) and up to MAX_SHEETS subjects on screen during
    it (their sheets go along). A subject without a sheet keeps the game's look, and a continuing segment copies that
    look from the previous result's frame, so the sheets go to the subjects that fill the segment most (`presence`);
    in a segment that continues a shot, subjects the previous result already drew from a sheet (on screen where this
    segment begins, so its first frame carries their look) come last. `sheets` in the edits names a segment's sheets
    instead (when a carried look did not hold: a character redrawn as another)."""
    fps = row["fps"]
    carried: set[int] = set()       # subjects the previous result of this shot drew from a sheet or a carried look
    for seg in row["segments"]:
        start, end = seg["render_frames"][0] / fps, seg["render_frames"][1] / fps
        seg["scenes"] = [k for k, scene in enumerate(row["scenes"], 1) if overlaps(scene, start, end)] or [1]
        if seg.get("seam") != "continuous":
            carried = set()
        here = {k: s for k, s in enumerate(row["subjects"], 1) if overlaps(s, start, end)}
        carried = {k for k in carried if k in here and here[k].get("start", 0) <= start}
        ranked = sorted(here, key=lambda k: (k in carried, -presence(here[k], start, end), k))
        named = [k for k in (row.get("sheets") or {}).get(seg["id"], []) if 1 <= k <= len(row["subjects"])]
        seg["subjects"] = named[:MAX_SHEETS] if named else ranked[:MAX_SHEETS]
        carried |= set(seg["subjects"])


def build(job: Path, edits: dict, catalog: dict, style: dict, max_seconds: float) -> dict:
    plan = {"sources": {}}
    for row in catalog["sources"]:
        survey_path = job / "catalog/survey" / f"{row['id']}.json"
        survey = read(survey_path) if survey_path.is_file() else None
        settings = source_edits(edits, row["id"], row, style, survey)
        if settings["skip"]:
            continue
        motion_path = job / "catalog/motion" / f"{row['id']}.json"
        camera_path = job / "catalog/motion" / f"{row['id']}-camera.json"
        motion = read(motion_path) if motion_path.is_file() else []
        camera = read(camera_path) if camera_path.is_file() else []
        limit = min(MAX_SECONDS, settings["max_segment_seconds"] or max_seconds)
        segments = segment(row, settings["keep"], limit, motion, camera, settings["split_at"])
        plan["sources"][row["id"]] = {**settings, "fps": row["info"]["fps"], "surveyed": survey is not None,
                                      "width": row["info"]["width"], "height": row["info"]["height"],
                                      "segments": segments}
        assign_refs(plan["sources"][row["id"]])
    write(job / "config/plan.json", plan)
    write_text(job / "reports/plan.md", markdown(plan, catalog))
    return plan


def markdown(plan: dict, catalog: dict) -> str:
    names = {row["id"]: row["name"] for row in catalog["sources"]}
    lines = ["# Plan", ""]
    for source_id, row in plan["sources"].items():
        lines.append(f"## {source_id} {names.get(source_id, '')}")
        lines.append(f"keep {row['keep'][0]:.2f}-{row['keep'][1]:.2f} s"
                     + (f" (look from {row['style_from']})" if row["style_from"] else "")
                     + ("" if row["surveyed"] or row.get("scenes") else " (no survey: run survey)"))
        lines.append(f"materials: {describe(row['materials'])}")
        lines.append(f"remove: {describe(row['remove']) or 'nothing'}")
        if row["extra"]:
            lines.append(f"extra: {row['extra']}")
        for k, scene in enumerate(row["scenes"], 1):
            lines.append(f"scene {k} {scene['name']}: {scene.get('start', 0):g}-{scene.get('end', 0):g} s, views at "
                         + ", ".join(f"{t:g}" for t in scene["views"]) + " s")
        for k, subject in enumerate(row["subjects"], 1):
            lines.append(f"subject P{k:02d} {subject['what']} ({subject.get('kind', 'object')}): "
                         f"{subject.get('start', 0):g}-{subject.get('end', 0):g} s, sheet from {subject['at']:g} s")
        for seg in row["segments"]:
            inner = f", contains cuts at {', '.join(f'{c / row['fps']:.2f}' for c in seg['inner_cuts'])} s" if seg["inner_cuts"] else ""
            seam = seg["seam"] + (f" (seam score {seg['seam_score']:.2f})" if "seam_score" in seg else "")
            refs = ", ".join([f"scene {k}" for k in seg["scenes"]] + [f"P{k:02d}" for k in seg["subjects"]])
            lines.append(f"- {seg['id']} {seg['start']:.2f}-{seg['end']:.2f} s (renders {seg['render_seconds']:g} s), "
                         f"seam before: {seam}{inner}; refs: {refs}")
        lines.append("")
    return "\n".join(lines)
