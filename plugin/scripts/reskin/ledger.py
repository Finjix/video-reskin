"""Spend ledger: one line per paid call (Seedance segment, GPT Image keyframe or preview, Gemini check) in
`state/ledger.jsonl`, with why it ran, so `cost` can show where the money went (first renders, redos, re-renders
of a chain after its upstream changed, automatic retries, plan or keyframe changes)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
# Why a Seedance segment was sent: what changed since its last finished render.
CAUSES = ("new", "redo", "auto-retry", "upstream", "keyframe", "wipe", "plan", "resolution", "model", "unusable",
          "resume")


def path(job: Path) -> Path:
    return Path(job) / "state/ledger.jsonl"


def add(job: Path, **entry) -> None:
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    with _LOCK:
        target = path(job)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def entries(job: Path) -> list[dict]:
    target = path(job)
    if not target.is_file():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def summary(job: Path) -> dict:
    """Seedance seconds by cause, resolution and source; image and Gemini call counts."""
    rows = entries(job)
    video = [r for r in rows if r.get("kind") == "video" and r.get("event") == "submit"]
    outcomes = {r["generation_id"]: r["event"] for r in rows if r.get("kind") == "video" and r.get("event") != "submit"
                and r.get("generation_id")}
    by_cause: dict[str, float] = {}
    by_resolution: dict[str, float] = {}
    by_source: dict[str, float] = {}
    for row in video:
        seconds = float(row.get("seconds") or 0)
        by_cause[row.get("cause", "?")] = by_cause.get(row.get("cause", "?"), 0) + seconds
        by_resolution[row.get("resolution", "?")] = by_resolution.get(row.get("resolution", "?"), 0) + seconds
        source = str(row.get("key", "?")).split(":")[0]
        by_source[source] = by_source.get(source, 0) + seconds
    failed = sum(float(r.get("seconds") or 0) for r in video if outcomes.get(r.get("generation_id")) in ("failed", "rejected"))
    images = [r for r in rows if r.get("kind") == "image" and r.get("event") == "submit"]
    gemini = [r for r in rows if r.get("kind") == "gemini"]
    return {"video_segments": len(video), "video_seconds": round(sum(by_cause.values()), 1),
            "by_cause": {k: round(v, 1) for k, v in sorted(by_cause.items(), key=lambda kv: -kv[1])},
            "by_resolution": {k: round(v, 1) for k, v in by_resolution.items()},
            "by_source": {k: round(v, 1) for k, v in sorted(by_source.items())},
            "failed_or_unusable_seconds": round(failed, 1),
            "images": {p: sum(1 for r in images if r.get("purpose") == p) for p in sorted({r.get("purpose") for r in images})},
            "gemini": {p: sum(1 for r in gemini if r.get("purpose") == p) for p in sorted({r.get("purpose") for r in gemini})},
            "gemini_video_seconds": round(sum(float(r.get("seconds") or 0) for r in gemini), 1)}


def markdown(value: dict) -> list[str]:
    lines = [f"Seedance: {value['video_segments']} submissions, {value['video_seconds']} s of video"
             + (f" ({value['failed_or_unusable_seconds']} s failed or unusable)" if value["failed_or_unusable_seconds"] else "")]
    if value["by_cause"]:
        lines.append("  by cause: " + ", ".join(f"{k} {v} s" for k, v in value["by_cause"].items()))
        lines.append("  by resolution: " + ", ".join(f"{k} {v} s" for k, v in value["by_resolution"].items()))
        lines.append("  by source: " + ", ".join(f"{k} {v} s" for k, v in value["by_source"].items()))
    lines.append("GPT Image: " + (", ".join(f"{k} {v}" for k, v in value["images"].items()) or "none"))
    lines.append("Gemini: " + (", ".join(f"{k} {v}" for k, v in value["gemini"].items()) or "none")
                 + (f" ({value['gemini_video_seconds']} s of video)" if value["gemini_video_seconds"] else ""))
    return lines
