"""The check right after a segment renders: Gemini watches the video, the agent looks at stills, and the next segment
of its shot starts only once the agent has passed it.

Segments of one shot form a chain (each starts from the previous result), so a flaw found only in the final review
sends the whole rest of the shot back. Every segment is therefore checked on its own as soon as it is rendered, in two
layers that see different things:
- Gemini gets the video itself, uncut and barely compressed, each panel at the delivery's size, read at the HIGH media
  resolution: the segment beside its original (continuity, bugs, UI, realism over time), and, when the segment before
  it in the video is rendered, both segments beside their originals in a 2 x 2 grid (one look across the two: colour,
  brightness, scenery and subjects). Gemini judges motion and the whole; it reads each frame coarsely.
- The agent gets full-size stills (JPEG 95): every 2 s the original above the segment, and the segment below the
  segment before it at the same offsets. Stills show the detail Gemini misses (garbled letters, soft logos, a
  morphing hand, a colour shift).
The free measures stay (the seam to the segment before it, wiped logo corners). Everything goes on one card,
`review/segments/<id>-<Kxx>/card.md`. The chain waits until the agent passes the segment (`accept`) or sends it back
(`redo`); a segment with a high Gemini finding, a visible measured seam or a smeared corner is `held`: passing it
needs a reason.
"""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reskin import assemble, credentials, ledger, review, winky
from reskin.common import log, read, write, write_text

GEMINI_FPS = 6.0         # frames per second Gemini reads of a segment (each at the HIGH media resolution)
STILL_STEP = 2.0         # seconds between the agent's stills of a segment
PAIR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "consistent": {"type": "BOOLEAN"},
        "summary": {"type": "STRING"},
        "differences": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "kind": {"type": "STRING", "enum": ["colour", "brightness", "scenery", "subject", "realism"]},
            "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
            "where": {"type": "STRING"}, "description": {"type": "STRING"}},
            "required": ["kind", "severity", "start", "end", "description"]}}},
    "required": ["consistent", "summary", "differences"]}


def record_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}.json"


def folder(job: Path, key: str) -> Path:
    return job / "review/segments" / key.replace(":", "-")


def stored(job: Path, key: str) -> dict | None:
    """The check of the segment's current generation, if it was checked."""
    path = record_path(job, key)
    record = read(path) if path.is_file() else {}
    gate = record.get("gate")
    return gate if gate and gate.get("generation_id") == record.get("generation_id") else None


def holds(gate: dict | None) -> bool:
    """The check found something (a high Gemini finding, a visible measured seam, a smeared corner) and the agent has
    not passed it."""
    return bool(gate and gate.get("held") and not gate.get("accepted"))


def pending(gate: dict | None) -> bool:
    """The agent has not passed the check yet (every check waits for the agent's look at its stills)."""
    return bool(gate and not gate.get("accepted"))


def accept(job: Path, key: str, note: str = "") -> dict:
    """The agent looked at the stills and passes the segment; a held one needs the reason its findings are none."""
    path = record_path(job, key)
    if not path.is_file():
        raise ValueError(f"{key} has not been rendered")
    record = read(path)
    gate = record.get("gate")
    if not gate or gate.get("generation_id") != record.get("generation_id"):
        raise ValueError(f"{key} has no segment check of its current render (render it first)")
    if gate.get("held") and not note.strip():
        raise ValueError(f"{key} is held ({'; '.join(gate.get('reasons') or [])}): accept it only with --reason saying "
                         "why the stills show no such flaw")
    gate.update(accepted=True, note=note.strip() or "stills checked: no visible flaw")
    write(path, record)
    return gate


def _done(job: Path, key: str) -> bool:
    path = record_path(job, key)
    return path.is_file() and read(path).get("status") == "done"


def pair_prompt(row: dict, seam: str, layout_note: str, earlier: str, this: str) -> str:
    named = "; ".join(f"{s['id']}: {s['what']}" for s in row.get("subjects") or []) or "the characters and objects shown"
    between = ("a hard camera cut: the shot changes, the look must not" if seam != "continuous" else
               "the same camera shot continues: the later part follows the earlier one in time")
    return "\n".join([
        "This clip shows two consecutive parts of one AI-restyled game advertisement as a 2 x 2 grid: the top row is "
        f"the ORIGINAL game (left: the earlier part {earlier}, right: this part {this}), the bottom row the RESTYLED "
        "video (left: the earlier part, right: this part). Both columns play from their own start on one clock; the "
        f"shorter part holds its last frame. Between the two parts: {between}. {layout_note}",
        "Judge only whether the restyle keeps ONE look across the two parts: colour grading, brightness, contrast, "
        "saturation and white balance; the materials and the level of realism; the scenery (terrain, ground, water, "
        f"snow, trees, buildings) and the recurring characters and objects ({named}) rendered the same way (build, "
        "clothes, colours, material). The content moves on between the parts: a difference counts only where the "
        "ORIGINAL columns do not show the same change (compare the bottom row's difference with the top row's).",
        "List each difference in `differences`: kind, severity (high: a viewer sees at once that the look changed "
        "between the parts; medium: noticeable when watching closely; low: minor), start/end in seconds of this clip "
        "where it shows in the right column, `where` the screen area, and `description`. consistent = false when any "
        "difference is medium or high. Write `summary` and `description` in Chinese.",
    ])


def check(job: Path, state: dict, row: dict, source_id: str, index: int) -> dict:
    """Check segment `index` of `row["segments"]` (rendered); stores and returns the verdict. Runs once per
    generation; the agent's verdict (`accept`) is added later."""
    seg = row["segments"][index]
    key = f"{source_id}:{seg['id']}"
    known = stored(job, key)
    if known:
        return known
    record = read(record_path(job, key))
    out_fps, size = assemble.output_spec(state, row)
    lines = assemble.timeline(row, out_fps)
    line = lines[index]
    clip = assemble.work_clip(job, source_id, line, out_fps, size)
    here = folder(job, key)
    shutil.rmtree(here, ignore_errors=True)      # the stills of an earlier generation must not be mistaken for these
    here.mkdir(parents=True, exist_ok=True)
    source = next(r for r in state["sources"] if r["id"] == source_id)
    fps = row["fps"]
    start, end = seg["render_frames"][0] / fps, seg["render_frames"][1] / fps
    body = (line["body"][0] - line["out"][0]) / out_fps          # where the segment's own frames start in its clip
    length = (line["out"][1] - line["out"][0]) / out_fps
    offset = start - row["keep"][0]                              # its clip's start on the delivered clock
    verdict = {"generation_id": record["generation_id"], "issues": [], "seam": None, "held": False,
               "accepted": False, "previous": None}
    reasons = []
    before = index - 1 if index and not row["segments"][index - 1].get("live") and \
        _done(job, f"{source_id}:{row['segments'][index - 1]['id']}") else None
    prev_clip = prev_line = prev_seg = None
    if before is not None:
        prev_seg, prev_line = row["segments"][before], lines[before]
        prev_clip = assemble.work_clip(job, source_id, prev_line, out_fps, size)
        verdict["previous"] = f"{source_id}:{prev_seg['id']}"
    # Measured (free): the seam to the segment before it inside the shot, and the wiped logo corners.
    if seg["seam"] == "continuous" and prev_clip is not None:
        cut = bool((record.get("inputs") or {}).get("lead"))
        seam = assemble.measure_seam(prev_clip, clip, prev_line, line, out_fps, cut=cut)
        verdict["seam"] = {k: seam.get(k) for k in ("mismatch", "motion", "local", "colour", "jump")}
        verdict["seam"]["inside"] = seam["inside"]
        if seam["jump"] == "visible":
            reasons.append(f"visible seam to {prev_seg['id']} (switch: jump {seam['mismatch']} vs motion "
                           f"{seam['motion']}, local {seam['local']}; where its own frames start: "
                           f"{seam['inside']['jump']}, step {seam['inside']['mismatch']} vs {seam['inside']['motion']})")
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
    # The agent's stills: every STILL_STEP s of the segment's own frames, at full size.
    stills = {"original": [], "previous": []}
    for t in review.every(length - body, STILL_STEP, out_fps):
        moment = body + t
        stills["original"].append(str(review.still_pair(
            (source["path"], start + moment, f"ORIGINAL {key} +{t:.1f} s (video {offset + moment:.2f} s)"),
            (str(clip), moment, f"RESTYLED {key} +{t:.1f} s (video {offset + moment:.2f} s)"),
            size, here / f"original-{t:05.1f}.jpg")))
    if prev_clip is not None:
        prev_body = (prev_line["body"][0] - prev_line["out"][0]) / out_fps
        prev_length = (prev_line["out"][1] - prev_line["out"][0]) / out_fps - prev_body
        for t in review.every(min(length - body, prev_length), STILL_STEP, out_fps):
            stills["previous"].append(str(review.still_pair(
                (str(prev_clip), prev_body + t, f"PREVIOUS {verdict['previous']} +{t:.1f} s"),
                (str(clip), body + t, f"THIS {key} +{t:.1f} s (video {offset + body + t:.2f} s)"),
                size, here / f"previous-{t:05.1f}.jpg")))
    verdict["stills"] = stills
    # Gemini on the video: the segment beside its original, and both segments beside theirs (in parallel).
    try:
        credentials.winky_key()
        has_key = True
    except credentials.MissingCredential:
        has_key = False
        verdict["note"] = "no Winky key: only the measures and the stills"
    if has_key:
        def against_original():
            compare, layout = review.side_by_side(source["path"], start, end, str(clip), size, here / "vs-original.mp4")
            known_notes = review.accepted_notes(job, source_id)
            text = review.segment_prompt(state, row, layout, known=known_notes)
            text += (f"\nThis clip starts at {start:.2f} s of the whole video: second t of the clip is second "
                     f"{start:.2f} + t of the video (the times listed above count in seconds of the whole video).")
            if body > .05:
                text += (f"\nThe first {body:.1f} s of this clip repeat the end of the previous segment (this one "
                         "continues from them); judge them like the rest.")
            answer = winky.gemini([review.video_part(compare, GEMINI_FPS), {"text": text}], schema=review.SCHEMA)
            ledger.add(job, kind="gemini", purpose="gate", key=key, seconds=round(end - start, 2))
            return compare, answer

        def against_previous():
            if prev_clip is None:
                return None, None
            prev_body = (prev_line["body"][0] - prev_line["out"][0]) / out_fps
            prev_start = prev_seg["render_frames"][0] / fps + prev_body
            prev_end = prev_seg["render_frames"][1] / fps
            panels = [{"path": source["path"], "start": prev_start, "end": prev_end, "label": "ORIGINAL earlier"},
                      {"path": source["path"], "start": start + body, "end": end, "label": "ORIGINAL this"},
                      {"path": str(prev_clip), "start": prev_body, "end": None, "label": "RESTYLED earlier"},
                      {"path": str(clip), "start": body, "end": None, "label": "RESTYLED this"}]
            grid = review.grid_clip(panels, size, here / "vs-previous.mp4")
            text = pair_prompt(row, seg["seam"], "", f"{prev_seg['start']:.2f}-{prev_seg['end']:.2f} s",
                               f"{seg['start']:.2f}-{seg['end']:.2f} s")
            answer = winky.gemini([review.video_part(grid, GEMINI_FPS), {"text": text}], schema=PAIR_SCHEMA)
            ledger.add(job, kind="gemini", purpose="gate-pair", key=key,
                       seconds=round(max(length - body, prev_end - prev_start), 2))
            return grid, answer

        with ThreadPoolExecutor(2) as pool:
            jobs = {"original": pool.submit(against_original), "previous": pool.submit(against_previous)}
        for name, future in jobs.items():
            try:
                path, answer = future.result()
            except Exception as error:        # the stills still go to the agent; the card says Gemini failed
                verdict.setdefault("gemini_errors", []).append(f"{name}: {type(error).__name__}: {error}")
                log(f"{key}: Gemini check against the {name} failed ({type(error).__name__}: {error})")
                continue
            if answer is None:
                continue
            if name == "original":
                verdict["compare"] = str(path)
                for number, issue in enumerate(answer.get("issues") or [], 1):
                    a, b = sorted((float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))))
                    verdict["issues"].append({"n": len(verdict["issues"]) + 1, **issue, "start": round(a + offset, 2),
                                              "end": round(b + offset, 2), "segments": [seg["id"]],
                                              "source": "gemini-gate", "local": [round(a, 2), round(b, 2)]})
                local = review.realism_ranges(answer)
                verdict["realism"] = [dict(r, start=round(r["start"] + offset, 2), end=round(r["end"] + offset, 2))
                                      for r in local]
                local_issues = [dict(i, start=i["local"][0], end=i["local"][1]) for i in verdict["issues"]]
                for extra in review.realism_issues(local, local_issues):
                    verdict["issues"].append({"n": len(verdict["issues"]) + 1, **extra,
                                              "start": round(extra["start"] + offset, 2),
                                              "end": round(extra["end"] + offset, 2), "segments": [seg["id"]],
                                              "source": "gemini-realism", "local": [extra["start"], extra["end"]]})
                log(f"{key}: realism {review.realism_line(verdict['realism'])}")
                verdict["summary"] = answer.get("summary", "")
            else:
                verdict["pair_clip"] = str(path)
                verdict["pair_summary"] = answer.get("summary", "")
                for difference in answer.get("differences") or []:
                    a, b = sorted((float(difference.get("start", 0)), float(difference.get("end", 0))))
                    verdict["issues"].append({
                        "n": len(verdict["issues"]) + 1, "severity": difference.get("severity", "medium"),
                        "category": "identity" if difference.get("kind") == "subject" else "style_drift",
                        "where": difference.get("where", ""),
                        "description": f"与前一段 {verdict['previous']} 相比：{difference.get('description', '')}",
                        "start": round(body + a + offset, 2), "end": round(body + b + offset, 2),
                        "segments": [seg["id"]], "source": "gemini-pair",
                        "local": [round(body + a, 2), round(body + b, 2)]})
        serious = [i for i in verdict["issues"] if i["severity"] == "high"]
        if serious:
            reasons.append("Gemini: " + "; ".join(f"[high] {i['category']}: {i['description']}" for i in serious))
    if reasons:
        verdict["held"] = True
        verdict["reasons"] = reasons
        entries = [dict(i, start=i["local"][0], end=i["local"][1]) for i in verdict["issues"]
                   if i["severity"] in ("high", "medium")]
        if verdict["seam"] and verdict["seam"]["jump"] == "visible":
            entries.append({"n": len(entries) + 1, "severity": "high", "category": "seam_color",
                            "start": 0.0, "end": round(body, 2), "source": "measured"})
        for wipe in verdict["wipes"]:
            if wipe["verdict"] == "smeared":
                moment = (wipe["smeared_at"] or [wipe["start"]])[0]
                entries.append({"n": len(entries) + 1, "severity": "high", "category": "artifact",
                                "start": moment, "end": moment, "source": "measured"})
        sheets = review.issue_sheets(source["path"], start, str(clip), end - start, size, entries, here / "issue")
        verdict["sheets"] = [str(p) for p in sheets]
        log(f"{key}: checked, held ({'; '.join(reasons)}); waiting for the agent")
    else:
        log(f"{key}: checked; waiting for the agent's look at its stills")
    verdict["time_offset"] = round(offset, 3)
    verdict["card"] = str(write_card(here, key, seg, row, verdict))
    record = read(record_path(job, key))
    record["gate"] = verdict
    write(record_path(job, key), record)
    return verdict


LOOK_FOR = [
    "Original stills (upper ORIGINAL, lower RESTYLED; portrait: left/right), every one of them: the same objects, "
    "characters, counts, positions, shapes, sizes and colours as the original; real materials (a large flat cartoon "
    "area left is a flaw, the original's shapes kept are not); nothing morphing, melting, doubled, added or missing; "
    "no strong noise, smear or blurred patch; the look holds from the first still to the last.",
    "Screen UI: static text, logos and warning lines exactly as in the original and crisp (any blur, soft or haloed "
    "edge, garbled or changed character is a flaw); animated HUD and text effects may blur or trail a little while "
    "they move, but no garbled characters or wrong digits.",
    "Previous stills (upper PREVIOUS segment, lower THIS one, the same seconds into each): one look across both: the "
    "same colour grade, brightness, saturation and white balance, the same terrain, water, trees and buildings, and "
    "every subject rendered the same way (its reference sheet in keyframes/ is the truth). The content itself moves "
    "on; a change the original also shows is no flaw.",
    "Gemini's findings are a second opinion: confirm each on the stills (or `frames`) before acting on it.",
]


def write_card(here: Path, key: str, seg: dict, row: dict, verdict: dict) -> Path:
    """The check card the agent (or a sub-agent per card) works through."""
    lines = [f"# Segment check {key}", "",
             f"generation {verdict['generation_id']}; {seg['start']:.2f}-{seg['end']:.2f} s of the source (seam "
             f"before: {seg['seam']}); stills labelled with seconds into the segment and on the delivered video",
             f"status: {'HELD: ' + '; '.join(verdict.get('reasons') or []) if verdict.get('held') else 'nothing found by the measures or Gemini'}"
             " - waiting for the agent", ""]
    if seg.get("subjects"):
        owner = row.get("subjects_owner") or key.split(":")[0]
        lines.append("subjects in it (reference sheets): " + ", ".join(
            f"{sid} (keyframes/{owner}-subject-{sid}.png)" for sid in seg["subjects"]))
    lines += ["", "## Measured"]
    if verdict.get("seam"):
        seam = verdict["seam"]
        lines.append(f"- seam to {verdict['previous']}: {seam['jump']} (switch {seam['mismatch']} vs motion "
                     f"{seam['motion']}, local {seam['local']}; own frames start: {seam['inside'].get('jump')})")
    for wipe in verdict.get("wipes") or []:
        lines.append(f"- wiped corner {wipe['what']}: {wipe['verdict']} (detail {wipe['share']} of the scene's)")
    lines += ["", "## Gemini (the video itself)"]
    if verdict.get("note"):
        lines.append(f"- {verdict['note']}")
    for error in verdict.get("gemini_errors") or []:
        lines.append(f"- failed: {error} (judge from the stills alone)")
    if verdict.get("compare"):
        lines.append(f"- against the original ({Path(verdict['compare']).name}): {verdict.get('summary', '')}")
        if verdict.get("realism"):
            from reskin import review as REVIEW
            lines.append(f"  realism (5 = real footage): {REVIEW.realism_line(verdict['realism'])}")
    if verdict.get("pair_clip"):
        lines.append(f"- against {verdict['previous']} ({Path(verdict['pair_clip']).name}): "
                     f"{verdict.get('pair_summary', '')}")
    for issue in verdict["issues"]:
        lines.append(f"  - #{issue['n']} [{issue['severity']}] {issue['category']} {issue['local'][0]:.1f}-"
                     f"{issue['local'][1]:.1f} s into the clip ({issue['source']}): {issue['description']}")
    lines += ["", "## Stills to look at (every one, at full size)", "",
              "against the original (upper original, lower restyled):"]
    lines += [f"- {p}" for p in verdict["stills"]["original"]]
    if verdict["stills"]["previous"]:
        lines += ["", f"against the segment before it, {verdict['previous']} (upper previous, lower this):"]
        lines += [f"- {p}" for p in verdict["stills"]["previous"]]
    if verdict.get("sheets"):
        lines += ["", "around the findings (upper original, lower restyled, T-0.3 / T / T+0.3 s):"]
        lines += [f"- {p}" for p in verdict["sheets"]]
    lines += ["", "## What to look for", *[f"- {text}" for text in LOOK_FOR], "",
              "## Report (a sub-agent writes this back)",
              "`pass` or one line per flaw: `<still file> | <what is wrong, where in the frame> | <high: a viewer sees it "
              "at once / medium / low>`.", "",
              "## Verdict",
              f"- no visible flaw: `accept --segment {key}`" + (" --reason \"<why each finding is none>\"" if verdict.get("held") else ""),
              f"- a confirmed visible flaw: `redo --segment {key} --reason \"<what>\" [--prompt \"<one English sentence>\"]`"]
    path = here / "card.md"
    write_text(path, "\n".join(lines) + "\n")
    return path
