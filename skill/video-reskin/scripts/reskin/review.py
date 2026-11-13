"""Quality gate. `check` judges each rendered segment before anything continues from it: Gemini watches the segment
beside its source clip, and the frame the next segment will start from; then the segments up to this one are joined
(a preview, `assemble --upto`) and Gemini watches the join to the segment before it (1 s either side, beside the
original) with the frame this segment was told to start from beside its own first frame. There is no review of the
whole video afterwards: a video whose every segment and seam passed is done. The agent confirms what Gemini reports,
on one three-frame sheet per issue, and looks at the pictures no model judges for it: five frames across the seam
(SEAM_MOMENTS) and the handoff frame. `frames` cuts stills around any time of a delivery."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import winky
from reskin.analyze import font
from reskin.assemble import COLOUR_REVIEW, SEAM_REVIEW
from reskin.common import FFMPEG, log, read, run, write, write_text
from reskin.job import style as job_style
from reskin.prompts import describe

CATEGORIES = {
    "continuity": "the restyled side shows something the original does not (cartoon material left over, an object or "
                  "character changing identity, shape, colour or count, things appearing or vanishing, motion that no "
                  "longer matches)",
    "motion": "something moves differently from the original: what the original moves (conveyor belts and the items "
              "on them, walking or running characters, carried, thrown or flying objects, water, rising stacks, the "
              "camera) stands still, moves the other way, faster or slower, or jumps on the restyled side, or something "
              "the original keeps still moves",
    "artifact": "bug: morphing, melting, warping, flicker, jitter, frozen or repeated frames, garbled UI text or numbers, "
                "broken limbs, smeared detail, strong noise or a fragmented look",
    "added_text": "text the original does not have: captions, warning lines, watermarks or garbled characters drawn "
                  "anywhere (look along the bottom and top edges in particular)",
    "seam_color": "a sudden jump in colour, brightness, contrast, texture or realism between consecutive moments, "
                  "especially at the listed seam times",
    "leftover_overlay": "an element explicitly named in the removal list is still visible; source overlays not named there are allowed",
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


INLINE_LIMIT = 256_000_000     # bytes: Winky's Gemini rejects a larger inline video part (measured)
PROXY_CRF = 4                  # review clips are near-lossless: Gemini judges the delivered detail
BUDGET = .93                   # share of INLINE_LIMIT the video bitrate is capped to (container and VBV headroom)
REVIEW_FPS = 24                # frame rate of the clips Gemini gets (the delivery's); it samples them at its own rate
ISSUE_SPAN = .3                # an issue shorter than two spans gets frames at its middle and that far either side
SEAM_MOMENTS = (-.5, -.25, 0.0, .25, .5)     # the agent's five frames across every seam (user rule)
SEAM_SPAN = 1.0                # seconds of the joined preview Gemini watches on either side of a seam (user rule)


def grid_clip(panels: list[dict], size: tuple[int, int], output: Path, crf: int = PROXY_CRF,
              preset: str = "medium") -> Path:
    """Two panels on one clip, each at the delivery's own `size` (Gemini judges the delivered detail) and labelled,
    side by side for portrait, stacked for landscape. A panel is {"path", "start", "end", "label"} and optionally
    "stretch" (its time is multiplied by it, to put a slightly shorter result on the source's clock); both share one
    clock from their starts. Encoded at `crf` (near-lossless) with the bitrate capped so the clip uses as much of
    INLINE_LIMIT as it needs and no more; a clip that still overshoots is encoded again with a lower cap."""
    width, height = round(size[0] / 2) * 2, round(size[1] / 2) * 2
    portrait = height >= width
    inputs, chains, lengths = [], [], []
    for panel in panels:
        start, end = max(0.0, float(panel["start"])), float(panel["end"])
        stretch = float(panel.get("stretch", 1.0))
        inputs += ["-ss", f"{start:.6f}", "-t", f"{max(1 / REVIEW_FPS, end - start):.6f}", "-i", str(panel["path"])]
        lengths.append(max(1 / REVIEW_FPS, end - start) * stretch)
    longest = max(lengths)
    for index, (panel, length) in enumerate(zip(panels, lengths)):
        chains.append(f"[{index}:v]setpts=(PTS-STARTPTS)*{float(panel.get('stretch', 1.0)):.6f},fps={REVIEW_FPS},scale={width}:{height}:flags=lanczos,setsar=1,"
                      f"tpad=stop_mode=clone:stop_duration={longest - length + .2:.3f},trim=duration={longest:.6f}"
                      f"{_drawtext(panel['label'])}[p{index}]")
    graph = ";".join(chains) + f";[p0][p1]{'hstack' if portrait else 'vstack'}=inputs=2,format=yuv420p[v]"
    output.parent.mkdir(parents=True, exist_ok=True)
    cap = INLINE_LIMIT * BUDGET * 8 / longest          # bits per second
    while True:
        run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-an",
             "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-maxrate", f"{cap:.0f}",
             "-bufsize", f"{cap:.0f}", "-r", str(REVIEW_FPS), output])
        size = output.stat().st_size
        if size <= INLINE_LIMIT or cap < 2e6:
            return output
        cap *= INLINE_LIMIT * BUDGET / size


def frame_at(path: str, t: float, width: int, height: int) -> Image.Image:
    """One frame at `t` seconds at width x height (accurate seek)."""
    raw = run([FFMPEG, "-v", "error", "-ss", f"{max(0.0, t):.4f}", "-i", path, "-frames:v", "1",
               "-vf", f"scale={width}:{height}:flags=lanczos", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
    if len(raw) < width * height * 3:
        return Image.new("RGB", (width, height), "black")
    return Image.frombytes("RGB", (width, height), raw[: width * height * 3])


def still_pair(upper: tuple[str, float, str], lower: tuple[str, float, str], size: tuple[int, int],
               output: Path) -> Path:
    """Two frames at the delivery's full size, each (path, seconds, label): side by side for a portrait video,
    stacked for a landscape one, labelled above; lossless PNG (the agent judges detail on them)."""
    width, height = size
    portrait = height >= width
    band = 34
    canvas = Image.new("RGB", ((2 * width if portrait else width), (height + band) * (1 if portrait else 2)), "white")
    draw = ImageDraw.Draw(canvas)
    text = font(24)
    for number, (path, t, label) in enumerate((upper, lower)):
        x, y = (number * width, 0) if portrait else (0, number * (height + band))
        draw.text((x + 8, y + 4), label, fill="black", font=text)
        canvas.paste(frame_at(path, t, width, height), (x, y + band))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, optimize=False, compress_level=1)
    return output


def issue_moments(start: float, end: float, last: float) -> list[float]:
    """Three moments that show an issue: its start, middle and end, or for a short one its middle and ISSUE_SPAN
    either side; inside [0, last]."""
    a, b = min(start, end), max(start, end)
    middle = (a + b) / 2
    moments = [a, middle, b] if b - a >= 2 * ISSUE_SPAN else [middle - ISSUE_SPAN, middle, middle + ISSUE_SPAN]
    return [round(min(max(0.0, m), last), 3) for m in moments]


def three_frames(rows: list[tuple[str, callable, str]], moments: list[float], size: tuple[int, int],
                 output: Path, shown: float = 0.0) -> Path:
    """Original (top row) and restyled (bottom row) at each moment; a row is (path, moment -> seconds in that file,
    name). Lossless PNG."""
    width, height = size
    tile_w = 540 if height >= width else 960
    tile_h = round(tile_w * height / width)
    label = font(18)
    canvas = Image.new("RGB", (len(moments) * tile_w, len(rows) * (tile_h + 26)), "white")
    draw = ImageDraw.Draw(canvas)
    for row, (path, to_file, name) in enumerate(rows):
        for column, moment in enumerate(moments):
            canvas.paste(frame_at(path, to_file(moment), tile_w, tile_h), (column * tile_w, row * (tile_h + 26) + 26))
            draw.text((column * tile_w + 4, row * (tile_h + 26) + 4), f"{name} {moment + shown:.2f} s", fill="black", font=label)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, compress_level=1)
    return output


def issue_sheets(issues: list[dict], rows: list[tuple[str, callable, str]], to_clip: callable, last: float,
                 size: tuple[int, int], folder: Path) -> None:
    """One three-frame sheet per issue (`sheet` on the issue), for the agent to confirm it. Issue times are
    delivered-video seconds; `to_clip` turns them into the rows' moment scale."""
    for issue in issues:
        moments = issue_moments(to_clip(issue["start"]), to_clip(issue["end"]), last)
        issue["sheet"] = str(three_frames(rows, moments, size, folder / f"{issue['n']:02d}-{issue['category']}.png",
                                          shown=issue["start"] - to_clip(issue["start"])))


def seam_sheet(job: Path, state: dict, source_id: str, assembled: dict, seam: dict, out: Path) -> Path:
    """Five moments across a seam (SEAM_MOMENTS around the middle of the switch): original on top, restyled below,
    one lossless PNG per seam."""
    source = next(r for r in state["sources"] if r["id"] == source_id)
    width, height = assembled["size"]
    tile_w = 432 if height >= width else 768
    tile_h = round(tile_w * height / width)
    band = 26
    label = font(18)
    canvas = Image.new("RGB", (len(SEAM_MOMENTS) * tile_w, 2 * (tile_h + band)), "white")
    draw = ImageDraw.Draw(canvas)
    last = assembled["duration"] - 1 / assembled["fps"]
    for column, step in enumerate(SEAM_MOMENTS):
        t = min(max(0.0, seam["time"] + step), last)
        for row_number, (path, offset, name) in enumerate(((source["path"], assembled["keep"][0], "original"),
                                                           (assembled["output"], 0.0, "restyled"))):
            y = row_number * (tile_h + band)
            canvas.paste(frame_at(path, t + offset, tile_w, tile_h), (column * tile_w, y + band))
            draw.text((column * tile_w + 4, y + 4), f"{name} {step:+.2f} s ({t:.2f})", fill="black", font=label)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, compress_level=1)
    return out


def prompt(state: dict, plan_row: dict, assembled: dict, layout: str) -> str:
    other = "right" if layout == "left" else "bottom"
    seams = ", ".join(f"{s['time']:.2f} s ({'hard cut' if s['seam'] == 'cut' else 'inside a shot'})"
                      for s in assembled["seams"])
    removal = describe(plan_row["remove"]) or "nothing named"
    style = job_style(state)
    look = " ".join(part for part in (style.get("look"), style.get("ground")) if part)
    lines = [
        "You are a strict quality reviewer for AI-restyled game advertising videos.",
        f"The video shows the ORIGINAL on the {layout} and the RESTYLED version on the {other}, frame-synchronised "
        f"(same timestamps). The restyle goal: {describe(plan_row['materials'])} — game-rendered cartoon materials replaced by "
        f"realistic real-world materials (style preset: {style.get('label', 'custom')}) while composition, camera motion, characters, object "
        "positions, counts and timing stay exactly as in the original. Original overlays and HUD numbers, icons and buttons must stay unless explicitly listed for removal.",
        *([f"Restyled side, target {look[0].lower()}{look[1:]}"] if look else []),
        f"Removed on purpose (must NOT appear on the restyled side): {removal}. The closing card was cut off on purpose.",
        *([f"The restyled video was rendered in segments and joined at: {seams}. Inside a shot each join is a short "
           "plain cut or a very short dissolve while the camera moves fast; report it only when a viewer would notice a "
           "jump in colour, brightness, materials, objects or realism, or a double image."] if seams else []),
        "Report every defect on the RESTYLED side, using the original only as the reference for what should be there. "
        "Do not report flaws that the original shows identically (game physics, clipping already in the original), "
        "and do not report the intended material change itself.",
        "Compare movement, not only single frames: follow every moving thing on the original side from frame to frame "
        "(items riding a conveyor, characters walking, objects carried or flying, stacks growing, the camera) and check "
        "that the same thing on the restyled side moves the same way at the same speed; a belt that runs while its "
        "items stay put, or items that slide while the belt stands, is a motion issue even when every single frame "
        "looks right.",
        "Categories:",
        *[f"- {name}: {text}" for name, text in CATEGORIES.items()],
        "Severity: high = a casual viewer notices it at once; medium = noticeable when watching attentively; low = minor.",
        "Times are seconds from the start of this clip; give start and end of each problem (end >= start). "
        "`where` names the screen area. Write `description` and `summary` in Chinese. "
        "verdict = fail if there is any high issue or two or more medium issues, else pass.",
    ]
    return "\n".join(lines)


def stills(job: Path, state: dict, source_id: str, times: list[float], span: float = .3) -> list[Path]:
    """For each time: original (top row) and restyled (bottom row) at t-span, t, t+span."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    rows = [(source["path"], lambda m: m + assembled["keep"][0], "original"),
            (assembled["output"], lambda m: m, "restyled")]
    return [three_frames(rows, [max(0.0, t - span), t, min(assembled["duration"] - .02, t + span)],
                         tuple(assembled["size"]), job / "review/frames" / f"{source_id}-{t:07.2f}.png")
            for t in times]


def gemini_issues(answer: dict, shift: float, segment: str, origin: str) -> list[dict]:
    issues = []
    for issue in answer.get("issues") or []:
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        issues.append({**issue, "start": round(shift + min(a, b), 2), "end": round(shift + max(a, b), 2),
                       "segments": [segment], "source": "gemini", "part": origin})
    return issues


def failed(answer: dict, issues: list[dict]) -> bool:
    return answer.get("verdict") == "fail" or any(i["severity"] == "high" for i in issues)


def seam_prompt(entry: dict, t0: float, continuous: bool, layout: str, stills: bool) -> str:
    other = "right" if layout == "left" else "bottom"
    where = entry["time"] - t0
    kind = ("inside one camera shot: a plain cut, or a very short dissolve, while the camera moves" if continuous
            else "at a camera cut of the original: only the look has to carry over")
    lines = [
        "You are a strict quality reviewer for AI-restyled game advertising videos.",
        f"The video shows the ORIGINAL on the {layout} and the RESTYLED version on the {other}, frame-synchronised, "
        f"{2 * SEAM_SPAN:g} seconds around the moment ({where:.2f} s into this clip) where two separately rendered parts "
        f"of the restyled video are joined, {kind}.",
        "Report on the restyled side what a viewer would notice at the join: a jump in colour, brightness, contrast, "
        "materials, textures or realism; objects, characters, text or logos that change, appear or vanish; a double "
        "image; a stutter, a jump or a change of speed in motion or camera. Do not report what the original does "
        "identically.",
        *(["The pictures after the video: A is the frame the second part was told to start from (taken from the end of "
           "the first part), B is the second part's own first frame, C the original at that moment. B must look like A "
           "(the same materials, colours, brightness, objects, characters and no added text); report every difference "
           "between A and B, and anything in A or B the original C does not show, as issues at the join."]
          if stills else []),
        "Categories:", *[f"- {name}: {text}" for name, text in CATEGORIES.items()],
        "Severity: high = a casual viewer notices it at once; medium = noticeable when watching attentively; low = minor.",
        "Times are seconds from the start of this clip. Write `description` and `summary` in Chinese. verdict = fail "
        "if there is any high or medium issue, else pass."]
    return "\n".join(lines)


def seam_check(job: Path, state: dict, source_id: str, plan_row: dict, seg: dict, record: dict,
               folder: Path) -> tuple[list[dict], dict | None]:
    """The join to the segment before this one, on a preview of every segment up to this one: Gemini watches
    SEAM_SPAN s either side beside the original (and, inside a shot, compares the frame this segment was told to
    start from with its own first frame); measured seam and colour figures join the issues; the agent gets the
    seam's five-frame sheet. Returns (issues, seam info), or ([], {"pending": ...}) while the segment before it has
    no approved result."""
    from reskin import assemble as ASSEMBLE, render as RENDER
    index = next(i for i, s in enumerate(plan_row["segments"]) if s["id"] == seg["id"])
    after_original = any(o["frames"][1] == seg["frames"][0] for o in plan_row.get("originals") or [])
    if index == 0 and not after_original:
        return [], None
    previous = plan_row["segments"][index - 1] if index else None
    if previous and not RENDER.approved(job, f"{source_id}:{previous['id']}"):
        return [], {"pending": f"{source_id}:{previous['id']} has no approved result yet: check this segment again "
                               "once it is approved"}
    preview = ASSEMBLE.assemble_source(job, state, source_id, plan_row, job_style(state), upto=seg["id"])
    entry = preview["seams"][-1]
    continuous = entry["seam"] == "continuous"
    source = next(r for r in state["sources"] if r["id"] == source_id)
    keep = plan_row["keep"][0]
    last = preview["duration"] - 1 / preview["fps"]
    t0, t1 = max(0.0, entry["time"] - SEAM_SPAN), min(last, entry["time"] + SEAM_SPAN)
    size = tuple(preview["size"])
    clip = grid_clip([{"path": source["path"], "start": keep + t0, "end": keep + t1, "label": "ORIGINAL"},
                      {"path": preview["output"], "start": t0, "end": t1, "label": "RESTYLED"}], size,
                     folder / f"seam-{entry['time']:07.2f}.mp4")
    layout = "left" if size[1] >= size[0] else "top"
    parts = [winky.inline(clip, "video/mp4", fps=winky.sample_fps())]
    stills = False
    chain = job / "refs/chain" / f"{source_id}-{seg['id']}-start.png"
    if continuous and chain.is_file():
        from reskin.survey import scaled
        first = folder / "start-first.png"
        frame_at(str(job / "ai" / f"{source_id}-{seg['id']}.mp4"), 0.0,
                 record["ai"]["width"], record["ai"]["height"]).save(first)
        original = folder / "start-original.png"
        frame_at(source["path"], seg["render_frames"][0] / plan_row["fps"], *size).save(original)
        for label, path in (("A", chain), ("B", first), ("C", original)):
            parts += [{"text": f"Picture {label}"},
                      winky.inline(scaled(path, folder / f"start-{label}.jpg"), "image/jpeg")]
        stills = True
    parts.append({"text": seam_prompt(entry, t0, continuous, layout, stills)})
    log(f"{source_id}:{seg['id']}: asking Gemini to check the seam at {entry['time']:.2f} s "
        f"({clip.stat().st_size / 1e6:.1f} MB)")
    answer = winky.gemini(parts, schema=SCHEMA)
    issues = gemini_issues(answer, t0, seg["id"], "seam")
    if entry.get("mode") == "dissolve" and entry.get("same", 0) > SEAM_REVIEW:
        issues.append({"start": round(entry["time"] - .25, 2), "end": round(entry["time"] + .25, 2),
                       "category": "seam_color", "severity": "medium", "where": "whole frame", "source": "measured",
                       "segments": [seg["id"]], "part": "seam",
                       "description": f"接缝处两段画面差异较大（两版画面相差 {entry['same']}，跳变为正常帧间变化的 "
                                      f"{entry['ratio']} 倍），已用轻微叠化"})
    colour = (preview.get("colour") or [{}])[-1]
    if colour.get("before") == seg["id"] and colour.get("residual", 0) > COLOUR_REVIEW:
        issues.append({"start": entry["time"], "end": entry["time"], "category": "seam_color", "severity": "medium",
                       "where": "whole frame", "source": "measured", "segments": [seg["id"]], "part": "seam",
                       "description": f"接缝处颜色校正后仍差 {colour['residual']}（0-255）"})
    for problem in preview.get("problems") or []:
        if problem.startswith(f"{seg['id']}:") or f"before {seg['id']}" in problem:
            issues.append({"start": entry["time"], "end": entry["time"], "category": "continuity", "severity": "medium",
                           "where": "whole frame", "source": "measured", "segments": [seg["id"]], "part": "seam",
                           "description": problem})
    rows = [(source["path"], lambda m: m + keep, "original"), (preview["output"], lambda m: m, "restyled")]
    # Gemini first: the agent reviews the seam on its own five-frame sheet only when Gemini found nothing there;
    # otherwise it confirms Gemini's issues on their own sheets.
    info = {"time": entry["time"], "seam": entry["seam"], "mode": entry.get("mode"), "ratio": entry.get("ratio"),
            "same": entry.get("same"), "preview": preview["output"], "summary": answer.get("summary", ""),
            "verdict": "fail" if failed(answer, issues) or any(i["severity"] == "medium" for i in issues) else "pass",
            "sheet": None if issues else
            str(seam_sheet(job, state, source_id, preview, entry, folder / f"seam-{entry['time']:07.2f}.png")),
            "rows": rows, "last": last, "size": size}
    return issues, info


def check_segment(job: Path, state: dict, source_id: str, plan_row: dict, seg: dict) -> dict:
    """Judge one rendered segment before anything continues from it. Gemini (sampled at winky.GEMINI_FPS, clips
    near-lossless up to the inline limit) watches the segment beside its source clip and looks at the frame the next
    segment of the shot starts from; then the seam to the segment before it (seam_check). The agent gets one
    three-frame sheet per issue, the seam's five-frame sheet and, full size, the handoff frame. Times are seconds of
    the delivered video (source time minus the kept start). The result is `approve`d only when this passes."""
    from reskin import render as RENDER
    key = f"{source_id}:{seg['id']}"
    record = read(RENDER.state_path(job, key)) if RENDER.state_path(job, key).is_file() else {}
    if record.get("status") != "done":
        raise ValueError(f"{key} has no finished result to check; run render first")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    fps = plan_row["fps"]
    clip = job / "segments" / f"{source_id}-{seg['id']}.mp4"
    if not clip.is_file():
        RENDER.cut_clip(Path(source["path"]), seg["render_frames"], fps, plan_row["width"], plan_row["height"], clip)
    result_path = job / "ai" / f"{source_id}-{seg['id']}.mp4"
    begin = seg["render_frames"][0] / fps
    length = (seg["render_frames"][1] - seg["render_frames"][0]) / fps
    stretch = length / record["ai"]["duration"]
    size = (record["ai"]["width"], record["ai"]["height"])
    offset = begin - plan_row["keep"][0]          # clip time -> delivered video time
    name = f"{source_id}-{seg['id']}-a{record.get('attempt', 1)}"
    folder = job / "review/check" / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    to_result = lambda u: min(u / stretch, record["ai"]["duration"] - .02)
    following = next((s for s in plan_row["segments"]
                      if (RENDER.previous_of(plan_row, s) or {}).get("id") == seg["id"]), None)
    handoff = (following["render_frames"][0] - seg["render_frames"][0]) / fps if following else None
    handoff_still = None
    if handoff is not None:
        t = offset + handoff
        handoff_still = str(still_pair((source["path"], begin + handoff, f"ORIGINAL {t:.2f} s (next segment starts here)"),
                                       (str(result_path), to_result(handoff), f"RESTYLED {t:.2f} s (next segment starts here)"),
                                       size, folder / f"handoff-{t:07.2f}.png"))
    compare = grid_clip([{"path": clip, "start": 0.0, "end": length, "label": "ORIGINAL"},
                         {"path": result_path, "start": 0.0, "end": record["ai"]["duration"], "label": "RESTYLED",
                          "stretch": stretch}], size, job / "review/check" / f"{name}.mp4")
    layout = "left" if size[1] >= size[0] else "top"
    text = prompt(state, plan_row, {"seams": []}, layout) + (
        f"\nThis clip is one rendered segment of the video ({offset:.2f}-{offset + length:.2f} s of it); the next "
        "segment continues from its last frames, so check the end of the clip as closely as the rest.")
    parts = [winky.inline(compare, "video/mp4", fps=winky.sample_fps())]
    if handoff_still:
        from reskin.survey import scaled
        text += (f"\nThe picture after the video is the frame at {handoff:.2f} s of this clip (original | restyled) "
                 "that the next segment will be drawn from: whatever is wrong in it (leftover text or logos, a changed "
                 "or missing character, crowd or object, a wrong material) is copied into the rest of the shot, so "
                 "report every flaw in it as a high issue at that time.")
        parts.append(winky.inline(scaled(Path(handoff_still), folder / "handoff.jpg", 2048), "image/jpeg"))
    log(f"{key}: asking Gemini to check the segment ({compare.stat().st_size / 1e6:.1f} MB)")
    answer = winky.gemini(parts + [{"text": text}], schema=SCHEMA)
    issues = gemini_issues(answer, offset, seg["id"], "segment")
    for problem in record.get("problems") or []:
        issues.append({"start": round(offset, 2), "end": round(offset + length, 2), "category": "continuity",
                       "severity": "high", "where": "whole frame", "source": "measured", "segments": [seg["id"]],
                       "part": "segment", "description": problem})
    for number, issue in enumerate(issues, 1):
        issue["n"] = number
    rows = [(source["path"], lambda u: begin + u, "original"), (str(result_path), to_result, "restyled")]
    issue_sheets(issues, rows, lambda t: t - offset, length - 1 / fps, size, folder)
    seam_issues, seam = seam_check(job, state, source_id, plan_row, seg, record, folder)
    for number, issue in enumerate(seam_issues, len(issues) + 1):
        issue["n"] = number
    if seam and not seam.get("pending"):
        issue_sheets(seam_issues, seam.pop("rows"), lambda t: t, seam.pop("last"), seam.pop("size"), folder)
    issues += seam_issues
    verdict = "fail" if failed(answer, issues) or (seam and (seam.get("pending") or seam["verdict"] == "fail")) \
        else "pass"
    # Gemini first: the agent reviews the handoff frame itself only when no issue Gemini reported covers it;
    # otherwise it confirms those issues on their sheets.
    at_handoff = handoff is not None and any(i["part"] == "segment" and i["start"] - ISSUE_SPAN
                                             <= offset + handoff <= i["end"] + ISSUE_SPAN for i in issues)
    result = {"segment": key, "attempt": record.get("attempt", 1), "generation_id": record["generation_id"],
              "verdict": verdict, "summary": answer.get("summary", ""), "issues": issues, "compare": str(compare),
              "seam": seam, "handoff": None if handoff is None else round(offset + handoff, 2),
              "handoff_still": None if at_handoff else handoff_still, "handoff_reported": at_handoff,
              "next": following and f"{source_id}:{following['id']}"}
    write(job / "reports/check" / f"{name}.json", result)
    lines = [f"# Check {key} attempt {result['attempt']}: {verdict}", "", result["summary"], ""]
    lines += [f"- #{i['n']} [{i['severity']}] {i['part']} {i['category']} {i['start']:.2f}-{i['end']:.2f} s "
              f"({i.get('where', '')}; {i['source']}): {i['description']}"
              + (f"\n  sheet: {i['sheet']}" if i.get("sheet") else "") for i in issues]
    if seam and seam.get("pending"):
        lines += ["", f"- seam: not checked: {seam['pending']}"]
    elif seam:
        lines += ["", f"- seam at {seam['time']:.2f} s ({seam['seam']}{', ' + seam['mode'] if seam.get('mode') else ''}): "
                      f"{seam['verdict']}; {seam['summary']}",
                  f"  Gemini found nothing: review it yourself on {seam['sheet']}" if seam["sheet"] else
                  "  Gemini reported issues there: confirm them on their sheets above"]
    if result["handoff_still"]:
        lines += ["", f"- handoff: Gemini found nothing: review it yourself (lossless, full size): {handoff_still}"]
    elif at_handoff:
        lines += ["", "- handoff: Gemini reported issues there: confirm them on their sheets above"]
    if result["next"]:
        lines.append(f"- {result['next']} starts from this result's frame at {result['handoff']:.2f} s")
    write_text(job / "reports/check" / f"{name}.md", "\n".join(lines) + "\n")
    return result
