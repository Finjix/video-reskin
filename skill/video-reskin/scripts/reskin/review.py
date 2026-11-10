"""Quality review. `check`: each rendered segment on its own, before the next segment of its shot is rendered from
it (Gemini on the segment beside its source clip, the agent on its stills). `review`: Gemini watches the whole
original and restyled video side by side once and lists what went wrong; the agent looks at stills itself: the whole
video every STILL_STEP s and five frames across every seam (SEAM_MOMENTS). `frames` cuts stills around a reported
time to confirm an issue before a redo."""
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
    "artifact": "bug: morphing, melting, warping, flicker, jitter, frozen or repeated frames, garbled UI text or numbers, "
                "broken limbs, smeared detail, strong noise or a fragmented look",
    "added_text": "text the original does not have: captions, warning lines, watermarks or garbled characters drawn "
                  "anywhere (look along the bottom and top edges in particular)",
    "seam_color": "a sudden jump in colour, brightness, contrast, texture or realism between consecutive moments, "
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


INLINE_LIMIT = 256_000_000     # bytes: Winky's Gemini rejects a larger inline video part (measured)
PROXY_CRF = 4                  # review clips are near-lossless: Gemini judges the delivered detail
BUDGET = .93                   # share of INLINE_LIMIT the video bitrate is capped to (container and VBV headroom)
REVIEW_FPS = 24                # frame rate of the clips Gemini gets (the delivery's); it samples them at its own rate
STILL_STEP = 3.0               # seconds between the agent's full-size stills of the whole video (user rule)
SEAM_MOMENTS = (-1.0, -.5, 0.0, .5, 1.0)     # the agent's five frames across every seam (user rule)


def grid_clip(panels: list[dict], size: tuple[int, int], output: Path, crf: int = PROXY_CRF) -> Path:
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
             "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-maxrate", f"{cap:.0f}",
             "-bufsize", f"{cap:.0f}", "-r", str(REVIEW_FPS), output])
        size = output.stat().st_size
        if size <= INLINE_LIMIT or cap < 2e6:
            return output
        cap *= INLINE_LIMIT * BUDGET / size


def proxy(job: Path, state: dict, source_id: str, assembled: dict) -> tuple[Path, str]:
    """Original (left or top) and restyled (right or bottom) at the delivery's full size, same clock for both."""
    source = next(r for r in state["sources"] if r["id"] == source_id)
    start, end = assembled["keep"]
    width, height = assembled["size"]
    clip = grid_clip([{"path": source["path"], "start": start, "end": end, "label": "ORIGINAL"},
                      {"path": assembled["output"], "start": 0.0, "end": end - start, "label": "RESTYLED"}],
                     (width, height), job / "review" / f"{source_id}-compare.mp4")
    return clip, ("left" if height >= width else "top")


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


def every(length: float, step: float, fps: float) -> list[float]:
    """Moments from 0 every `step` seconds, the last frame included when it lies a third of a step past the last."""
    last = max(0.0, length - 1 / fps)
    moments = [round(i * step, 3) for i in range(int(last / step) + 1)]
    if last - moments[-1] > step / 3:
        moments.append(round(last, 3))
    return moments


def overview_stills(job: Path, state: dict, source_id: str, assembled: dict, round_number: int) -> list[str]:
    """Full-size original | restyled pairs every STILL_STEP s of the delivery, for the agent's own review."""
    source = next(r for r in state["sources"] if r["id"] == source_id)
    folder = job / "review/stills" / f"{source_id}-r{round_number}"
    shutil.rmtree(folder, ignore_errors=True)
    size = tuple(assembled["size"])
    paths = []
    for number, t in enumerate(every(assembled["duration"], STILL_STEP, assembled["fps"]), 1):
        paths.append(str(still_pair((source["path"], assembled["keep"][0] + t, f"ORIGINAL {t:.2f} s"),
                                    (assembled["output"], t, f"RESTYLED {t:.2f} s"), size,
                                    folder / f"{number:02d}-{t:07.2f}.png")))
    return paths


def seam_sheet(job: Path, state: dict, source_id: str, assembled: dict, seam: dict) -> Path:
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
            draw.text((column * tile_w + 4, y + 4), f"{name} {step:+.1f} s ({t:.2f})", fill="black", font=label)
    out = job / "review/seams" / f"{source_id}-{seam['segment']}.png"
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
        "positions, counts and timing stay exactly as in the original. HUD numbers, icons and buttons must stay.",
        *([f"Restyled side, target {look[0].lower()}{look[1:]}"] if look else []),
        f"Removed on purpose (must NOT appear on the restyled side): {removal}. The closing card was cut off on purpose.",
        *([f"The restyled video was rendered in segments and joined at: {seams}. Inside a shot each join is a short "
           "plain cut or a very short dissolve while the camera moves fast; report it only when a viewer would notice a "
           "jump in colour, brightness, materials, objects or realism, or a double image."] if seams else []),
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


def segment_for(time_a: float, time_b: float, assembled: dict) -> list[str]:
    hits = [s["id"] for s in assembled["segments"] if s["start"] - .25 <= time_b and time_a <= s["end"] + .25]
    return hits or [assembled["segments"][-1]["id"]]


def review_source(job: Path, state: dict, source_id: str, plan_row: dict, fps: float | None = None) -> dict:
    """Gemini watches the whole delivery beside the original once (full size, near-lossless, inline up to 256 MB);
    the agent gets lossless stills of the whole video every STILL_STEP s and one five-frame sheet per seam."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    clip, layout = proxy(job, state, source_id, assembled)
    size = clip.stat().st_size
    if size > INLINE_LIMIT:
        raise RuntimeError(f"{source_id}: review clip is {size / 1e6:.0f} MB even at the lowest bitrate, above the "
                           "inline limit")
    fps = fps or winky.sample_fps(assembled["duration"])
    log(f"{source_id}: asking Gemini to review ({size / 1e6:.1f} MB comparison clip, {fps:g} frames/s)")
    answer = winky.gemini([winky.inline(clip, "video/mp4", fps=fps), {"text": prompt(state, plan_row, assembled, layout)}],
                          schema=SCHEMA)
    issues = []
    for number, issue in enumerate(answer.get("issues") or [], 1):
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        a, b = min(a, b), max(a, b)
        issues.append({"n": number, **issue, "start": round(a, 2), "end": round(b, 2),
                       "segments": segment_for(a, b, assembled), "source": "gemini"})
    for seam in assembled.get("seams") or []:
        if seam.get("mode") == "dissolve" and seam.get("same", 0) > SEAM_REVIEW:
            issues.append({"n": len(issues) + 1, "start": round(seam["time"] - .25, 2), "end": round(seam["time"] + .25, 2),
                           "category": "seam_color", "severity": "medium", "where": "whole frame", "source": "measured",
                           "description": f"段内接缝 {seam['segment']} 处两段画面差异较大（两版画面相差 {seam['same']}，"
                                          f"跳变为正常帧间变化的 {seam['ratio']} 倍），直接切会跳，已用轻微叠化，看接缝图确认",
                           "segments": [seam["segment"]]})
    for seam in assembled.get("colour") or []:
        if seam["residual"] > COLOUR_REVIEW:
            when = next((s["time"] for s in assembled["seams"] if s["segment"] == seam["before"]), 0.0)
            issues.append({"n": len(issues) + 1, "start": when, "end": when, "category": "seam_color",
                           "severity": "medium", "where": "whole frame", "source": "measured",
                           "description": f"接缝 {seam['before']} 处颜色校正后仍差 {seam['residual']}（0-255）",
                           "segments": [seam["before"]]})
    for seg in plan_row["segments"]:
        record_path = job / "state/render" / f"{source_id}-{seg['id']}.json"
        record = read(record_path) if record_path.is_file() else {}
        if record.get("kept_rejected"):
            where = next((s for s in assembled["segments"] if s["id"] == seg["id"]), {"start": 0, "end": 0})
            issues.append({"n": len(issues) + 1, "start": where["start"], "end": where["end"], "category": "continuity",
                           "severity": "high", "where": "whole frame", "source": "measured", "segments": [seg["id"]],
                           "description": "视频模型两次结果都偏离了原片的机位和构图（自行推近、切镜或改取景），保留了较接近的一次，"
                                          "看静帧确认"})
    seams = [{**seam, "sheet": str(seam_sheet(job, state, source_id, assembled, seam))} for seam in assembled["seams"]]
    rounds_path = job / "state/review" / f"{source_id}.json"
    history = read(rounds_path) if rounds_path.is_file() else {"rounds": []}
    number = len(history["rounds"]) + 1
    stills = overview_stills(job, state, source_id, assembled, number)
    verdict = "fail" if (answer.get("verdict") == "fail" or any(i["severity"] == "high" for i in issues)) else "pass"
    result = {"source": source_id, "round": number, "output": assembled["output"], "verdict": verdict,
              "summary": answer.get("summary", ""), "issues": issues, "compare": str(clip),
              "stills": stills, "seams": seams}
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
    lines += ["", "## Agent stills (lossless; look at every one)", ""]
    if result.get("stills"):
        lines.append(f"- whole video every {STILL_STEP:g} s: {Path(result['stills'][0]).parent} ({len(result['stills'])} pairs)")
    for seam in result.get("seams") or []:
        how = seam["seam"] + (f", {seam['mode']} (jump {seam['ratio']} of the frame change, drawings {seam['same']} "
                              "apart)" if seam.get("mode") else "")
        lines.append(f"- seam {seam['segment']} at {seam['time']:.2f} s ({how}): {seam['sheet']}")
    return "\n".join(lines) + "\n"


def stills(job: Path, state: dict, source_id: str, times: list[float], span: float = .3) -> list[Path]:
    """For each time: original (top row) and restyled (bottom row) at t-span, t, t+span."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    width, height = assembled["size"]
    tile_w = 540 if height >= width else 960
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
                canvas.paste(frame_at(path, moment + offset, tile_w, tile_h), (column * tile_w, row * (tile_h + 26) + 26))
                draw.text((column * tile_w + 4, row * (tile_h + 26) + 4), f"{name} {moment:.2f} s", fill="black", font=label)
        out = job / "review/frames" / f"{source_id}-{t:07.2f}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, compress_level=1)
        outputs.append(out)
    return outputs


def check_segment(job: Path, state: dict, source_id: str, plan_row: dict, seg: dict) -> dict:
    """Review one rendered segment before anything continues from it: Gemini watches the segment's source clip beside
    its result once, and the agent gets lossless stills of it every STILL_STEP s plus the frame the next segment of
    the shot starts from. Times are seconds of the delivered video (source time minus the kept start)."""
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
    compare = grid_clip([{"path": clip, "start": 0.0, "end": length, "label": "ORIGINAL"},
                         {"path": result_path, "start": 0.0, "end": record["ai"]["duration"], "label": "RESTYLED",
                          "stretch": stretch}], size, job / "review/check" / f"{name}.mp4")
    layout = "left" if size[1] >= size[0] else "top"
    text = prompt(state, plan_row, {"seams": []}, layout) + (
        f"\nThis clip is one rendered segment of the video ({offset:.2f}-{offset + length:.2f} s of it); the next "
        "segment continues from its last frames, so check the end of the clip as closely as the rest.")
    log(f"{key}: asking Gemini to check the segment ({compare.stat().st_size / 1e6:.1f} MB)")
    answer = winky.gemini([winky.inline(compare, "video/mp4", fps=winky.sample_fps(length)), {"text": text}],
                          schema=SCHEMA)
    issues = []
    for number, issue in enumerate(answer.get("issues") or [], 1):
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        issues.append({"n": number, **issue, "start": round(offset + min(a, b), 2), "end": round(offset + max(a, b), 2),
                       "segments": [seg["id"]], "source": "gemini"})
    for problem in record.get("problems") or []:
        issues.append({"n": len(issues) + 1, "start": round(offset, 2), "end": round(offset + length, 2),
                       "category": "continuity", "severity": "high", "where": "whole frame", "source": "measured",
                       "segments": [seg["id"]], "description": problem})
    moments = every(length, STILL_STEP, fps)
    following = next((s for s in plan_row["segments"]
                      if (RENDER.previous_of(plan_row, s) or {}).get("id") == seg["id"]), None)
    handoff = (following["render_frames"][0] - seg["render_frames"][0]) / fps if following else None
    if handoff is not None and all(abs(handoff - m) > .05 for m in moments):
        moments = sorted(moments + [round(handoff, 3)])
    folder = job / "review/check" / name
    shutil.rmtree(folder, ignore_errors=True)
    stills_paths = []
    for number, u in enumerate(moments, 1):
        t = offset + u
        tag = " (next segment starts here)" if handoff is not None and abs(u - handoff) <= .05 else ""
        stills_paths.append(str(still_pair((source["path"], begin + u, f"ORIGINAL {t:.2f} s{tag}"),
                                           (str(result_path), min(u / stretch, record["ai"]["duration"] - .02),
                                            f"RESTYLED {t:.2f} s{tag}"), size, folder / f"{number:02d}-{t:07.2f}.png")))
    verdict = "fail" if (answer.get("verdict") == "fail" or any(i["severity"] == "high" for i in issues)) else "pass"
    result = {"segment": key, "attempt": record.get("attempt", 1), "generation_id": record["generation_id"],
              "verdict": verdict, "summary": answer.get("summary", ""), "issues": issues, "compare": str(compare),
              "stills": stills_paths, "handoff": None if handoff is None else round(offset + handoff, 2),
              "next": following and f"{source_id}:{following['id']}"}
    write(job / "reports/check" / f"{name}.json", result)
    lines = [f"# Check {key} attempt {result['attempt']}: {verdict}", "", result["summary"], ""]
    lines += [f"- #{i['n']} [{i['severity']}] {i['category']} {i['start']:.2f}-{i['end']:.2f} s ({i.get('where', '')}; "
              f"{i['source']}): {i['description']}" for i in issues]
    lines += ["", f"- stills every {STILL_STEP:g} s (lossless; look at every one): {folder} ({len(stills_paths)} pairs)"]
    if result["next"]:
        lines.append(f"- {result['next']} starts from this result's frame at {result['handoff']:.2f} s")
    write_text(job / "reports/check" / f"{name}.md", "\n".join(lines) + "\n")
    return result
