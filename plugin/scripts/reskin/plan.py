"""The agent's edits (kept range, materials, what to remove, anchor frame) and the segment plan built from them.

Seedance edits clips of 4-30 s, so each kept range is cut into segments: at camera cuts where possible (a hard cut
hides the seam), otherwise inside a shot into as few even pieces as fit, each split at the stillest moment near its
even position, with a short overlap: the later piece is rendered from the earlier result's frame there (`render`) and
`assemble` switches inside the overlap at the frame where both match best.
"""
from __future__ import annotations

from pathlib import Path

from reskin.common import read, write, write_text
from reskin.prompts import describe

MIN_SECONDS = 3.0         # Seedance needs at least 2 s; shorter pieces are merged into a neighbour
OVERLAP_SECONDS = 0.5     # both sides of a seam inside a shot render this much more, cross-faded in assemble
EDIT_KEYS = {"keep", "materials", "remove", "anchor", "extra", "skip", "style_from", "split_at", "max_segment_seconds"}
REVEAL_DELAY = .7         # seconds after a timed material's start that its reveal frame is taken (it is fully in view)
REVEAL_GROUP = 2.0        # timed materials starting this close together share one reveal frame
MAX_REVEALS = 2           # reveal frames one segment gets at most


def items_problem(items, name: str) -> str | None:
    """`remove` (and `materials` when not one text) is a list of descriptions, each a string or
    {"what": ..., "start": s, "end": s} for something on screen only part of the time (materials may add "at": the
    moment their reveal frame shows them best)."""
    if not isinstance(items, list):
        return f"{name} must be a list"
    for item in items:
        if isinstance(item, str) and item.strip():
            continue
        if (isinstance(item, dict) and isinstance(item.get("what"), str) and item["what"].strip()
                and set(item) <= {"what", "start", "end", "at"}
                and all(isinstance(item.get(k, 0), (int, float)) for k in ("start", "end", "at"))
                and item.get("start", 0) <= item.get("end", float("inf"))):
            continue
        return f"{name} item {item!r} must be text or {{\"what\": text, \"start\": seconds, \"end\": seconds}}"
    return None


def check_edits(value: dict, state: dict, catalog: dict) -> dict:
    """Validate the edits file against the catalog; returns it normalised."""
    if not isinstance(value, dict) or not isinstance(value.get("sources", {}), dict):
        raise ValueError('edits must look like {"defaults": {...}, "sources": {"S01": {...}}}')
    known = {row["id"]: row for row in catalog["sources"]}
    defaults = value.get("defaults") or {}
    problems = []
    for key in set(defaults) - EDIT_KEYS:
        problems.append(f"defaults: unknown key {key}")
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
            elif keep[1] - keep[0] < MIN_SECONDS:
                problems.append(f"{source_id}: keep must be at least {MIN_SECONDS:g} s")
        if "extra" in row and not isinstance(row["extra"], str):
            problems.append(f"{source_id}: extra must be text")
        if "materials" in row and not isinstance(row["materials"], str) and items_problem(row["materials"], "materials"):
            problems.append(f"{source_id}: {items_problem(row['materials'], 'materials')}")
        if "remove" in row and items_problem(row["remove"], "remove"):
            problems.append(f"{source_id}: {items_problem(row['remove'], 'remove')}")
        if "anchor" in row and not (isinstance(row["anchor"], (int, float)) and 0 <= row["anchor"] < duration):
            problems.append(f"{source_id}: anchor must be a time in seconds inside the source")
        if row.get("style_from") and (row["style_from"] not in known or row["style_from"] == source_id):
            problems.append(f"{source_id}: style_from must name another source")
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
            "style_from": row.get("style_from"), "split_at": row.get("split_at") or [],
            "max_segment_seconds": float(row.get("max_segment_seconds") or 0) or None}


def _stillest(motion: list[float], lo: int, hi: int, fps: float) -> int:
    """The frame in [lo, hi) whose surrounding half second changes least."""
    if hi - lo < 2 or not motion:
        return (lo + hi) // 2
    half = max(1, int(fps / 4))
    best, frame = None, (lo + hi) // 2
    for i in range(lo, hi):
        window = motion[max(0, i - half): i + half + 1]
        score = (round(sum(window) / len(window), 6), abs(i - (lo + hi) // 2))   # ties: nearest the middle
        if best is None or score < best:
            best, frame = score, i
    return frame


def segment(catalog_row: dict, keep: list[float], max_seconds: float, motion: list[float],
            split_at: list[float]) -> list[dict]:
    fps = catalog_row["info"]["fps"]
    first, last = round(keep[0] * fps), min(catalog_row["info"]["frames"], round(keep[1] * fps))
    cuts = sorted({s["frames"][0] for s in catalog_row["shots"]} | {round(t * fps) for t in split_at})
    cuts = [c for c in cuts if first < c < last]
    minimum, maximum = round(MIN_SECONDS * fps), int(max_seconds * fps)
    overlap = round(OVERLAP_SECONDS * fps)
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
            end, following = options[-1], "cut"
        else:
            # As few pieces as fit up to the next cut out of reach (every seam inside a shot can show), split
            # evenly, each split at the stillest moment near its even position.
            stretch = min([c for c in cuts if c > position + limit] + [last]) - position
            lead = overlap if seam == "continuous" else 0
            pieces = 2
            while stretch + lead + (pieces - 1) * overlap > pieces * maximum:
                pieces += 1
            target = position + stretch // pieces
            # what is left after this piece must still fit into pieces - 1 pieces
            rest = (pieces - 1) * (maximum - overlap)
            lo = max(position + minimum, position + stretch - rest, target - window)
            hi = min(position + limit, position + stretch - minimum, target + window)
            end, following = (_stillest(motion, lo, hi, fps) if lo < hi else min(hi, max(lo, target))), "continuous"
        spans.append((position, end, seam))
        position, seam = end, following
    segments = [{"frames": [a, b], "seam": kind, "inner_cuts": [c for c in cuts if a < c < b]}
                for a, b, kind in spans]
    for number, row in enumerate(segments, 1):
        row["id"] = f"K{number:02d}"
        lead = overlap if row["seam"] == "continuous" else 0
        row["render_frames"] = [max(first, row["frames"][0] - lead), row["frames"][1]]
        row["start"], row["end"] = round(row["frames"][0] / fps, 3), round(row["frames"][1] / fps, 3)
        row["seconds"] = round((row["render_frames"][1] - row["render_frames"][0]) / fps, 3)
    return segments


def mark_reveals(row: dict) -> None:
    """Per segment after a source's first: the timed materials that come into view for the first time during it
    (`reveals`). Nothing earlier shows them, so GPT Image restyles one source frame of them after the anchor, and
    the segment gets that frame as a reference besides the earlier results (render)."""
    items = [i for i in (row["materials"] if isinstance(row["materials"], list) else [])
             if isinstance(i, dict) and "start" in i]
    fps = row["fps"]
    for number, seg in enumerate(row["segments"]):
        seg["reveals"] = []
        if not number:
            continue
        start, end = seg["frames"][0] / fps, seg["frames"][1] / fps
        new = sorted((min(max(float(i.get("at", i["start"] + REVEAL_DELAY)), start), end - .1), i["what"])
                     for i in items if start <= i["start"] < end)
        groups: list[list] = []
        for moment, what in new:
            if groups and moment - groups[-1][0][0] <= REVEAL_GROUP:
                groups[-1].append((moment, what))
            else:
                groups.append([(moment, what)])
        while len(groups) > MAX_REVEALS:      # merge the two closest groups
            gaps = [groups[k + 1][0][0] - groups[k][-1][0] for k in range(len(groups) - 1)]
            k = gaps.index(min(gaps))
            groups[k:k + 2] = [groups[k] + groups[k + 1]]
        for k, group in enumerate(groups, 1):
            moment = max(m for m, _ in group)       # every item of the group in view
            seg["reveals"].append({"id": f"{seg['id']}-N{k}", "frame": round(moment * fps), "time": round(moment, 3),
                                   "what": list(dict.fromkeys(w for _, w in group))})


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
        mark_reveals(plan["sources"][row["id"]])
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
        for seg in row["segments"]:
            inner = f", contains cuts at {', '.join(f'{c / row['fps']:.2f}' for c in seg['inner_cuts'])} s" if seg["inner_cuts"] else ""
            reveals = "".join(f"; reveal {r['id']} at {r['time']:.2f} s: {', '.join(r['what'])}"
                              for r in seg.get("reveals") or [])
            lines.append(f"- {seg['id']} {seg['start']:.2f}-{seg['end']:.2f} s (renders {seg['seconds']:.2f} s), "
                         f"seam before: {seg['seam']}{inner}{reveals}")
        lines.append("")
    return "\n".join(lines)
