"""Job creation and state: the work order, the sources and the delivery folder."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from reskin.common import PROMPTS, desktop, read, sha, write

STATE = "job.json"
SOURCE_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v"}
RENDER_RESOLUTIONS = ("1080p", "720p")  # what Seedance 2.5 renders at; deliveries are 1080p either way
DEFAULTS = {"style": "realistic", "render_resolution": "1080p", "max_segment_seconds": 30.0, "fps": 24}


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
                         "label, look, default_materials and scene, subject and video prompts)")
    if options["render_resolution"] not in RENDER_RESOLUTIONS:
        raise ValueError(f"options.render_resolution must be one of {', '.join(RENDER_RESOLUTIONS)} (default 1080p)")
    if not 12 <= float(options["fps"]) <= 30:
        raise ValueError("options.fps must be between 12 and 30 (default 24, the rate Seedance renders at)")
    if not 4 <= float(options["max_segment_seconds"]) <= 30:
        raise ValueError("options.max_segment_seconds must be between 4 and 30 (Seedance 2.5 edits clips of up to 30 s)")
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
