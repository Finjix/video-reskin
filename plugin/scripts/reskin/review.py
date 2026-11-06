"""Quality review: Gemini watches the original and the restyled video side by side and lists what went wrong;
`frames` cuts stills around a reported time so the agent can confirm an issue before sending a segment back."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import winky
from reskin.analyze import font
from reskin.common import FFMPEG, log, read, run, video_info, write, write_text
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


INLINE_LIMIT = 256_000_000     # bytes: Winky's Gemini rejects a larger inline video part (measured 2026-11-06)
PROXY_CRF = 4                  # review clips are near-lossless: Gemini judges the delivered detail
BUDGET = .93                   # share of INLINE_LIMIT the video bitrate is capped to (container and VBV headroom)
REVIEW_FPS = 24                # frame rate of the clips Gemini gets (the delivery's); it samples them at its own rate
STILL_STEP = 2.0               # seconds between the agent's full-size stills of the whole video
SEAM_WINDOW = 2.0              # seconds on either side of a seam that Gemini watches and the agent's stills cover


def grid_clip(panels: list[dict], size: tuple[int, int], output: Path, crf: int = PROXY_CRF) -> Path:
    """Two panels on one clip, each at the delivery's own `size` (Gemini judges the delivered detail) and labelled,
    side by side for portrait, stacked for landscape. A panel is {"path", "start", "end", "label"}; both share one
    clock from their starts. Encoded at `crf` (near-lossless) with the bitrate capped so the clip uses as much of
    INLINE_LIMIT as it needs and no more; a clip that still overshoots is encoded again with a lower cap."""
    width, height = round(size[0] / 2) * 2, round(size[1] / 2) * 2
    portrait = height >= width
    inputs, chains, lengths = [], [], []
    for panel in panels:
        start, end = max(0.0, float(panel["start"])), float(panel["end"])
        lengths.append(max(1 / REVIEW_FPS, end - start))
        inputs += ["-ss", f"{start:.6f}", "-t", f"{lengths[-1]:.6f}", "-i", str(panel["path"])]
    longest = max(lengths)
    for index, (panel, length) in enumerate(zip(panels, lengths)):
        chains.append(f"[{index}:v]setpts=PTS-STARTPTS,fps={REVIEW_FPS},scale={width}:{height}:flags=lanczos,setsar=1,"
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


def auto_fps(duration: float) -> float:
    """Frames per second Gemini samples: short ads get 4 so flicker and brief morphs are not missed."""
    return 4.0 if duration <= 75 else 2.0


def video_part(path: Path, fps: float) -> dict:
    part = winky.inline(path, "video/mp4")
    part["videoMetadata"] = {"fps": fps}
    return part


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
    import shutil
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


SEAM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "consistent": {"type": "BOOLEAN"},
        "severity": {"type": "STRING", "enum": ["high", "medium", "low", "none"]},
        "changed": {"type": "ARRAY", "items": {"type": "STRING"}},
        "description": {"type": "STRING"}},
    "required": ["consistent", "severity", "changed", "description"]}


def seam_prompt(seam: dict, layout: str, switch: float, length: float) -> str:
    other = "right" if layout == "left" else "bottom"
    base = (f"This {length:.1f}-second clip shows the ORIGINAL game video on the {layout} and an AI-restyled version "
            f"on the {other}, frame-synchronised, at the video's own frame rate. At {switch:.2f} s two separately "
            "generated parts of the restyled video meet. ")
    if seam["seam"] == "continuous":
        task = ("In the original this is one continuous shot: nothing changes at that moment. Look only at the restyled "
                "side, frame by frame around it, and decide whether a viewer could notice the join: a jump; a dissolve "
                "or a gradual change over a second or so; a step in colour, brightness, contrast, sharpness or "
                "saturation; any change of terrain, ground, water, rocks, trees, objects, textures, lighting or "
                "realism; an object that pops, morphs or vanishes; a double image. consistent = false for any such "
                "change.")
    else:
        task = ("The original has a camera cut there, so the content may change. Decide only whether the restyled look "
                "(colour grading, brightness, materials, realism level, the same objects and characters rendered the "
                "same way) stays consistent across the cut as it does in the original. consistent = false if the look "
                "visibly changes.")
    return (base + task + " List what changed in `changed` and write `description` in Chinese; severity none when "
            "consistent, high when a casual viewer notices it at once, medium when noticeable on a closer look, low "
            "when minor.")


def seam_moments(switch: float, fps: float, duration: float, blend: int = 3) -> list[tuple[float, str]]:
    """The agent's stills of a seam: -2, -1 and -0.5 s, every frame from two before the blend to two after it, then
    +0.5, +1 and +2 s (delivered clock, label)."""
    frame = 1 / fps
    first = switch - (blend / 2) * frame
    wanted = {}
    for step in (-SEAM_WINDOW, -1.0, -.5, .5, 1.0, SEAM_WINDOW):
        wanted[round(switch + step, 4)] = f"{step:+.1f} s"
    for k in range(-2, blend + 2):
        t = round(first + k * frame, 4)
        wanted[t] = ("before the blend" if k < 0 else "after the blend" if k >= blend else f"blend frame {k + 1}")
    last = duration - frame
    return [(t, label) for t, label in sorted(wanted.items()) if 0 <= t <= last]


def seam_checks(job: Path, state: dict, source_id: str, assembled: dict, layout: str) -> list[dict]:
    """Every seam: Gemini watches SEAM_WINDOW s on either side at full size and frame rate; the agent gets full-size
    stills (seam_moments) in review/seams/<id>-<Kxx>/."""
    import shutil
    source = next(r for r in state["sources"] if r["id"] == source_id)
    size = tuple(assembled["size"])
    fps, duration, keep_start = assembled["fps"], assembled["duration"], assembled["keep"][0]
    found = []
    for seam in assembled["seams"]:
        switch = seam["time"]
        a, b = max(0.0, switch - SEAM_WINDOW), min(duration, switch + SEAM_WINDOW)
        here = job / "review/seams" / f"{source_id}-{seam['segment']}"
        shutil.rmtree(here, ignore_errors=True)
        stills = [str(still_pair((source["path"], keep_start + t, f"ORIGINAL seam {seam['segment']} {label} ({t:.3f} s)"),
                                 (assembled["output"], t, f"RESTYLED seam {seam['segment']} {label} ({t:.3f} s)"),
                                 size, here / f"{number:02d}-{t:08.3f}.png"))
                  for number, (t, label) in enumerate(seam_moments(switch, fps, duration), 1)]
        clip = grid_clip([{"path": source["path"], "start": keep_start + a, "end": keep_start + b, "label": "ORIGINAL"},
                          {"path": assembled["output"], "start": a, "end": b, "label": "RESTYLED"}], size, here / "seam.mp4")
        entry = {**seam, "window": [round(a, 3), round(b, 3)], "stills": stills, "answer": None}
        try:
            entry["answer"] = winky.gemini([video_part(clip, fps), {"text": seam_prompt(seam, layout, switch - a, b - a)}],
                                           schema=SEAM_SCHEMA)
            answer = entry["answer"]
            log(f"{source_id}: seam {seam['segment']} at {switch:.2f} s: " + ("consistent" if answer.get("consistent")
                else f"{answer.get('severity')} ({', '.join(answer.get('changed') or [])})"))
        except (RuntimeError, ValueError) as error:
            entry["error"] = str(error)
            log(f"{source_id}: Gemini seam check {seam['segment']} failed ({error}); the stills are in {here}")
        found.append(entry)
    return found


def prompt(state: dict, plan_row: dict, assembled: dict, layout: str) -> str:
    other = "right" if layout == "left" else "bottom"
    seams = ", ".join(f"{s['time']:.2f} s ({'hard cut' if s['seam'] == 'cut' else 'inside a shot'})"
                      for s in assembled["seams"]) or "none"
    removal = describe(plan_row["remove"]) or "nothing named"
    style = state["style"] if isinstance(state["style"], str) else state["style"].get("label")
    lines = [
        "You are a strict quality reviewer for AI-restyled game advertising videos.",
        f"The video shows the ORIGINAL on the {layout} and the RESTYLED version on the {other}, frame-synchronised "
        f"(same timestamps). The restyle goal: {describe(plan_row['materials'])} — game-rendered cartoon materials replaced by "
        f"realistic real-world materials (style preset: {style}) while composition, camera motion, characters, object "
        "positions, counts and timing stay exactly as in the original. HUD numbers, icons and buttons must stay.",
        *([f"Restyled side, target {look[0].lower()}{look[1:]}"] if (look := job_style(state).get("look")) else []),
        f"Removed on purpose (must NOT appear on the restyled side): {removal}. The closing card was cut off on purpose.",
        f"The restyled video was rendered in segments and joined at: {seams}. Check these moments closely for jumps.",
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
    """Gemini watches the whole delivery beside the original (full size, crf 18, inline up to 256 MB) and every seam
    up close; the agent gets full-size lossless stills of the whole video and of every seam to judge for itself."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    clip, layout = proxy(job, state, source_id, assembled)
    size = clip.stat().st_size
    if size > INLINE_LIMIT:
        raise RuntimeError(f"{source_id}: review clip is {size / 1e6:.0f} MB even at crf 36, above the inline limit")
    fps = fps or auto_fps(assembled["duration"])
    log(f"{source_id}: asking Gemini to review ({size / 1e6:.1f} MB comparison clip, {fps:g} frames/s)")
    answer = winky.gemini([video_part(clip, fps), {"text": prompt(state, plan_row, assembled, layout)}], schema=SCHEMA)
    issues = []
    for number, issue in enumerate(answer.get("issues") or [], 1):
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        a, b = min(a, b), max(a, b)
        issues.append({"n": number, **issue, "start": round(a, 2), "end": round(b, 2),
                       "segments": segment_for(a, b, assembled), "source": "gemini"})
    for seam in assembled.get("seams") or []:
        if seam.get("verdict") == "visible":
            issues.append({"n": len(issues) + 1, "start": round(seam["time"] - .25, 2), "end": round(seam["time"] + .25, 2),
                           "category": "seam_color", "severity": "high", "where": "whole frame", "source": "measured",
                           "description": f"段内接缝 {seam['segment']} 处前后两段画面不一致（差异 {seam['difference']}，"
                                          f"正常帧间变化 {seam['motion']}，最大区域差异 {seam['local']}），切换时可见跳变或叠影",
                           "segments": [seam["segment"]]})
    for seam in assembled.get("colour") or []:
        if seam["residual"] > 6:
            when = next((s["time"] for s in assembled["seams"] if s["segment"] == seam["before"]), 0.0)
            issues.append({"n": len(issues) + 1, "start": when, "end": when, "category": "seam_color",
                           "severity": "medium", "where": "whole frame", "source": "measured",
                           "description": f"接缝 {seam['before']} 处颜色校正后仍差 {seam['residual']}（0-255）",
                           "segments": [seam["before"]]})
    seams = seam_checks(job, state, source_id, assembled, layout)
    for seam in seams:
        answer_seam = seam.get("answer") or {}
        if answer_seam and not answer_seam.get("consistent") and answer_seam.get("severity") not in (None, "none"):
            issues.append({"n": len(issues) + 1, "start": seam["window"][0], "end": seam["window"][1],
                           "category": "seam_color", "severity": answer_seam["severity"], "where": "seam close-up",
                           "source": "gemini-seam", "description": answer_seam.get("description", ""),
                           "segments": [seam["segment"]]})
    rounds_path = job / "state/review" / f"{source_id}.json"
    history = read(rounds_path) if rounds_path.is_file() else {"rounds": []}
    number = len(history["rounds"]) + 1
    stills = overview_stills(job, state, source_id, assembled, number)
    verdict = "fail" if (answer.get("verdict") == "fail" or any(i["severity"] == "high" for i in issues)) else "pass"
    result = {"source": source_id, "round": number, "output": assembled["output"], "verdict": verdict,
              "summary": answer.get("summary", ""), "issues": issues, "compare": str(clip),
              "stills": stills, "seams": [{k: v for k, v in seam.items() if k != "stills"} | {"stills": seam["stills"]}
                                          for seam in seams]}
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
    lines += ["", "## Agent stills (full size, lossless; look at every one)", ""]
    if result.get("stills"):
        lines.append(f"- whole video every {STILL_STEP:g} s: {Path(result['stills'][0]).parent} ({len(result['stills'])} pairs)")
    for seam in result.get("seams") or []:
        answer = seam.get("answer") or {}
        verdict = ("consistent" if answer.get("consistent") else f"{answer.get('severity')}: {answer.get('description', '')}"
                   ) if answer else f"no Gemini answer ({seam.get('error', '')})"
        lines.append(f"- seam {seam['segment']} at {seam['time']:.2f} s ({seam['seam']}"
                     + (f", measured {seam['verdict']}" if seam.get("verdict") else "") + f"; Gemini {verdict}): "
                     f"{Path(seam['stills'][0]).parent if seam['stills'] else '-'} ({len(seam['stills'])} pairs)")
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
                raw = run([FFMPEG, "-v", "error", "-ss", f"{moment + offset:.3f}", "-i", path, "-frames:v", "1",
                           "-vf", f"scale={tile_w}:{tile_h}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          binary=True)
                if len(raw) >= tile_w * tile_h * 3:
                    canvas.paste(Image.frombytes("RGB", (tile_w, tile_h), raw[: tile_w * tile_h * 3]),
                                 (column * tile_w, row * (tile_h + 26) + 26))
                draw.text((column * tile_w + 4, row * (tile_h + 26) + 4), f"{name} {moment:.2f} s", fill="black", font=label)
        out = job / "review/frames" / f"{source_id}-{t:07.2f}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, compress_level=1)
        outputs.append(out)
    return outputs
