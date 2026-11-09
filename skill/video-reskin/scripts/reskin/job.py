"""Job creation and state: the work order, the sources and the delivery folder."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reskin.common import PROMPTS, desktop, read, sha, write

STATE = "job.json"
SOURCE_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v"}
DELIVERY_NAME = re.compile(r"视频换风格_\d{8}_\d{6}")
RESOLUTION = "1080p"       # deliveries: one size for every job (the user's choice, 2026-11-05)
RENDER_RESOLUTIONS = ("2K", "768P")     # what MiniMax H3 renders at (2K by default, scaled to 1080p)
# Segments as long as MiniMax H3 takes (15 s); seams inside a shot go where the camera moves fast (plan).
DEFAULTS = {"style": "realistic", "resolution": RESOLUTION, "render_resolution": "2K", "max_segment_seconds": 15.0,
            "fps": 24, "remove_end_card": True}


def presets() -> dict:
    return read(PROMPTS)["presets"]


def expand_sources(entries: list) -> list[Path]:
    """Files as given; folders contribute their videos (not recursive), sorted by name."""
    paths = []
    for entry in entries:
        path = Path(entry.get("path", "") if isinstance(entry, dict) else str(entry)).expanduser().resolve()
        if path.is_dir():
            paths += sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES)
        elif path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
            paths.append(path)
        else:
            raise ValueError(f"not a source video or folder (.mp4/.mov/.mkv/.m4v): {path}")
    unique = list(dict.fromkeys(paths))
    if not unique:
        raise ValueError("sources must list video files or folders that contain videos")
    return unique


def init(order: dict, job_dir: Path | None = None) -> tuple[Path, dict]:
    sources = expand_sources(order.get("sources") or [])
    options = {**DEFAULTS, **{k: v for k, v in (order.get("options") or {}).items() if k in DEFAULTS}}
    unknown = sorted(set(order.get("options") or {}) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"unknown options {unknown}; known: {sorted(DEFAULTS)}")
    style = order.get("style") or options["style"]
    if isinstance(style, str) and style not in presets():
        raise ValueError(f"unknown style preset {style}; known: {sorted(presets())} (or give style as an object with "
                         "label, goal, scene, subject and video prompts)")
    if options["resolution"] != RESOLUTION:
        raise ValueError(f"options.resolution is fixed at {RESOLUTION} (leave it out)")
    if options["render_resolution"] not in RENDER_RESOLUTIONS:
        raise ValueError(f"options.render_resolution must be one of {', '.join(RENDER_RESOLUTIONS)} (default 2K)")
    if not 12 <= float(options["fps"]) <= 30:
        raise ValueError("options.fps must be between 12 and 30 (default 24, the rate H3 renders at)")
    if not 4 <= float(options["max_segment_seconds"]) <= 15:
        raise ValueError("options.max_segment_seconds must be between 4 and 15 (MiniMax H3 edits 4-15 s clips)")
    if job_dir:
        job = Path(job_dir).resolve()
        delivery = job.parent
    else:
        root = Path(order.get("output_root") or desktop()).expanduser().resolve()
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        delivery = root / f"视频换风格_{stamp}"
        job = delivery / "temp"
    if (job / STATE).is_file():
        raise FileExistsError(f"a job already exists in {job}; continue it with --job-dir, or init a new one")
    job.mkdir(parents=True, exist_ok=True)
    rows = []
    for number, path in enumerate(sources, 1):
        rows.append({"id": f"S{number:02d}", "path": str(path), "name": path.stem, "sha256": sha(path),
                     "size": path.stat().st_size})
    state = {"schema": 1, "created": datetime.now().isoformat(timespec="seconds"), "delivery_dir": str(delivery),
             "request": order.get("request", ""), "style": style, "options": options, "sources": rows}
    write(job / STATE, state)
    return job, state


def load(job: Path) -> dict:
    path = Path(job) / STATE
    if not path.is_file():
        raise FileNotFoundError(f"no job in {job}: run init first (or pass the job's temp folder as --job-dir)")
    state = read(path)
    for row in state["sources"]:
        source = Path(row["path"])
        if not source.is_file() or source.stat().st_size != row["size"]:
            raise FileNotFoundError(f"source {row['id']} moved or changed: {source}; init a new job")
    return state


def style(state: dict) -> dict:
    value = state["style"]
    return presets()[value] if isinstance(value, str) else value


def source(state: dict, source_id: str) -> dict:
    for row in state["sources"]:
        if row["id"] == source_id:
            return row
    raise ValueError(f"unknown source {source_id}; known: {', '.join(r['id'] for r in state['sources'])}")
