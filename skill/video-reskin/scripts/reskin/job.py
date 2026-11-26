"""Job creation and state: the work order, the sources and the delivery folder."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from reskin.common import ROOT, desktop, read, sha, write

STATE = "job.json"
SOURCE_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v"}
SCHEMA = 2


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
    style = order.get("style")
    styles = read(ROOT / "assets/prompts.json")["styles"]
    if style not in (*styles, "elements"):
        raise ValueError(f"style must be one of {', '.join(styles)}, elements (got {style!r})")
    if order.get("options"):
        if style == "elements":
            raise ValueError("elements uses the source dimensions and timing; work order options are not supported")
        raise ValueError("work order options are not supported: delivery is fixed at 1080p, 24 fps")
    if job_dir:
        job = Path(job_dir).resolve()
        if job.name != "temp":
            raise ValueError("--job-dir for init must be a folder named temp inside the delivery folder")
        delivery = job.parent
    else:
        root = Path(order.get("output_root") or desktop()).expanduser().resolve()
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        label = "视频换元素" if style == "elements" else "视频换风格"
        delivery = root / f"{label}_{stamp}"
        job = delivery / "temp"
    if style == "elements" and order.get("output_root"):
        delivery = Path(order["output_root"]).expanduser().resolve()
    if (job / STATE).is_file():
        raise FileExistsError(f"a job already exists in {job}; continue it with --job-dir, or init a new one")
    job.mkdir(parents=True, exist_ok=True)
    rows = []
    for number, path in enumerate(sources, 1):
        rows.append({"id": f"S{number:02d}", "path": str(path), "name": path.stem, "sha256": sha(path),
                     "size": path.stat().st_size})
    state = {"schema": SCHEMA, "created": datetime.now().isoformat(timespec="seconds"), "delivery_dir": str(delivery),
             "request": order.get("request", ""), "style": style, "sources": rows}
    if style == "elements":
        from reskin.elements import variants_from_order
        from reskin.element_seedance import generation_limit
        state["generation_limit"] = generation_limit(order.get("generation_limit", 0))
        state["surveys"] = order.get("surveys", {})
        state["variants"] = variants_from_order(order, rows, delivery) if "variants" in order else []
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

