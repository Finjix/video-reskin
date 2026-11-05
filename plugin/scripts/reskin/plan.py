"""The agent's edits (kept range, materials, what to remove, anchor frame) and the segment plan built from them.

Seedance edits clips of 4-30 s, so each kept range is cut into as few segments as possible: at camera cuts where
possible (a hard cut hides the seam), otherwise inside a shot at its stillest moment, with a 1 s overlap: the later
segment's clip starts with the earlier result's last second, and `assemble` switches at the best-matching frame in it.
"""
from __future__ import annotations

from pathlib import Path

from reskin.common import read, write, write_text
from reskin.overlay import problem as overlay_problem
from reskin.prompts import describe

MIN_SECONDS = 4.0         # Seedance video editing rejects clips under 4 s; shorter pieces are merged into a neighbour
OVERLAP_SECONDS = 1.0     # a segment after a seam inside a shot renders this much more (the earlier result's last
#                           frames); assemble joins inside it
HEAD_SECONDS = 0.5        # ... and its clip starts with this much of the shot's opening (cut off again in assembly)
EDIT_KEYS = {"keep", "materials", "remove", "anchor", "extra", "skip", "style_from", "split_at", "max_segment_seconds",
             "hud", "overlays", "style_ref", "chain_prefix"}
REMOVED_KEYS = {"midframes": "midframes were removed (extra preview references cost previews and carried their "
                             "colour mistakes into the video); leave the key out"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def box_problem(box) -> bool:
    return (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box)
            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1))


def items_problem(items, name: str) -> str | None:
    """`remove` (and `materials` when not one text) is a list of English descriptions (they go into the prompts),
    each a string or
    {"what": ..., "start": s, "end": s} for something on screen only part of the time; a `remove` item may add
    "box": [x0, y0, x1, y1] (fractions) for a screen-fixed logo or warning line: named in the prompt for the model
    to remove (`"wipe": "before"`: wiped letter by letter from the clip Seedance gets instead)."""
    if not isinstance(items, list):
        return f"{name} must be a list"
    for item in items:
        if isinstance(item, str) and item.strip():
            continue
        if (isinstance(item, dict) and isinstance(item.get("what"), str) and item["what"].strip()
                and set(item) <= {"what", "start", "end", *(("box", "wipe") if name == "remove" else ())}
                and ("box" not in item or not box_problem(item["box"]))
                and item.get("wipe", "none") in ("before", "none")
                and all(isinstance(item.get(k, 0), (int, float)) for k in ("start", "end"))
                and item.get("start", 0) <= item.get("end", float("inf"))):
            continue
        return (f"{name} item {item!r} must be text or {{\"what\": text, \"start\": seconds, \"end\": seconds"
                + (", \"box\": [x0, y0, x1, y1], \"wipe\": \"before\" or \"none\"" if name == "remove" else "")
                + "}")
    return None


def hud_problem(items) -> str | None:
    """`hud`: screen-fixed UI (counters, timers) copied back from the original: [{"box": [x0, y0, x1, y1] as
    fractions of the frame, "start": s, "end": s, "what": text}]."""
    if not isinstance(items, list):
        return "hud must be a list"
    for item in items:
        box = item.get("box") if isinstance(item, dict) else None
        if (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box)
                or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)
                or (box[2] - box[0]) * (box[3] - box[1]) > .15
                or not set(item) <= {"box", "start", "end", "what"}
                or not all(isinstance(item.get(k, 0), (int, float)) for k in ("start", "end"))):
            return (f"hud item {item!r} must be {{\"box\": [x0, y0, x1, y1] (fractions, at most 15% of the frame), "
                    "\"start\": s, \"end\": s, \"what\": text}")
    return None


def style_ref_problem(value) -> str | None:
    """`style_ref`: a texture reference for the anchor keyframe only: an image path, or {"path": image or video,
    "at": seconds (videos), "box": [x0, y0, x1, y1] (fractions; crop away its logos and text)}."""
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not set(value) <= {"path", "at", "box"}:
        return 'style_ref must be a path or {"path": file, "at": seconds, "box": [x0, y0, x1, y1]}'
    path = Path(value["path"])
    if not path.is_file():
        return f"style_ref file not found: {path}"
    suffix = path.suffix.lower()
    if suffix not in VIDEO_SUFFIXES | IMAGE_SUFFIXES:
        return f"style_ref must be an image or a video, not {suffix or 'a file without extension'}"
    if suffix in VIDEO_SUFFIXES and not (isinstance(value.get("at"), (int, float)) and value["at"] >= 0):
        return "style_ref of a video needs \"at\": the time in seconds of the frame to use"
    box = value.get("box")
    if box is not None and (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box)
                            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
        return "style_ref box must be [x0, y0, x1, y1] fractions of the frame"
    return None


def check_edits(value: dict, state: dict, catalog: dict) -> dict:
    """Validate the edits file against the catalog; returns it normalised."""
    if not isinstance(value, dict) or not isinstance(value.get("sources", {}), dict):
        raise ValueError('edits must look like {"defaults": {...}, "sources": {"S01": {...}}}')
    known = {row["id"]: row for row in catalog["sources"]}
    defaults = value.get("defaults") or {}
    problems = []
    for key in set(defaults) - EDIT_KEYS:
        problems.append(f"defaults: {REMOVED_KEYS.get(key, f'unknown key {key}')}")
    if "remove" in defaults and items_problem(defaults["remove"], "remove"):
        problems.append(f"defaults: {items_problem(defaults['remove'], 'remove')}")
    if defaults.get("style_ref") is not None and style_ref_problem(defaults["style_ref"]):
        problems.append(f"defaults: {style_ref_problem(defaults['style_ref'])}")
    out = {"defaults": defaults, "sources": {}}
    for source_id, row in (value.get("sources") or {}).items():
        if source_id not in known:
            problems.append(f"{source_id}: not a source of this job")
            continue
        for key in set(row) - EDIT_KEYS:
            problems.append(f"{source_id}: {REMOVED_KEYS.get(key, f'unknown key {key}')}")
        duration = known[source_id]["duration"]
        keep = row.get("keep")
        if keep is not None:
            if (not isinstance(keep, list) or len(keep) != 2 or not all(isinstance(x, (int, float)) for x in keep)
                    or not 0 <= keep[0] < keep[1] <= duration + .05):
                problems.append(f"{source_id}: keep must be [start, end] seconds inside 0-{duration:.2f}")
            elif keep[1] - keep[0] < MIN_SECONDS:
                problems.append(f"{source_id}: keep must be at least {MIN_SECONDS:g} s")
        if "extra" in row and not isinstance(row["extra"], str):
            problems.append(f"{source_id}: extra must be text")
        if "materials" in row and not isinstance(row["materials"], str) and items_problem(row["materials"], "materials"):
            problems.append(f"{source_id}: {items_problem(row['materials'], 'materials')}")
        if "remove" in row and items_problem(row["remove"], "remove"):
            problems.append(f"{source_id}: {items_problem(row['remove'], 'remove')}")
        if "hud" in row and hud_problem(row["hud"]):
            problems.append(f"{source_id}: {hud_problem(row['hud'])}")
        if "overlays" in row and overlay_problem(row["overlays"]):
            problems.append(f"{source_id}: {overlay_problem(row['overlays'])}")
        if "anchor" in row and not (isinstance(row["anchor"], (int, float)) and 0 <= row["anchor"] < duration):
            problems.append(f"{source_id}: anchor must be a time in seconds inside the source")
        if row.get("style_from") and (row["style_from"] not in known or row["style_from"] == source_id):
            problems.append(f"{source_id}: style_from must name another source")
        if row.get("style_ref") is not None and style_ref_problem(row["style_ref"]):
            problems.append(f"{source_id}: {style_ref_problem(row['style_ref'])}")
        if "chain_prefix" in row and not isinstance(row["chain_prefix"], bool):
            problems.append(f"{source_id}: chain_prefix must be true or false")
        if "split_at" in row and not (isinstance(row["split_at"], list) and all(isinstance(x, (int, float)) for x in row["split_at"])):
            problems.append(f"{source_id}: split_at must be a list of times in seconds")
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


def source_edits(edits: dict, source_id: str, catalog_row: dict, style: dict) -> dict:
    """Edits for one source with defaults filled in."""
    defaults = edits.get("defaults") or {}
    row = {**{k: v for k, v in defaults.items() if k not in ("keep", "anchor", "split_at")},
           **(edits.get("sources") or {}).get(source_id, {})}
    card = catalog_row.get("end_card")
    keep = row.get("keep") or [0.0, card["start"] if card else catalog_row["duration"]]
    remove = []
    for item in [*(defaults.get("remove") or []), *((edits.get("sources") or {}).get(source_id, {}).get("remove") or [])]:
        if item not in remove:
            remove.append(item)
    return {"keep": [float(keep[0]), float(keep[1])], "materials": row.get("materials") or style["default_materials"],
            "remove": remove, "anchor": row.get("anchor"), "extra": row.get("extra", ""), "skip": bool(row.get("skip")),
            "style_from": row.get("style_from"), "split_at": row.get("split_at") or [], "hud": row.get("hud") or [],
            "overlays": row.get("overlays") or [],
            "style_ref": ({"path": row["style_ref"]} if isinstance(row.get("style_ref"), str) else row.get("style_ref")),
            "chain_prefix": row.get("chain_prefix"),
            "max_segment_seconds": float(row.get("max_segment_seconds") or 0) or None}


def _stillest(motion: list[float], lo: int, hi: int, fps: float) -> int:
    """The frame in [lo, hi) whose surrounding half second changes least."""
    if hi - lo < 2 or not motion:
        return (lo + hi) // 2
    half = max(1, int(fps / 4))
    centre = (lo + hi) // 2
    best, frame = None, centre
    for i in range(lo, hi):
        window = motion[max(0, i - half): i + half + 1]
        # equal stillness: prefer the middle of the range, which keeps split pieces even
        score = sum(window) / len(window) + .05 * abs(i - centre) / fps
        if best is None or score < best:
            best, frame = score, i
    return frame


def segment(catalog_row: dict, keep: list[float], max_seconds: float, motion: list[float],
            split_at: list[float]) -> list[dict]:
    fps = catalog_row["info"]["fps"]
    first, last = round(keep[0] * fps), min(catalog_row["info"]["frames"], round(keep[1] * fps))
    # Camera cuts hide a seam; `split_at` points are wanted seam moments inside a shot (to keep a seam off an
    # effect burst), so they join chained like any seam inside a shot, not like a cut.
    camera = {c for c in (s["frames"][0] for s in catalog_row["shots"]) if first < c < last}
    cuts = sorted(camera | {c for c in (round(t * fps) for t in split_at) if first < c < last})
    minimum, maximum = round(MIN_SECONDS * fps), int(max_seconds * fps)
    overlap = round(OVERLAP_SECONDS * fps) + round(HEAD_SECONDS * fps)   # what a chained clip adds within the limit
    lead = round(OVERLAP_SECONDS * fps)
    window = int(2 * fps)
    # Walk forward: end each segment at the latest camera cut that fits, else at the stillest moment near the
    # limit inside the shot (that segment then also renders the overlap before its start).
    spans, position, seam = [], first, "start"
    while True:
        limit = maximum - (overlap if seam == "continuous" else 0)
        remaining = last - position
        if remaining <= limit:
            spans.append((position, last, seam))
            break
        options = [c for c in cuts if position + minimum <= c <= position + limit and last - c >= minimum]
        if options:
            end = options[-1]
            following = "cut" if end in camera else "continuous"
        else:
            # Split this shot (up to its next cut) into equal pieces, each at a still moment within the limits;
            # fewer, longer pieces mean fewer seams.
            stretch = next((c for c in cuts if c > position + limit), last) - position
            body = maximum - overlap
            pieces = 1 + -(-(stretch - limit) // body)
            target = position + stretch // pieces
            lo = max(position + minimum, position + stretch - (pieces - 1) * body, target - window)
            hi = min(position + limit, last - minimum, target + window)
            end, following = (_stillest(motion, lo, hi, fps) if lo < hi else max(lo, min(hi, target))), "continuous"
        spans.append((position, end, seam))
        position, seam = end, following
    segments = [{"frames": [a, b], "seam": kind, "inner_cuts": [c for c in sorted(camera) if a < c < b]}
                for a, b, kind in spans]
    for number, row in enumerate(segments, 1):
        row["id"] = f"K{number:02d}"
        chained = row["seam"] == "continuous"
        row["render_frames"] = [max(first, row["frames"][0] - (lead if chained else 0)), row["frames"][1]]
        row["start"], row["end"] = round(row["frames"][0] / fps, 3), round(row["frames"][1] / fps, 3)
        # what Seedance gets (and is paid for): the chained clip's opening included
        row["seconds"] = round((row["render_frames"][1] - row["render_frames"][0]
                                + (round(HEAD_SECONDS * fps) if chained else 0)) / fps, 3)
    return segments


def auto_anchor(catalog_row: dict, keep: list[float], motion: list[float]) -> float:
    """Per the reference case: a frame from a lively stretch (much movement means much of the scene is shown),
    inside the longest kept shot, which sets the look of most of the video."""
    fps = catalog_row["info"]["fps"]
    spans = [(max(s["start"], keep[0]), min(s["end"], keep[1])) for s in catalog_row["shots"]]
    start, end = max((span for span in spans if span[1] > span[0]), key=lambda span: span[1] - span[0], default=keep)
    lo, hi = round((start + 1) * fps), round((end - 1) * fps)
    if not motion or hi - lo < fps:
        return round((keep[0] + keep[1]) / 2, 2)
    window = int(fps)
    best, frame = -1.0, lo
    for i in range(lo, hi, max(1, int(fps / 4))):
        stretch = motion[max(0, i - window): i + window]
        score = sum(stretch) / max(1, len(stretch))
        # the frame itself should be steady, so it is not a blurred transition
        if score > best and motion[i] < 3 * (score + 1):
            best, frame = score, i
    return round(frame / fps, 2)


def build(job: Path, state: dict, edits: dict, catalog: dict, style: dict, max_seconds: float) -> dict:
    plan = {"sources": {}}
    for row in catalog["sources"]:
        settings = source_edits(edits, row["id"], row, style)
        if settings["skip"]:
            continue
        motion_path = job / "catalog/motion" / f"{row['id']}.json"
        motion = read(motion_path) if motion_path.is_file() else []
        limit = min(30.0, settings["max_segment_seconds"] or max_seconds)
        segments = segment(row, settings["keep"], limit, motion, settings["split_at"])
        anchor = settings["anchor"] if settings["anchor"] is not None else auto_anchor(row, settings["keep"], motion)
        plan["sources"][row["id"]] = {**settings, "anchor": anchor, "fps": row["info"]["fps"],
                                      "width": row["info"]["width"], "height": row["info"]["height"],
                                      "segments": segments}
    write(job / "config/plan.json", plan)
    write_text(job / "reports/plan.md", markdown(plan, catalog))
    return plan


def markdown(plan: dict, catalog: dict) -> str:
    names = {row["id"]: row["name"] for row in catalog["sources"]}
    lines = ["# Plan", ""]
    for source_id, row in plan["sources"].items():
        lines.append(f"## {source_id} {names.get(source_id, '')}")
        lines.append(f"keep {row['keep'][0]:.2f}-{row['keep'][1]:.2f} s, anchor frame {row['anchor']:.2f} s"
                     + (f" (style from {row['style_from']})" if row["style_from"] else ""))
        lines.append(f"materials: {describe(row['materials'])}")
        lines.append(f"remove: {describe(row['remove']) or 'nothing'}")
        if row["extra"]:
            lines.append(f"extra: {row['extra']}")
        if row.get("style_ref"):
            ref = row["style_ref"]
            lines.append(f"style reference (anchor only): {ref['path']}" + (f" at {ref['at']:g} s" if "at" in ref else "")
                         + (f", box {ref['box']}" if ref.get("box") else ""))
        for seg in row["segments"]:
            inner = f", contains cuts at {', '.join(f'{c / row['fps']:.2f}' for c in seg['inner_cuts'])} s" if seg["inner_cuts"] else ""
            lines.append(f"- {seg['id']} {seg['start']:.2f}-{seg['end']:.2f} s (renders {seg['seconds']:.2f} s), "
                         f"seam before: {seg['seam']}{inner}")
        lines.append("")
    return "\n".join(lines)
