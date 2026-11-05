"""The check right after a segment renders, before the next segment of its shot starts from it.

Segments of one shot form a chain (each starts from the previous result), so a flaw found only in the final review
sends the whole rest of the shot back. Here every segment that has a successor is checked on its own as soon as it is
done: the seam to the segment before it and its wiped logo corners are measured (free, the same measures `assemble`
uses) and Gemini compares the segment with its original. A segment is held only for what a viewer sees at once: a
visible seam, a smeared logo corner, or a high Gemini finding (medium ones go to the review). The rest of its chain
waits (other chains go on) until the agent confirms on the stills and either sends it back (`redo`) or lets it
through (`accept`).
"""
from __future__ import annotations

from pathlib import Path

from reskin import assemble, credentials, ledger, review, winky
from reskin.common import log, read, write

GEMINI_FPS = 4.0


def record_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}.json"


def stored(job: Path, key: str) -> dict | None:
    """The gate verdict of the segment's current generation, if it was checked."""
    path = record_path(job, key)
    record = read(path) if path.is_file() else {}
    gate = record.get("gate")
    return gate if gate and gate.get("generation_id") == record.get("generation_id") else None


def holds(gate: dict | None) -> bool:
    return bool(gate and gate.get("held") and not gate.get("accepted"))


def accept(job: Path, key: str, note: str = "") -> dict:
    path = record_path(job, key)
    if not path.is_file():
        raise ValueError(f"{key} has not been rendered")
    record = read(path)
    gate = record.get("gate")
    if not gate or gate.get("generation_id") != record.get("generation_id"):
        raise ValueError(f"{key} has no segment check to accept (it is not held)")
    gate.update(accepted=True, note=note)
    write(path, record)
    return gate


def check(job: Path, state: dict, row: dict, source_id: str, index: int) -> dict:
    """Check segment `index` of `row["segments"]` (rendered, with a successor in its shot); stores and returns the
    verdict. Runs once per generation."""
    seg = row["segments"][index]
    key = f"{source_id}:{seg['id']}"
    known = stored(job, key)
    if known:
        return known
    record = read(record_path(job, key))
    out_fps, size = assemble.output_spec(state, row)
    lines = assemble.timeline(row, out_fps)
    clip = assemble.work_clip(job, source_id, lines[index], out_fps, size)
    verdict = {"generation_id": record["generation_id"], "issues": [], "seam": None, "held": False,
               "accepted": False}
    reasons = []
    if seg["seam"] == "continuous" and index > 0:
        before = assemble.work_clip(job, source_id, lines[index - 1], out_fps, size)
        cut = bool((record.get("inputs") or {}).get("lead"))
        seam = assemble.measure_seam(before, clip, lines[index - 1], lines[index], out_fps, assemble.ignore_boxes(row),
                                     cut=cut)
        verdict["seam"] = {k: seam.get(k) for k in ("mismatch", "motion", "local", "colour", "jump")}
        verdict["seam"]["inside"] = seam["inside"]
        if seam["jump"] == "visible":
            reasons.append(f"visible seam to {row['segments'][index - 1]['id']} (switch: jump {seam['mismatch']} vs "
                           f"motion {seam['motion']}, local {seam['local']}; where its own frames start: "
                           f"{seam['inside']['jump']}, step {seam['inside']['mismatch']} vs {seam['inside']['motion']})")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    start = seg["render_frames"][0] / row["fps"]
    end = seg["render_frames"][1] / row["fps"]
    # Wiped logo corners inside this segment, on its own clock: a blurred patch is held like a visible seam.
    from reskin import logos
    shift = row["keep"][0] - start
    corners = []
    for item in assemble.wipe_items(job, state, source_id, row):
        a, b = max(0.0, item["start"] + shift), min(end - start, item["end"] + shift)
        if b - a >= .5:
            corners.append(dict(item, start=a, end=b))
    verdict["wipes"] = logos.quality(clip, corners, samples=4)
    for wipe in verdict["wipes"]:
        if wipe["verdict"] == "smeared":
            reasons.append(f"blurred patch where {wipe['what']} was wiped (detail {wipe['share']} of the scene's)")
    try:
        credentials.winky_key()
        has_key = True
    except credentials.MissingCredential:
        has_key = False
        verdict["note"] = "no Winky key: only the seam was measured"
    if has_key:
        compare, layout = review.side_by_side(source["path"], start, end, str(clip), size,
                                              job / "review/gate" / f"{source_id}-{seg['id']}-compare.mp4")
        part = winky.inline(compare, "video/mp4")
        part["videoMetadata"] = {"fps": GEMINI_FPS}
        known = review.accepted_notes(job, source_id)
        answer = winky.gemini([part, {"text": review.segment_prompt(state, row, layout, known=known)}],
                              schema=review.SCHEMA)
        ledger.add(job, kind="gemini", purpose="gate", key=key, seconds=round(end - start, 2))
        for number, issue in enumerate(answer.get("issues") or [], 1):
            a, b = sorted((float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))))
            # Times on the delivered clock (seconds from the kept start), like the review's.
            offset = start - row["keep"][0]
            verdict["issues"].append({"n": number, **issue, "start": round(a + offset, 2), "end": round(b + offset, 2),
                                      "segments": [seg["id"]], "source": "gemini-gate", "local": [round(a, 2), round(b, 2)]})
        offset = start - row["keep"][0]
        local = review.realism_ranges(answer)
        verdict["realism"] = [dict(r, start=round(r["start"] + offset, 2), end=round(r["end"] + offset, 2)) for r in local]
        local_issues = [dict(i, start=i["local"][0], end=i["local"][1]) for i in verdict["issues"]]
        for extra in review.realism_issues(local, local_issues):
            verdict["issues"].append({"n": len(verdict["issues"]) + 1, **extra, "start": round(extra["start"] + offset, 2),
                                      "end": round(extra["end"] + offset, 2), "segments": [seg["id"]],
                                      "source": "gemini-realism", "local": [extra["start"], extra["end"]]})
        log(f"{key}: realism {review.realism_line(verdict['realism'])}")
        verdict["summary"] = answer.get("summary", "")
        serious = [i for i in verdict["issues"] if i["severity"] == "high"]
        if serious:
            reasons.append("Gemini: " + "; ".join(f"[high] {i['category']}: {i['description']}" for i in serious))
    if reasons:
        verdict["held"] = True
        verdict["reasons"] = reasons
        offset = start - row["keep"][0]
        entries = [dict(i, start=i["local"][0], end=i["local"][1]) for i in verdict["issues"]]
        if verdict["seam"] and verdict["seam"]["jump"] == "visible":
            entries.append({"n": len(entries) + 1, "severity": "high", "category": "seam_color",
                            "start": 0.0, "end": round((lines[index]["body"][0] - lines[index]["out"][0]) / out_fps, 2),
                            "source": "measured"})
        for wipe in verdict["wipes"]:
            if wipe["verdict"] == "smeared":
                moment = (wipe["smeared_at"] or [wipe["start"]])[0]
                entries.append({"n": len(entries) + 1, "severity": "high", "category": "artifact",
                                "start": moment, "end": moment, "source": "measured"})
        sheets = review.issue_sheets(source["path"], start, str(clip), end - start, size, entries,
                                     job / "review/gate" / f"{source_id}-{seg['id']}")
        verdict["sheets"] = [str(p) for p in sheets]
        verdict["time_offset"] = round(offset, 3)
        log(f"{key}: held by the segment check ({'; '.join(reasons)}); the rest of its shot waits")
    else:
        log(f"{key}: segment check passed")
    record = read(record_path(job, key))
    record["gate"] = verdict
    write(record_path(job, key), record)
    return verdict
