"""Quality review: Gemini watches the original and the restyled video side by side and lists what went wrong;
`frames` cuts stills around a reported time so the agent can confirm an issue before sending a segment back."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import winky
from reskin.analyze import font
from reskin.common import FFMPEG, log, read, run, video_info, write, write_text
from reskin.prompts import describe

CATEGORIES = {
    "continuity": "穿帮: the restyled side shows something the original does not (cartoon material left over, an object or "
                  "character changing identity, shape, colour or count, things appearing or vanishing, motion that no "
                  "longer matches)",
    "artifact": "bug: morphing, melting, warping, flicker, jitter, frozen or repeated frames, garbled UI text or numbers, "
                "broken limbs, smeared detail, strong noise or a fragmented look",
    "added_text": "text the original does not have: captions, warning lines, watermarks or garbled characters drawn "
                  "anywhere (look along the bottom and top edges in particular)",
    "seam_color": "拼接色差: a sudden jump in colour, brightness, contrast, texture or realism between consecutive moments, "
                  "especially at the listed seam times",
    "leftover_overlay": "a logo, watermark or warning/disclaimer text that should have been removed is still visible",
    "end_card": "closing-card content (big logo, store badges, call to action) is visible",
    "style_drift": "the realism level or overall look changes from one part of the video to another",
}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["pass", "fail"]},
        "summary": {"type": "STRING"},
        "issues": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
            "category": {"type": "STRING", "enum": sorted(CATEGORIES)},
            "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
            "where": {"type": "STRING"}, "description": {"type": "STRING"}},
            "required": ["start", "end", "category", "severity", "description"]}}},
    "required": ["verdict", "summary", "issues"]}
LABEL_FONT = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/arial.ttf"


def _drawtext(text: str) -> str:
    if not LABEL_FONT.is_file():
        return ""
    path = str(LABEL_FONT).replace("\\", "/").replace(":", "\\:")
    return (f",drawtext=fontfile='{path}':text='{text}':x=12:y=12:fontsize=28:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=6")


def proxy(job: Path, state: dict, source_id: str, assembled: dict) -> tuple[Path, str]:
    """Original (left or top) and restyled (right or bottom) on one small clip, the same clock for both."""
    source = next(r for r in state["sources"] if r["id"] == source_id)
    width, height = assembled["size"]
    portrait = height >= width
    side = (480, round(480 * height / width / 2) * 2) if portrait else (854, round(854 * height / width / 2) * 2)
    start, end = assembled["keep"]
    output = job / "review" / f"{source_id}-compare.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    stack = "hstack" if portrait else "vstack"
    graph = (f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,fps=15,scale={side[0]}:{side[1]}:flags=area"
             f"{_drawtext('ORIGINAL')}[a];"
             f"[1:v]setpts=PTS-STARTPTS,fps=15,scale={side[0]}:{side[1]}:flags=area{_drawtext('RESTYLED')}[b];"
             f"[a][b]{stack}=inputs=2:shortest=1,format=yuv420p[v]")
    run([FFMPEG, "-v", "error", "-y", "-i", source["path"], "-i", assembled["output"], "-filter_complex", graph,
         "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", output])
    return output, ("left" if portrait else "top")


def prompt(state: dict, plan_row: dict, assembled: dict, layout: str) -> str:
    other = "right" if layout == "left" else "bottom"
    seams = ", ".join(f"{s['time']:.2f} s ({'hard cut' if s['seam'] == 'cut' else 'inside a shot'})"
                      for s in assembled["seams"]) or "none"
    removal = describe(plan_row["remove"]) or "nothing named"
    hud = "; ".join(item.get("what") or "HUD box" for item in plan_row.get("hud") or [])
    style = state["style"] if isinstance(state["style"], str) else state["style"].get("label")
    lines = [
        "You are a strict quality reviewer for AI-restyled game advertising videos.",
        f"The video shows the ORIGINAL on the {layout} and the RESTYLED version on the {other}, frame-synchronised "
        f"(same timestamps). The restyle goal: {describe(plan_row['materials'])} — game-rendered cartoon materials replaced by "
        f"realistic real-world materials (style preset: {style}) while composition, camera motion, characters, object "
        "positions, counts and timing stay exactly as in the original. HUD numbers, icons and buttons must stay.",
        f"Removed on purpose (must NOT appear on the restyled side): {removal}. The closing card was cut off on purpose.",
        f"The restyled video was rendered in segments and joined at: {seams}. Check these moments closely: inside a "
        "shot the restyled scene must continue unchanged (same terrain, objects, rope and cable colours, textures, "
        "light); any change, dissolve or double image there is a seam_color defect.",
        *([f"Screen-fixed HUD copied from the original (identical on both sides by design): {hud}."] if hud else []),
        "Report every defect on the RESTYLED side, using the original only as the reference for what should be there. "
        "Do not report flaws that the original shows identically (game physics, clipping already in the original), "
        "and do not report the intended material change itself.",
        "Categories:",
        *[f"- {name}: {text}" for name, text in CATEGORIES.items()],
        "Severity: high = a casual viewer notices it at once; medium = noticeable when watching attentively; low = minor.",
        "Times are seconds from the start of this clip; give start and end of each problem (end >= start). "
        "`where` names the screen area. Write `description` and `summary` in Chinese. "
        "verdict = fail if there is any high issue or two or more medium issues, else pass.",
    ]
    return "\n".join(lines)


SEAM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "consistent": {"type": "BOOLEAN"},
        "severity": {"type": "STRING", "enum": ["high", "medium", "low", "none"]},
        "changed": {"type": "ARRAY", "items": {"type": "STRING"}},
        "description": {"type": "STRING"}},
    "required": ["consistent", "severity", "changed", "description"]}
SEAM_WINDOW = 1.5       # seconds on either side of a join in the close-up clip


def seam_prompt(seam: dict, layout: str) -> str:
    other = "right" if layout == "left" else "bottom"
    base = (f"This {2 * SEAM_WINDOW:g}-second clip shows the ORIGINAL game video on the {layout} and an AI-restyled "
            f"version on the {other}, frame-synchronised. At {SEAM_WINDOW:g} s two separately generated parts of the "
            "restyled video meet. ")
    if seam["seam"] == "continuous":
        task = ("In the original this is one continuous shot: nothing changes at that moment. Look only at the restyled "
                "side and decide whether a viewer could notice the join: any change of terrain, ground, water, rocks, "
                "trees, objects, rope or cable colour, textures, lighting, colour grading or realism, any object that "
                "pops, morphs or vanishes, any dissolve or double image. consistent = false for any such change.")
    else:
        task = ("The original has a camera cut there, so the content may change. Decide only whether the restyled look "
                "(materials, colour grading, realism level, the same objects rendered the same way) stays consistent "
                "across the cut as it does in the original. consistent = false if the look visibly changes.")
    return (base + task + " List what changed in `changed`; write `description` in Chinese; severity none when "
            "consistent, high when a casual viewer notices it at once.")


def seam_checks(job: Path, source_id: str, assembled: dict, clip: Path, layout: str) -> list[dict]:
    """Gemini close-up of every join: 3 s around it at 10 fps, one question each."""
    found = []
    for seam in assembled["seams"]:
        start = max(0.0, seam["time"] - SEAM_WINDOW)
        piece = job / "review" / f"{source_id}-seam-{seam['segment']}.mp4"
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{2 * SEAM_WINDOW:.3f}", "-i", clip,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-an", piece])
        part = winky.inline(piece, "video/mp4")
        part["videoMetadata"] = {"fps": 10}
        answer = winky.gemini([part, {"text": seam_prompt({**seam, "time": seam["time"] - start}, layout)}],
                              schema=SEAM_SCHEMA)
        found.append({**seam, "answer": answer})
        log(f"{source_id}: seam {seam['segment']} at {seam['time']:.2f} s: "
            + ("consistent" if answer.get("consistent") else f"changes ({', '.join(answer.get('changed') or [])})"))
    return found


def segment_for(time_a: float, time_b: float, assembled: dict) -> list[str]:
    hits = [s["id"] for s in assembled["segments"] if s["start"] - .25 <= time_b and time_a <= s["end"] + .25]
    return hits or [assembled["segments"][-1]["id"]]


def review_source(job: Path, state: dict, source_id: str, plan_row: dict, fps: float = 2.0) -> dict:
    record = job / "state/assembled" / f"{source_id}.json"
    newest = max((p.stat().st_mtime for p in (job / "ai").glob(f"{source_id}-*.mp4")), default=0.0)
    if record.stat().st_mtime < newest:
        raise ValueError(f"{source_id}: segments were rendered after the last assemble; run assemble first "
                         "(reviewing now would judge the old video)")
    assembled = read(record)
    clip, layout = proxy(job, state, source_id, assembled)
    size = clip.stat().st_size
    if size > 18 * 1024 * 1024:
        raise RuntimeError(f"{source_id}: review proxy is {size / 1e6:.0f} MB, too large to send inline")
    log(f"{source_id}: asking Gemini to review ({size / 1e6:.1f} MB comparison clip)")
    part = winky.inline(clip, "video/mp4")
    part["videoMetadata"] = {"fps": fps}
    answer = winky.gemini([part, {"text": prompt(state, plan_row, assembled, layout)}], schema=SCHEMA)
    issues = []
    for number, issue in enumerate(answer.get("issues") or [], 1):
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        a, b = min(a, b), max(a, b)
        issues.append({"n": number, **issue, "start": round(a, 2), "end": round(b, 2),
                       "segments": segment_for(a, b, assembled), "source": "gemini"})
    def add(**issue):
        issues.append({"n": len(issues) + 1, **issue})
    for seam in assembled.get("colour") or []:
        if seam["residual"] > 6:
            when = next((s["time"] for s in assembled["seams"] if s["segment"] == seam["before"]), 0.0)
            add(start=when, end=when, category="seam_color", severity="medium", where="whole frame", source="measured",
                description=f"接缝 {seam['before']} 处颜色校正后仍差 {seam['residual']}（0-255）", segments=[seam["before"]])
    # Measured: how far the two versions differ where the video switches from one to the other.
    for seam in assembled["seams"]:
        if seam.get("jump") in ("visible", "slight"):
            add(start=seam["time"], end=seam["time"], category="seam_color",
                severity="high" if seam["jump"] == "visible" else "medium", where="whole frame", source="measured",
                description=f"接缝 {seam['segment']} 处两段画面差异 {seam['mismatch']}（局部 {seam.get('local', 0)}），画面自身运动只有 {seam['motion']}"
                            f"（{'明显跳变' if seam['jump'] == 'visible' else '轻微跳变'}）",
                segments=[seam["segment"]])
    # Gemini close-up of every join.
    for seam in seam_checks(job, source_id, assembled, clip, layout):
        reply = seam["answer"]
        if not reply.get("consistent") and reply.get("severity") != "none":
            add(start=round(seam["time"] - .3, 2), end=round(seam["time"] + .3, 2), category="seam_color",
                severity=reply.get("severity", "medium"), where="whole frame", source="gemini-seam",
                description=f"接缝 {seam['segment']}：{reply.get('description', '')}"
                            + (f"（变化：{'、'.join(reply['changed'])}）" if reply.get("changed") else ""),
                segments=[seam["segment"]])
    if any(i["severity"] == "high" for i in issues) or sum(i["severity"] == "medium" for i in issues) >= 2:
        answer["verdict"] = "fail"
    rounds_path = job / "state/review" / f"{source_id}.json"
    history = read(rounds_path) if rounds_path.is_file() else {"rounds": []}
    number = len(history["rounds"]) + 1
    result = {"source": source_id, "round": number, "output": assembled["output"], "verdict": answer.get("verdict"),
              "summary": answer.get("summary", ""), "issues": issues, "compare": str(clip)}
    history["rounds"].append({k: result[k] for k in ("round", "verdict", "summary")} | {"issues": len(issues)})
    write(rounds_path, history)
    write(job / "reports/review" / f"{source_id}-r{number}.json", result)
    write_text(job / "reports/review" / f"{source_id}-r{number}.md", markdown(result))
    return result


def markdown(result: dict) -> str:
    lines = [f"# Review {result['source']} round {result['round']}: {result['verdict']}", "", result["summary"], ""]
    for issue in result["issues"]:
        lines.append(f"- #{issue['n']} [{issue['severity']}] {issue['category']} {issue['start']:.2f}-{issue['end']:.2f} s "
                     f"({', '.join(issue['segments'])}; {issue.get('where', '')}; {issue['source']}): {issue['description']}")
    return "\n".join(lines) + "\n"


def stills(job: Path, state: dict, source_id: str, times: list[float], span: float = .3) -> list[Path]:
    """For each time: original (top row) and restyled (bottom row) at t-span, t, t+span."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    width, height = assembled["size"]
    tile_w = 360 if height >= width else 560
    tile_h = round(tile_w * height / width)
    label = font(18)
    outputs = []
    for t in times:
        moments = [max(0.0, t - span), t, min(assembled["duration"] - .02, t + span)]
        canvas = Image.new("RGB", (3 * tile_w, 2 * (tile_h + 26)), "white")
        draw = ImageDraw.Draw(canvas)
        for row, (path, offset, name) in enumerate(((source["path"], assembled["keep"][0], "original"),
                                                    (assembled["output"], 0.0, "restyled"))):
            for column, moment in enumerate(moments):
                raw = run([FFMPEG, "-v", "error", "-ss", f"{moment + offset:.3f}", "-i", path, "-frames:v", "1",
                           "-vf", f"scale={tile_w}:{tile_h}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          binary=True)
                if len(raw) >= tile_w * tile_h * 3:
                    canvas.paste(Image.frombytes("RGB", (tile_w, tile_h), raw[: tile_w * tile_h * 3]),
                                 (column * tile_w, row * (tile_h + 26) + 26))
                draw.text((column * tile_w + 4, row * (tile_h + 26) + 4), f"{name} {moment:.2f} s", fill="black", font=label)
        out = job / "review/frames" / f"{source_id}-{t:07.2f}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, quality=88)
        outputs.append(out)
    return outputs
