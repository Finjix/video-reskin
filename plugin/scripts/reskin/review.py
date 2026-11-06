"""Quality review: Gemini watches the original and the restyled video side by side and lists what went wrong;
`frames` cuts stills around a reported time so the agent can confirm an issue before sending a segment back."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

from reskin import ledger, winky
from reskin.analyze import font
from reskin.common import FFMPEG, digest, log, read, run, video_info, write, write_text
from reskin.prompts import describe

CATEGORIES = {
    "continuity": "continuity error: the restyled side shows something the original does not (cartoon material left over, an object or "
                  "character changing identity, shape, colour or count, things appearing or vanishing, motion that no "
                  "longer matches)",
    "artifact": "bug: morphing, melting, warping, flicker, jitter, frozen or repeated frames, garbled UI text or numbers, "
                "broken limbs, smeared detail, strong noise or a fragmented look",
    "added_text": "text the original does not have at that place and time: captions, warning lines, watermarks or "
                  "garbled characters drawn "
                  "anywhere (look along the bottom and top edges in particular)",
    "seam_color": "seam: a sudden jump in colour, brightness, contrast, texture or realism between consecutive moments, "
                  "especially at the listed seam times",
    "leftover_overlay": "an element listed as removed on purpose is still visible (game logos and warning lines "
                        "are kept unless listed there)",
    "end_card": "closing-card content (big logo, store badges, call to action) is visible",
    "style_drift": "the realism level or overall look changes from one part of the video to another",
    "identity": "a recurring character or object looks different in another part of the video (clothes or their "
                "colours, headwear, body proportions, face, species, material, realism level) while the original keeps "
                "it the same: compare across the whole clip, the first seconds with the last, not only neighbouring "
                "moments; a gradual drift counts",
    "not_realistic": "a large area of the restyled side kept the original's flat cartoon rendering (outlines, flat colour "
                     "fills, plastic or clay surfaces) instead of real materials; shapes and proportions that stay "
                     "as in the original are intended and do not count (name the areas)",
}
REALISM_SCORES = ("5 = looks like real camera footage throughout; 4 = real, with small game-like traces; 3 = mixed: "
                  "prominent objects still look like a game; 2 = mostly a game or CG look with real textures; "
                  "1 = cartoon")
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
            "required": ["start", "end", "category", "severity", "description"]}},
        "realism": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
            "score": {"type": "INTEGER"},       # 1-5 (Gemini takes only string enums)
            "cartoonish": {"type": "ARRAY", "items": {"type": "STRING"}}},
            "required": ["start", "end", "score", "cartoonish"]}}},
    "required": ["verdict", "summary", "issues", "realism"]}
LABEL_FONT = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/arial.ttf"


def _drawtext(text: str) -> str:
    if not LABEL_FONT.is_file():
        return ""
    path = str(LABEL_FONT).replace("\\", "/").replace(":", "\\:")
    return (f",drawtext=fontfile='{path}':text='{text}':x=12:y=12:fontsize=28:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=6")


INLINE_LIMIT = 256_000_000     # bytes: Winky's Gemini rejects a larger inline video part (measured 2026-11-06)
PROXY_CRF = 18                 # review proxy quality; raised step by step only when the clip would not fit inline


REVIEW_FPS = 24               # frame rate of the clips Gemini gets (the delivery's); it samples them at its own rate


def grid_clip(panels: list[dict], size: tuple[int, int], output: Path, crf: int = PROXY_CRF) -> Path:
    """Panels on one clip, each at the delivery's own `size` (Gemini judges the delivered detail) and labelled: two
    panels stacked (top/bottom for landscape, left/right for portrait), four as a 2 x 2 grid (rows: panels 1-2, 3-4).
    A panel is {"path", "start" (seconds), "end" (None: to its end), "label"}; all share one clock from their starts,
    a shorter one holding its last frame. `crf` goes up only when the clip would exceed INLINE_LIMIT."""
    width, height = round(size[0] / 2) * 2, round(size[1] / 2) * 2
    portrait = height >= width
    lengths = []
    inputs = []
    for panel in panels:
        start = max(0.0, float(panel.get("start") or 0.0))
        end = panel.get("end")
        length = (float(end) - start) if end is not None else video_info(Path(panel["path"]))["duration"] - start
        lengths.append(max(1 / REVIEW_FPS, length))
        inputs += ["-ss", f"{start:.6f}", "-t", f"{lengths[-1]:.6f}", "-i", str(panel["path"])]
    longest = max(lengths)
    chains = []
    for index, (panel, length) in enumerate(zip(panels, lengths)):
        chains.append(f"[{index}:v]setpts=PTS-STARTPTS,fps={REVIEW_FPS},scale={width}:{height}:flags=lanczos,setsar=1,"
                      f"tpad=stop_mode=clone:stop_duration={longest - length + .2:.3f},trim=duration={longest:.6f}"
                      f"{_drawtext(panel.get('label', ''))}[p{index}]")
    if len(panels) == 2:
        stack = f"[p0][p1]{'hstack' if portrait else 'vstack'}=inputs=2"
    elif len(panels) == 4:
        stack = "[p0][p1][p2][p3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0"
    else:
        raise ValueError("a review clip has 2 or 4 panels")
    graph = ";".join(chains) + f";{stack},format=yuv420p[v]"
    output.parent.mkdir(parents=True, exist_ok=True)
    while True:
        run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-an",
             "-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-r", str(REVIEW_FPS), output])
        if output.stat().st_size <= INLINE_LIMIT or crf >= 36:
            return output
        crf += 4


def side_by_side(source_path: str, start: float, end: float, result_path: str, size: tuple[int, int],
                 output: Path, crf: int = PROXY_CRF) -> tuple[Path, str]:
    """Original (left or top) and restyled (right or bottom) on one clip, the same clock for both: the source from
    `start` to `end`, the result from its first frame, each at the delivery's size (grid_clip)."""
    portrait = size[1] >= size[0]
    grid_clip([{"path": source_path, "start": start, "end": end, "label": "ORIGINAL"},
               {"path": result_path, "start": 0.0, "end": end - start, "label": "RESTYLED"}], size, output, crf)
    return output, ("left" if portrait else "top")


def video_part(path: Path, fps: float) -> dict:
    """An inline video part Gemini samples at `fps` (frames per second it reads, each at the HIGH media
    resolution)."""
    part = winky.inline(path, "video/mp4")
    part["videoMetadata"] = {"fps": fps}
    return part


def every(length: float, step: float, fps: float) -> list[float]:
    """Moments from 0 every `step` seconds over a clip of `length` seconds, its last frame included (when it lies
    more than a third of a step after the last moment)."""
    last = max(0.0, length - 1 / fps)
    moments = [round(i * step, 3) for i in range(int(last / step) + 1)]
    if last - moments[-1] > step / 3:
        moments.append(round(last, 3))
    return moments


def still_pair(upper: tuple[str, float, str], lower: tuple[str, float, str], size: tuple[int, int],
               output: Path) -> Path:
    """Two frames at the delivery's full size, each (path, seconds, label): stacked for a landscape video, side by
    side for a portrait one, labelled above each; JPEG quality 95 (the agent judges detail on them)."""
    from reskin.common import font as _font
    width, height = size
    portrait = height >= width
    band = 34
    pictures = [(frame_at(path, t, width, height), label) for path, t, label in (upper, lower)]
    canvas = Image.new("RGB", ((2 * width if portrait else width), (height + band) * (1 if portrait else 2)), "white")
    draw = ImageDraw.Draw(canvas)
    text = _font("regular", 24)
    for number, (picture, label) in enumerate(pictures):
        x, y = (number * width, 0) if portrait else (0, number * (height + band))
        draw.text((x + 8, y + 4), label, fill="black", font=text)
        canvas.paste(picture, (x, y + band))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=95)
    return output


def proxy(job: Path, state: dict, source_id: str, assembled: dict) -> tuple[Path, str]:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    start, end = assembled["keep"]
    return side_by_side(source["path"], start, end, assembled["output"], tuple(assembled["size"]),
                        job / "review" / f"{source_id}-compare.mp4")


def accepted_notes(job: Path, source_id: str) -> list[str]:
    """Findings of segment checks the editor looked at and let through (`accept`), with the reason: later checks and
    the review are told about them, so the same accepted flaw does not hold every following segment again."""
    notes = []
    for path in sorted((job / "state/render").glob(f"{source_id}-*.json")):
        gate = read(path).get("gate") or {}
        if not gate.get("accepted"):
            continue
        for issue in gate.get("issues") or []:
            if issue.get("severity") in ("high", "medium") and issue.get("description"):
                notes.append(f"{issue['description']} (accepted: {gate.get('note') or 'minor'})")
    return list(dict.fromkeys(notes))[:12]


def _known(notes: list[str] | None) -> list[str]:
    if not notes:
        return []
    return ["Already seen and accepted by the editor (do NOT report these again, also not where the same thing "
            "continues in this clip):", *[f"- {note}" for note in notes]]


def ui_listing(items) -> str:
    """The `ui` items for a review prompt, the animated ones marked (a little blur or ghosting is tolerated on them
    while they move; static text and logos must stay crisp)."""
    parts = []
    for item in items or []:
        text = describe([item])
        parts.append(text + (" [animated]" if isinstance(item, dict) and item.get("dynamic") else " [static]"))
    return "; ".join(parts)


UI_RULE = ("Static elements (text, logos, warning lines, labels that stay still) must be crisp: any blur, soft or "
           "haloed edge, garbled or changed character, doubling, restyle or move is high. Animated elements (counting "
           "digits, popping, zooming or flying text, HUD effects) may blur or trail a little while they move (low at "
           "most); garbled characters or wrong digits are still defects.")


def _goal(state: dict, plan_row: dict, layout: str, what: str) -> list[str]:
    other = "right" if layout == "left" else "bottom"
    removal = describe(plan_row["remove"]) or "nothing named"
    ui = ui_listing(plan_row.get("ui") or [])
    style = state["style"] if isinstance(state["style"], str) else state["style"].get("label")
    return [
        "You are a strict quality reviewer for AI-restyled game advertising videos.",
        f"The {what} shows the ORIGINAL on the {layout} and the RESTYLED version on the {other}, frame-synchronised "
        f"(same timestamps). The restyle goal: {describe(plan_row['materials'])} — game-rendered cartoon materials replaced by "
        f"realistic real-world materials (style preset: {style}) while composition, camera motion, characters, object "
        "positions, counts and timing stay exactly as in the original. HUD numbers, icons, buttons, game logos and "
        "warning lines must stay as in the original.",
        f"Removed on purpose (must NOT appear on the restyled side): {removal}. The closing card was cut off on purpose.",
        *([f"Screen-fixed interface and text the models must draw exactly as in the original (same characters, "
           f"digits, place, size and colours; report any garbled, missing, doubled, restyled or changed one as "
           f"added_text or artifact): {ui}. {UI_RULE}"] if ui else []),
    ]


def _rules() -> list[str]:
    return [
        "Report every defect on the RESTYLED side, using the original only as the reference for what should be there. "
        "Do not report flaws that the original shows identically (game physics, clipping already in the original), "
        "and do not report the intended material change itself.",
        "The goal is realistic MATERIALS with the original's shapes, proportions and character designs kept (stylised "
        "or chibi figures stay stylised, with real cloth, skin and hair). What matters most is a stable video: no "
        "jumps, dissolves or changes at the seam times, no flicker, morphing, vanishing or added objects, no leftover "
        "or garbled text, no blurred or smeared patches. Report not_realistic only where a large area still shows the "
        "original's flat cartoon rendering (medium at most). HUD, icons, price tags, bubbles and other UI are exempt.",
        f"Also rate the realism over time in `realism`: consecutive ranges covering the whole clip, each at most "
        f"10 seconds long (rate each range on its own: realism often fades later in a clip); score {REALISM_SCORES}. `cartoonish` lists (in Chinese) the objects that still "
        "look game-like in that range (empty at 5).",
        "Categories:",
        *[f"- {name}: {text}" for name, text in CATEGORIES.items()],
        "Severity: high = a casual viewer notices it at once; medium = noticeable when watching attentively; low = minor.",
        "Times are seconds from the start of this clip; give start and end of each problem (end >= start). "
        "`where` names the screen area. Write `description` and `summary` in Chinese. "
        "verdict = fail if there is any high issue or two or more medium issues, else pass.",
    ]


def live_spans(assembled: dict) -> list[tuple[float, float]]:
    """Live-action stretches on the delivered clock: the original's own frames, never restyled."""
    return [(s["start"], s["end"]) for s in assembled.get("segments") or [] if s.get("live")]


def inside_live(assembled: dict, start: float, end: float) -> bool:
    return any(a - .15 <= start and end <= b + .15 for a, b in live_spans(assembled))


def prompt(state: dict, plan_row: dict, assembled: dict, layout: str, known: list[str] | None = None) -> str:
    seams = ", ".join(f"{s['time']:.2f} s ({'hard cut' if s['seam'] == 'cut' else 'inside a shot'})"
                      for s in assembled["seams"]) or "none"
    live = live_spans(assembled)
    lines = [
        *_goal(state, plan_row, layout, "video"),
        f"The restyled video was rendered in segments and joined at: {seams}. Check these moments closely: inside a "
        "shot the restyled scene must continue unchanged (same terrain, objects, rope and cable colours, textures, "
        "light); any change, dissolve or double image there is a seam_color defect.",
        "Each segment was already checked on its own; watch above all for what only the whole video shows: the look, "
        "the scenery and every recurring character or object changing from one part to another (the first seconds "
        "against the last), drifts across camera cuts, the end card, and added text.",
        *([f"These stretches are live-action footage, not the game, delivered unchanged from the original on purpose: "
           f"{', '.join(f'{a:.2f}-{b:.2f} s' for a, b in live)}. Both sides show the same there; do not report them "
           "as unchanged, not restyled or not realistic, and leave them out of `realism`."] if live else []),
        *_rules(), *_known(known),
    ]
    return "\n".join(lines)


def segment_prompt(state: dict, plan_row: dict, layout: str, known: list[str] | None = None) -> str:
    """For the check right after one segment renders (before the next segment of its shot starts from it)."""
    lines = [*_goal(state, plan_row, layout, "clip of one segment"),
             "This is one separately rendered segment, checked before the rest of the shot is rendered from it: look "
             "hardest for wrong or vanished objects, changed colours of prominent objects, garbled or added text, "
             "leftover removed elements, blurred patches, sudden changes and morphing. Use high only for what "
             "a casual viewer notices at once.", *_rules(), *_known(known)]
    return "\n".join(lines)


def realism_ranges(answer: dict, offset: float = 0.0) -> list[dict]:
    """Gemini's realism timeline, times shifted by `offset`."""
    ranges = []
    for item in answer.get("realism") or []:
        a, b = sorted((float(item.get("start", 0)), float(item.get("end", item.get("start", 0)))))
        score = min(5, max(1, int(item.get("score") or 5)))
        ranges.append({"start": round(a + offset, 2), "end": round(b + offset, 2), "score": score,
                       "cartoonish": [str(x) for x in item.get("cartoonish") or []]})
    return ranges


REALISM_ISSUE = 2       # a stretch rated this or lower becomes a (medium) not_realistic issue; 3 is only reported
#                         (the 1080p batch chased realism with redos, midframes and identity checks and paid for it in
#                         seams and renders)


def realism_issues(ranges: list[dict], issues: list[dict]) -> list[dict]:
    """A stretch rated REALISM_ISSUE or lower that no not_realistic issue covers becomes a medium one: the video kept
    the game look there. Neighbouring ranges with the same score make one issue (the timeline keeps the detail)."""
    merged = []
    for item in sorted(ranges, key=lambda r: r["start"]):
        last = merged[-1] if merged else None
        if last and last["score"] == item["score"] and item["start"] - last["end"] <= .5:
            last["end"] = max(last["end"], item["end"])
            last["cartoonish"] = list(dict.fromkeys(last["cartoonish"] + item["cartoonish"]))
        else:
            merged.append(dict(item, cartoonish=list(item["cartoonish"])))
    found = []
    for item in merged:
        if item["score"] > REALISM_ISSUE or item["end"] - item["start"] < .5:
            continue
        if any(i["category"] == "not_realistic" and i["start"] <= item["end"] and item["start"] <= i["end"]
               for i in issues):
            continue
        objects = "、".join(item["cartoonish"][:6]) or "画面"
        found.append({"start": item["start"], "end": item["end"], "category": "not_realistic",
                      "severity": "medium", "where": objects,
                      "description": f"写实度 {item['score']}/5：{objects}仍有游戏/卡通感"})
    return found


def realism_line(ranges: list[dict]) -> str:
    """`realism: 0.0-12.5 s 4/5; 12.5-22.9 s 3/5 (雪地, 顾客)`"""
    return "; ".join(f"{r['start']:.1f}-{r['end']:.1f} s {r['score']}/5"
                     + (f" ({', '.join(r['cartoonish'])})" if r["cartoonish"] else "") for r in ranges) or "not rated"


# Identity: the same character or object must look the same from the first second to the last. Watching the video
# compares neighbouring moments with the original; a slow drift (a realistic adult in a dark parka turning, over a
# segment, back into the game's light-blue chibi) passes that, and every later segment of the shot inherits it. And
# on a sheet of whole frames the characters are a few pixels tall. So: Gemini first finds the recurring subjects on
# the ORIGINAL frames (the protagonist always; same composition, so the boxes hold for the restyle too), the program
# cuts close-ups of each subject from every moment and from the styled keyframes that set the look, and Gemini then
# compares the close-ups (and the whole frames), the original above each so intended changes can be told apart.
DETECT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "subjects": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "description": {"type": "STRING"}}, "required": ["name", "description"]}},
        "frames": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "index": {"type": "INTEGER"},
            "boxes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "subject": {"type": "STRING"},
                "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
                "required": ["subject", "box_2d"]}}},
            "required": ["index", "boxes"]}}},
    "required": ["subjects", "frames"]}
IDENTITY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"subjects": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "what": {"type": "STRING"},
        "reference_look": {"type": "STRING"},
        "columns": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "label": {"type": "STRING"},
            "difference": {"type": "STRING", "enum": ["none", "low", "medium", "high"]},
            "look": {"type": "STRING"}},
            "required": ["label", "difference", "look"]}}},
        "required": ["what", "reference_look", "columns"]}}},
    "required": ["subjects"]}
SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
IDENTITY_STILLS = 18     # moments of the restyle per identity check at most
SUBJECTS = 4             # recurring subjects followed in close-up at most (the protagonist first)
CLOSEUPS = 12            # close-ups per subject at most (references always kept)
FRAME_SIDE = 960         # long side of the frames the boxes are found on and cut from (at most the result's own)



def identity_times(start: float, end: float, step: float = 3.5, limit: int = IDENTITY_STILLS) -> list[float]:
    """Moments (on the result's clock) spread over [start, end): every `step` s, at most `limit`."""
    length = max(0.0, end - start - .4)
    count = max(2, min(limit, int(length / step) + 1))
    return [round(start + .2 + i * length / (count - 1), 2) for i in range(count)]


def _still(path, t: float, width: int, height: int) -> Image.Image | None:
    raw = run([FFMPEG, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
               "-vf", f"scale={width}:{height}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
    return Image.frombytes("RGB", (width, height), raw[: width * height * 3]) if len(raw) >= width * height * 3 else None


def _fit(size: tuple[int, int], side: int = FRAME_SIDE) -> tuple[int, int]:
    width, height = size
    scale = side / max(width, height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def identity_frames(references: list[tuple[str, Path, Path]], size: tuple[int, int], *,
                    source_path: str | None = None, source_offset: float = 0.0, result_path: str | None = None,
                    times: list[float] | None = None, stills: list[tuple[str, Path, Path]] | None = None) -> list[dict]:
    """The moments to compare: references (styled keyframes: label, original frame, styled), then either moments of
    a result video (`times` on its clock; original at t + source_offset) or ready pictures (`stills`: label, original,
    styled), each as {"label", "t", "reference", "original": image, "restyled": image}."""
    box = _fit(size, min(FRAME_SIDE, max(size)))
    frames = []

    def picture(label, t, src, styled, reference):
        if Path(src).is_file() and Path(styled).is_file():
            with Image.open(src) as a, Image.open(styled) as b:
                frames.append({"label": label, "t": t, "reference": reference,
                               "original": a.convert("RGB").resize(box, Image.LANCZOS),
                               "restyled": b.convert("RGB").resize(box, Image.LANCZOS)})
    for label, src, styled in references:
        picture(f"REF {label}", None, src, styled, True)
    for label, src, styled in stills or []:
        picture(label, _seconds(label), src, styled, False)
    for t in times or []:
        upper = _still(source_path, t + source_offset, *box)
        lower = _still(result_path, t, *box)
        if upper is not None and lower is not None:
            frames.append({"label": f"{t:.1f} s", "t": t, "reference": False, "original": upper, "restyled": lower})
    return frames


def _seconds(label: str) -> float | None:
    try:
        return float(label.split()[0])
    except (ValueError, IndexError):
        return None


def _pairs_sheet(columns: list[tuple[str, Image.Image, Image.Image, bool]], tile: tuple[int, int], per_row: int,
                 title: str, sections: list[tuple[str, int]] | None = None) -> Image.Image:
    """Columns of (label, upper, lower, reference) in rows of `per_row`; `sections`: (heading, column count) groups
    that each start on a new row under their heading."""
    from reskin.common import font as _font
    label = _font("regular", 15)
    groups = sections or [("", len(columns))]
    layout, index = [], 0
    for heading, count in groups:
        layout.append((heading, columns[index:index + count]))
        index += count
    cell_h = 20 + 2 * tile[1] + 4
    rows = sum(max(1, -(-len(cols) // per_row)) for _, cols in layout)
    headings = sum(1 for heading, _ in layout if heading)
    canvas = Image.new("RGB", (per_row * tile[0], 22 + headings * 24 + rows * cell_h), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((4, 3), title, fill="black", font=label)
    y = 22
    for heading, cols in layout:
        if heading:
            draw.rectangle([0, y, canvas.width, y + 22], fill=(225, 225, 225))
            draw.text((4, y + 3), heading, fill="black", font=label)
            y += 24
        for number, (text, upper, lower, reference) in enumerate(cols):
            x = (number % per_row) * tile[0]
            top = y + (number // per_row) * cell_h
            draw.text((x + 3, top + 2), text, fill=(180, 0, 0) if reference else "black", font=label)
            canvas.paste(upper.resize(tile, Image.LANCZOS), (x, top + 20))
            canvas.paste(lower.resize(tile, Image.LANCZOS), (x, top + 20 + tile[1]))
        y += max(1, -(-len(cols) // per_row)) * cell_h
    return canvas


def overview_sheet(frames: list[dict], size: tuple[int, int], output: Path) -> Path:
    portrait = size[1] >= size[0]
    tile = (150, round(150 * size[1] / size[0])) if portrait else (240, round(240 * size[1] / size[0]))
    canvas = _pairs_sheet([(f["label"], f["original"], f["restyled"], f["reference"]) for f in frames], tile,
                          10 if portrait else 6, "upper: ORIGINAL game | lower: RESTYLED (REF: styled keyframes that "
                                                 "set the look)")
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=88)
    return output


def detect_prompt(count: int, named: list[dict] | None = None) -> str:
    if named:
        listing = "; ".join(f"{s['id']}: {s['what']}" for s in named[:SUBJECTS])
        return (f"These are {count} frames of one game video, each after its number. Follow exactly these subjects, "
                f"whose look must stay the same through the video: {listing}. Use each id as `name` and describe the "
                "subject in Chinese in `description`. For every frame list the box of each subject visible in it: "
                "box_2d = [ymin, xmin, ymax, xmax], normalised to 0-1000 on that frame. Leave a subject out of a frame "
                "it is not in.")
    return (f"These are {count} frames of one game video, each after its number. Pick the recurring subjects whose "
            "look must stay the same through the video: always the player character (the one the player controls: "
            f"it walks around, fishes, carries and sells things), and up to {SUBJECTS - 1} more that show in several "
            "frames (e.g. the customers of a queue, a vehicle, a machine, an animal). Name each in Chinese in `name` "
            "(e.g. the player character, the queueing customers) and describe it in `description`. For every frame list "
            "the box of each "
            "subject visible in it: box_2d = [ymin, xmin, ymax, xmax], normalised to 0-1000 on that frame; for a "
            "group (a queue) box the nearest two or three members. Leave a subject out of a frame it is not in.")


def detect(job: Path, key: str, frames: list[dict], named: list[dict] | None = None) -> dict:
    """Gemini finds the recurring subjects on the original frames (the edits' `subjects` when there are any):
    {"subjects": [...], "boxes": {index: {name: box}}}."""
    import io
    parts = []
    for number, frame in enumerate(frames, 1):
        buffer = io.BytesIO()
        frame["original"].save(buffer, "JPEG", quality=85)
        parts.append({"text": f"Frame {number}:"})
        parts.append(winky.inline_bytes(buffer.getvalue(), "image/jpeg"))
    parts.append({"text": detect_prompt(len(frames), named)})
    answer = winky.gemini(parts, schema=DETECT_SCHEMA)
    ledger.add(job, kind="gemini", purpose="identity", key=key, seconds=0)
    names = [s.get("name", "") for s in answer.get("subjects") or [] if s.get("name")][:SUBJECTS]
    boxes: dict[int, dict[str, list[float]]] = {}
    for item in answer.get("frames") or []:
        index = int(item.get("index", 0)) - 1
        if not 0 <= index < len(frames):
            continue
        for found in item.get("boxes") or []:
            box = found.get("box_2d") or []
            if found.get("subject") in names and len(box) == 4:
                y0, x0, y1, x1 = (min(1000, max(0, int(v))) / 1000 for v in box)
                if y1 > y0 and x1 > x0:
                    boxes.setdefault(index, {})[found["subject"]] = [x0, y0, x1, y1]
    return {"subjects": [s for s in answer.get("subjects") or [] if s.get("name") in names], "boxes": boxes}


def closeup(image: Image.Image, box: list[float], side: int = 200) -> Image.Image:
    """The box (fractions x0, y0, x1, y1) widened to a square with some surroundings, as a side x side tile."""
    width, height = image.size
    x0, y0, x1, y1 = box[0] * width, box[1] * height, box[2] * width, box[3] * height
    centre_x, centre_y = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0, y1 - y0, .06 * width) * .8
    crop = image.crop((round(centre_x - half), round(centre_y - half), round(centre_x + half), round(centre_y + half)))
    return crop.resize((side, side), Image.LANCZOS)


BUILDS = ["realistic", "stylized", "chibi", "not_a_figure"]
RENDERINGS = ["photo_real", "game_cg", "cartoon"]
_SIDE = {"type": "OBJECT", "properties": {
    "build": {"type": "STRING", "enum": BUILDS},
    "rendering": {"type": "STRING", "enum": RENDERINGS},
    "colours": {"type": "STRING"}}, "required": ["build", "rendering", "colours"]}
PAIR_SCHEMA = {
    "type": "OBJECT",
    "properties": {"pairs": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "pair": {"type": "INTEGER"},
        "left": _SIDE, "middle": _SIDE,
        "same_colours": {"type": "BOOLEAN"},
        "original_changes_too": {"type": "BOOLEAN"},
        "difference": {"type": "STRING", "enum": ["none", "low", "medium", "high"]},
        "reference_look": {"type": "STRING"},
        "look": {"type": "STRING"}},
        "required": ["pair", "left", "middle", "same_colours", "original_changes_too", "difference",
                     "reference_look", "look"]}}},
    "required": ["pairs"]}
PAIR_PROMPT = (
    "Each pair image shows one subject of an AI-restyled video (a game advertisement turned into realistic, "
    "real-camera footage): LEFT = how the subject must look (its REFERENCE close-up: the styled keyframe, or its first "
    "appearance), MIDDLE = the same subject RESTYLED at a later moment, small RIGHT = the ORIGINAL game (top: at the "
    "reference moment, bottom: at the later moment; only to tell intended changes, such as an upgrade that recolours "
    "a machine). For every pair decide "
    "whether the MIDDLE keeps the look of the LEFT: clothes and their colours (dark navy vs bright light blue, grey "
    "vs white fur), headwear, body build and proportions (realistic adult vs chibi or doll with a big head), face and "
    "beard, species or kind of object, material, and how real it looks (photo vs game figure, plastic or clay). Judge "
    "only LEFT vs MIDDLE; ignore pose, angle, distance, cropping, motion blur and what covers part of the subject; "
    "a change the small RIGHT original makes too is intended (difference none). For LEFT and MIDDLE each fill in: "
    "build (people and animals: realistic = real adult proportions, bulky winter clothes and a view from above "
    "included; stylized = toy-like proportions (oversized head or hands) but not chibi; "
    "chibi = big head, short limbs, doll or figurine; objects: not_a_figure), rendering (photo_real = looks filmed; "
    "game_cg = a 3D game model or CG render; cartoon), colours (the main colours in Chinese, e.g. a navy coat and a "
    "grey fur collar). "
    "same_colours = the MIDDLE keeps the LEFT's colours. original_changes_too = the ORIGINAL bottom differs from the "
    "ORIGINAL top in the same way (then a colour change is intended). difference (your overall verdict): none; "
    "low = small; "
    "medium = clearly other colours, build or realism; high = it looks like another person or object. Describe both "
    "looks briefly in Chinese (`reference_look` for LEFT, `look` for MIDDLE).")


def pair_difference(item: dict) -> str:
    """The verdict on one pair from its attributes, raised to Gemini's own `difference` when that is higher: the
    words a model writes ("矮胖卡通化") and the grade it gives ("low") do not always agree; the attributes do."""
    left, middle = item.get("left") or {}, item.get("middle") or {}
    rank = SEVERITY_RANK.get(item.get("difference", "none"), 0)
    render = {name: i for i, name in enumerate(RENDERINGS)}
    rendering = 0
    if left.get("rendering") in render and middle.get("rendering") in render:
        rendering = render[middle["rendering"]] - render[left["rendering"]]
        rank = max(rank, 3 if rendering >= 2 else 2 if rendering == 1 else 0)
    recoloured = item.get("same_colours") is False and not item.get("original_changes_too")
    if recoloured:
        rank = max(rank, 2)
    build = {name: i for i, name in enumerate(BUILDS[:3])}
    if left.get("build") in build and middle.get("build") in build:
        step = build[middle["build"]] - build[left["build"]]
        # chibi instead of a real adult always counts; one step (a "stocky" heavy parka seen from above was read as
        # stylized) only together with a change in rendering or colours
        rank = max(rank, 3 if step >= 2 else 2 if step == 1 and (rendering > 0 or recoloured) else 1 if step == 1 else 0)
    if item.get("original_changes_too") and SEVERITY_RANK.get(item.get("difference", "none"), 0) == rank:
        rank = min(rank, 1)     # an intended change (an upgrade): only build or rendering still count
    return ["none", "low", "medium", "high"][rank]


def pair_images(frames: list[dict], found: dict, limit: int = CLOSEUPS) -> list[tuple[str, str, bytes]]:
    """Per subject: its reference close-up (the REF column with the largest box, else its first appearance) beside
    each later close-up, the original small on the right: (subject, column label, JPEG bytes)."""
    import io
    from reskin.common import font as _font
    label = _font("regular", 18)
    out = []
    for subject in found["subjects"]:
        name = subject["name"]
        hits = [(f, found["boxes"][i][name]) for i, f in enumerate(frames) if name in found["boxes"].get(i, {})]
        refs = [h for h in hits if h[0]["reference"]]
        if refs:
            ref = max(refs, key=lambda h: (h[1][2] - h[1][0]) * (h[1][3] - h[1][1]))
        elif len(hits) >= 2:
            ref = hits[0]
        else:
            continue
        rest = [h for h in hits if not h[0]["reference"] and h is not ref]
        if len(rest) > limit:
            step = len(rest) / limit
            rest = [rest[int(k * step)] for k in range(limit)]
        left = closeup(ref[0]["restyled"], ref[1], 256)
        left_original = closeup(ref[0]["original"], ref[1], 128)
        for frame, box in rest:
            picture = Image.new("RGB", (640, 282), "white")
            draw = ImageDraw.Draw(picture)
            draw.text((4, 3), f"REFERENCE ({ref[0]['label']})", fill=(180, 0, 0), font=label)
            draw.text((260, 3), f"RESTYLED {frame['label']}", fill="black", font=label)
            draw.text((516, 3), "original", fill="gray", font=label)
            picture.paste(left, (0, 26))
            picture.paste(closeup(frame["restyled"], box, 256), (256, 26))
            picture.paste(left_original, (512, 26))
            picture.paste(closeup(frame["original"], box, 128), (512, 154))
            buffer = io.BytesIO()
            picture.save(buffer, "JPEG", quality=88)
            out.append((name, frame["label"], buffer.getvalue()))
    return out


def compare_pairs(job: Path, key: str, pairs: list[tuple[str, str, bytes]]) -> list[dict]:
    """Gemini judges every pair; returns subjects as {"what", "reference_look", "columns": [...]}."""
    parts = []
    for number, (name, label, data) in enumerate(pairs, 1):
        parts.append({"text": f"Pair {number} ({name}, {label}):"})
        parts.append(winky.inline_bytes(data, "image/jpeg"))
    parts.append({"text": PAIR_PROMPT})
    answer = winky.gemini(parts, schema=PAIR_SCHEMA)
    ledger.add(job, kind="gemini", purpose="identity", key=key, seconds=0)
    subjects: dict[str, dict] = {}
    for item in answer.get("pairs") or []:
        number = int(item.get("pair", 0))
        if not 1 <= number <= len(pairs):
            continue
        name, label, _ = pairs[number - 1]
        subject = subjects.setdefault(name, {"what": name, "reference_look": item.get("reference_look", ""),
                                             "columns": []})
        subject["columns"].append({"label": label, "difference": pair_difference(item),
                                   "gemini": item.get("difference", "none"), "look": item.get("look", ""),
                                   "left": item.get("left"), "middle": item.get("middle"),
                                   "same_colours": item.get("same_colours")})
    return list(subjects.values())


def subjects_sheet(frames: list[dict], found: dict, output: Path) -> Path | None:
    """One section per subject: its close-ups over time (references first), original above, restyled below."""
    columns, sections = [], []
    for subject in found["subjects"]:
        name = subject["name"]
        hits = [(i, f) for i, f in enumerate(frames) if name in found["boxes"].get(i, {})]
        refs = [h for h in hits if h[1]["reference"]]
        rest = [h for h in hits if not h[1]["reference"]]
        if len(rest) > CLOSEUPS:
            step = len(rest) / CLOSEUPS
            rest = [rest[int(k * step)] for k in range(CLOSEUPS)]
        chosen = refs + rest
        if len(chosen) < 2:
            continue
        for index, frame in chosen:
            box = found["boxes"][index][name]
            columns.append((frame["label"], closeup(frame["original"], box), closeup(frame["restyled"], box),
                            frame["reference"]))
        sections.append((f"{name}: {subject.get('description', '')}"[:110], len(chosen)))
    if not columns:
        return None
    canvas = _pairs_sheet(columns, (200, 200), 8, "Close-ups of each recurring subject over time (upper: ORIGINAL, "
                                                   "lower: RESTYLED; REF: styled keyframes that set the look)", sections)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=90)
    return output


def identity_prompt(plan_row: dict, what: str, closeups: bool) -> str:
    sheets = ("The first image shows CLOSE-UPS of each recurring subject (one grey-headed section per subject) over "
              "time; the second the whole frames. " if closeups else "The image shows the whole frames. ")
    return "\n".join([
        f"These sheets show stills of {what}: an AI restyle of a game advertisement into realistic, real-camera "
        f"footage. {sheets}Each column is one moment (its time in seconds on top): the upper picture is the ORIGINAL "
        "game, the lower the RESTYLED video. Columns titled REF come first: the styled keyframes that set the "
        "restyled look (upper: the original frame they were made from).",
        "Task: the restyle must keep one look for every recurring character and object from the first second to the "
        "last. For every subject (the protagonist always; every close-up section; other people, animals and fish, "
        "vehicles, machines, the ground and terrain) write in `reference_look` how it looks in the lower (RESTYLED) "
        "picture of its REF columns: clothes and their colours, body build and proportions (realistic adult or "
        "chibi/doll), face, hair and headwear, species, material and how real it looks. The REF columns are the "
        "truth even when most other columns look different (no REF for it: its first appearance). Then judge EVERY "
        "other column of that subject on its own, the last ones too, in `columns` (label = the column title as "
        "printed, `look` = how it looks there, `difference` from the reference look: none, low = small, medium = "
        "clearly other clothes colour, build or realism, high = a viewer would take it for another person or object, "
        "e.g. a realistic adult in a dark parka vs a light-blue chibi figure). Count as different: other clothes, "
        "colours or headwear, chibi/doll proportions instead of a real adult, another species or kind of object, "
        "cartoon or plastic instead of real. A gradual drift counts as much as a jump; matching the ORIGINAL does "
        "not excuse it (the restyle must match its own REF).",
        "Not a change: what the ORIGINAL (upper pictures) also changes at that moment (an upgrade recolours a machine, "
        "a character changes clothes, objects arrive or leave), a subject seen from another angle or distance, motion "
        "blur, and HUD, icons, price tags and bubbles.",
        f"Intended objects and their looks (edit list): {describe(plan_row['materials'])}.",
        "Write `what`, `reference_look` and `look` in Chinese.",
    ])


def identity_check(job: Path, key: str, frames: list[dict], plan_row: dict, what: str, size: tuple[int, int],
                   stem: Path) -> tuple[list[dict], list[dict], list[Path]]:
    """Find the subjects, cut their close-ups, ask Gemini whether each keeps its look: (subjects as answered, issues
    on the frames' clock, sheets `<stem>-subjects.jpg` and `<stem>-frames.jpg`)."""
    overview = overview_sheet(frames, size, stem.with_name(stem.name + "-frames.jpg"))
    closeups, pairs = None, []
    try:
        found = detect(job, key, frames, plan_row.get("subjects"))
        closeups = subjects_sheet(frames, found, stem.with_name(stem.name + "-subjects.jpg"))
        pairs = pair_images(frames, found)
        write(stem.with_name(stem.name + "-subjects.json"),
              {"subjects": found["subjects"], "labels": [f["label"] for f in frames],
               "boxes": {frames[i]["label"]: b for i, b in found["boxes"].items()}})
    except (RuntimeError, ValueError) as error:
        log(f"{key}: subject boxes failed ({error}); comparing whole frames only")
    sheets = [p for p in (closeups, overview) if p]
    if pairs:
        # One close-up pair (reference | moment | original) per question: on a sheet of many small pictures Gemini
        # took the look most columns share for the reference and missed a protagonist drifting back to the game.
        subjects = compare_pairs(job, key, pairs)
    else:
        answer = winky.gemini([winky.inline(overview, "image/jpeg"),
                               {"text": identity_prompt(plan_row, what, False)}], schema=IDENTITY_SCHEMA)
        ledger.add(job, kind="gemini", purpose="identity", key=key, seconds=0)
        subjects = answer.get("subjects") or []
    return subjects, identity_issues(subjects, [f["t"] for f in frames if f["t"] is not None]), sheets


def identity_issues(subjects: list[dict], times: list[float]) -> list[dict]:
    """Per subject, stretches of columns judged medium or high in difference become one issue each, from the first
    to the last such column (extended to the next judged column's time when it is not different)."""
    issues = []
    for subject in subjects:
        judged = []
        for column in subject.get("columns") or []:
            t = _seconds(str(column.get("label", "")))
            if t is not None and not str(column.get("label", "")).startswith("REF"):
                judged.append((t, column.get("difference", "none"), column.get("look", "")))
        judged.sort()
        stretch = []
        for t, difference, look in judged + [(float("inf"), "none", "")]:
            if SEVERITY_RANK.get(difference, 0) >= 2:
                stretch.append((t, difference, look))
                continue
            if stretch:
                worst = max(stretch, key=lambda item: SEVERITY_RANK[item[1]])
                if len(stretch) >= 3:       # the same change over three moments or more: nobody misses it
                    worst = (worst[0], "high", worst[2])
                looks = "；".join(list(dict.fromkeys(item[2] for item in stretch if item[2]))[:3])
                end = stretch[-1][0]
                issues.append({"start": round(stretch[0][0], 2), "end": round(end, 2), "category": "identity",
                               "severity": worst[1], "where": subject.get("what", ""),
                               "description": f"{subject.get('what', '')}：参考画面（REF）里是"
                                              f"{subject.get('reference_look', '')}，{stretch[0][0]:.1f}-{end:.1f} 秒变成"
                                              f"{looks[:160]}"})
                stretch = []
    return issues


def identity_references(job: Path, source_id: str, plan_row: dict, segment_ids: list[str] | None = None) -> list:
    """The styled keyframes that set the look: the anchor and the keyframe of each shot's first segment (only the
    shots of `segment_ids` when given)."""
    folder = job / "keyframes"
    refs = [(f"anchor {plan_row['anchor']:.1f} s", folder / f"{source_id}-anchor-src.png",
             folder / f"{source_id}-anchor.png")]
    head = None
    for seg in plan_row["segments"]:
        if seg.get("live"):
            continue
        if seg["seam"] != "continuous":
            head = seg
        if segment_ids is None or seg["id"] in segment_ids:
            ref = (f"keyframe {head['frames'][0] / plan_row['fps']:.1f} s", folder / f"{source_id}-{head['id']}-src.png",
                   folder / f"{source_id}-{head['id']}.png")
            if ref not in refs:
                refs.append(ref)
    return refs[:4]


# --- the boxes logos were wiped from ------------------------------------------------------------------------------

WIPE_PROBLEMS = {
    "logo_left": "part of the logo, its letters, outline or glow, or of the warning text is still visible",
    "copied_objects": "objects appear twice or copied in from beside the box (a second fence, lock icon, price tag, "
                      "person, tree), or mirror-symmetric patterns such as a fence forming an X or V",
    "upside_down": "people, animals or objects stand upside down or lie mirrored",
    "smear": "a clearly blurred, smeared or streaky patch the scene around it does not have (a slightly softer "
             "area where the letters were is not a smear)",
    "rectangle": "a visible rectangle, frame or hard edge outlining where the box was",
}
WIPE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"problems": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "sheet": {"type": "INTEGER"}, "column": {"type": "INTEGER"},
        "problem": {"type": "STRING", "enum": sorted(WIPE_PROBLEMS)},
        "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "description": {"type": "STRING"}},
        "required": ["sheet", "column", "problem", "severity", "description"]}}},
    "required": ["problems"]}
WIPE_MOMENTS = 8          # moments per box sheet
WIPE_MARGIN = .06         # of the frame width: scene shown around the box
WIPE_STRICT = ("logo_left", "copied_objects", "upside_down")    # always high: a viewer sees them at once


def wipe_times(start: float, end: float, count: int = WIPE_MOMENTS) -> list[float]:
    """Moments spread over a box's time on the delivered clock."""
    length = max(0.0, end - start)
    return [round(start + length * (i + .5) / count, 2) for i in range(count)] if length > .2 else []


def wipe_sheets(plan_row: dict, assembled: dict, source_path: str, stem: Path) -> list[dict]:
    """Per boxed `remove` item a sheet of its corner over time (upper: the original with the logo, lower: the
    delivered video): {"sheet": path, "what", "times", "box"}."""
    return box_sheets([item for item in plan_row.get("remove") or [] if isinstance(item, dict) and item.get("box")],
                      assembled, source_path, stem, "upper: ORIGINAL (with the logo) | lower: DELIVERED (logo wiped)")


def box_sheets(items: list[dict], assembled: dict, source_path: str, stem: Path, legend: str) -> list[dict]:
    """Per boxed item a sheet of its box over time, the original above the delivered video:
    {"sheet": path, "what", "times", "box"}."""
    keep_start, keep_end = assembled["keep"]
    size = tuple(assembled["size"])
    found = []
    for item in items:
        a = max(keep_start, float(item.get("start", keep_start))) - keep_start
        b = min(keep_end, float(item.get("end", keep_end))) - keep_start
        times = [t for t in wipe_times(a, b) if not inside_live(assembled, t, t)]
        if not times:
            continue
        x0, y0, x1, y1 = item["box"]
        margin_y = WIPE_MARGIN * size[0] / size[1]
        crop = (max(0.0, x0 - WIPE_MARGIN), max(0.0, y0 - margin_y), min(1.0, x1 + WIPE_MARGIN), min(1.0, y1 + margin_y))
        pixels = (round(crop[0] * size[0]), round(crop[1] * size[1]), round(crop[2] * size[0]), round(crop[3] * size[1]))
        columns = []
        for t in times:
            upper = _still(source_path, t + keep_start, *size)
            lower = _still(assembled["output"], t, *size)
            if upper is None or lower is None:
                continue
            columns.append((f"{t:.1f} s", upper.crop(pixels), lower.crop(pixels), False))
        if not columns:
            continue
        w, h = pixels[2] - pixels[0], pixels[3] - pixels[1]
        scale = min(1.0, BOX_TILE / w)          # the crop at its own pixels (wide ones fitted to BOX_TILE)
        tile = (max(2, round(w * scale)), max(2, round(h * scale)))
        number = len(found) + 1
        animated = " [animated]" if item.get("dynamic") else ""
        sheet = _pairs_sheet(columns, tile, 4, f"sheet {number}{animated}: {item.get('what', '')} | {legend}")
        path = Path(f"{stem}-{number}.jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(path, quality=90)
        found.append({"sheet": str(path), "what": item.get("what", ""), "times": [_seconds(c[0]) for c in columns],
                      "box": item["box"], "dynamic": bool(item.get("dynamic"))})
    return found


def wipe_prompt(count: int) -> str:
    return "\n".join([
        f"These {count} sheets each show one corner of a game advertisement over time, column by column (the time "
        "is above each column). Upper row: the ORIGINAL game video, where a screen-fixed logo, watermark or warning "
        "text sits in that corner. Lower row: the DELIVERED restyled video, where the program wiped it and a video "
        "model repainted the scene behind it.",
        "Judge the lower row only, using the upper row to know what scene lies behind the logo (fences, icons, "
        "people, snow that the original also shows are fine, even if restyled to look real). Report a column when "
        "its lower picture shows any of these:",
        *[f"- {name}: {text}" for name, text in WIPE_PROBLEMS.items()],
        "Give the sheet number and column number (1 = leftmost, counting along each row of the sheet). Report "
        "nothing for clean columns; an empty list when every column is clean. Be strict: this is a delivery check, "
        "and a viewer notices a doubled icon, an upside-down person or a smeared corner.",
    ])


def wipe_check(job: Path, key: str, plan_row: dict, assembled: dict, source_path: str, stem: Path
               ) -> tuple[list[dict], list[dict]]:
    """Gemini looks at every wiped corner over time; returns (issues, sheets)."""
    sheets = wipe_sheets(plan_row, assembled, source_path, stem)
    if not sheets:
        return [], []
    parts = [winky.inline(Path(entry["sheet"]), "image/jpeg") for entry in sheets]
    parts.append({"text": wipe_prompt(len(sheets))})
    answer = winky.gemini(parts, schema=WIPE_SCHEMA)
    ledger.add(job, kind="gemini", purpose="wipe", key=key, seconds=0)
    # One issue per corner and kind of problem, over the run of neighbouring moments it shows in (a corner smeared
    # in every column is one finding, not eight).
    found: dict[tuple[int, str], list[tuple[int, dict]]] = {}
    for problem in answer.get("problems") or []:
        index, column = int(problem.get("sheet", 0)) - 1, int(problem.get("column", 0)) - 1
        if not (0 <= index < len(sheets)) or not (0 <= column < len(sheets[index]["times"])):
            continue
        found.setdefault((index, problem.get("problem", "smear")), []).append((column, problem))
    issues = []
    for (index, kind), hits in sorted(found.items()):
        hits.sort(key=lambda hit: hit[0])
        runs = [[hits[0]]]
        for hit in hits[1:]:
            if hit[0] == runs[-1][-1][0] + 1:
                runs[-1].append(hit)
            else:
                runs.append([hit])
        times = sheets[index]["times"]
        for span in runs:
            severities = [hit[1].get("severity", "medium") for hit in span]
            severity = "high" if kind in WIPE_STRICT else max(severities, key=lambda s: SEVERITY_RANK.get(s, 0))
            first, last = times[span[0][0]], times[span[-1][0]]
            moments = f"（{len(span)} 个时刻）" if len(span) > 1 else ""
            issues.append({"start": round(max(0.0, first - .3), 2), "end": round(last + .3, 2),
                           "category": "leftover_overlay" if kind == "logo_left" else "artifact",
                           "severity": severity, "where": sheets[index]["what"],
                           "description": f"去logo区域（{sheets[index]['what']}）{kind}{moments}："
                                          f"{span[0][1].get('description', '')}"})
    return issues, sheets


# --- the elements kept from the original (logos, warning lines, counters) ---------------------------------------------

KEEP_PROBLEMS = {
    "garbled": "letters, digits or characters differ from the original: misspelt, missing, extra, broken or "
               "unreadable strokes, other characters",
    "blurred": "softer, smeared or less crisp than in the original",
    "doubled": "a second copy, ghost, shadow outline or offset edge of the element next to or around it",
    "changed": "moved, resized, rotated, recoloured, restyled (realistic texture on the letters) or partly missing",
}
KEEP_SCHEMA = {
    "type": "OBJECT",
    "properties": {"problems": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "sheet": {"type": "INTEGER"}, "column": {"type": "INTEGER"},
        "problem": {"type": "STRING", "enum": sorted(KEEP_PROBLEMS)},
        "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "description": {"type": "STRING"}},
        "required": ["sheet", "column", "problem", "severity", "description"]}}},
    "required": ["problems"]}
KEEP_STRICT = ("garbled", "blurred", "doubled", "changed")    # always high: kept elements must stay faithful
KEEP_MOTION = ("blurred", "doubled")    # tolerated on animated elements (low): blur or a trail while they move
BOX_TILE = 900          # px: the widest a box close-up is drawn (its own pixels below that)


def keep_prompt(count: int) -> str:
    return "\n".join([
        f"These {count} sheets each show one screen-fixed element of a game advertisement over time (a game logo, "
        "a warning or disclaimer line, a HUD counter, a caption, banner or button), column by column (the time is "
        "above each column). Upper row: the ORIGINAL video. Lower row: the DELIVERED restyled video, where an AI "
        "model redrew the element; it must match the original's: the same characters and digits, sharpness, "
        "position, size and colours, flat and not restyled. The scene around it may look realistic in the lower "
        "row; that is intended.",
        "Compare the element itself, character by character. Report a column when its lower picture shows any of "
        "these:", *[f"- {name}: {text}" for name, text in KEEP_PROBLEMS.items()],
        "Give the sheet number and column number (1 = leftmost, counting along each row of the sheet). Report "
        "nothing for faithful columns; an empty list when every column is faithful. Be strict: a single wrong or "
        "blurred character or a soft, haloed edge of static text or a logo is a defect.",
        "Sheets titled [animated] show an element that moves or changes (counting digits, popping or flying text, "
        "a HUD effect): a little blur or trailing while it moves is acceptable there (report it as low); garbled "
        "characters or wrong digits are not.",
    ])


def keep_issues(answer: dict, sheets: list[dict]) -> list[dict]:
    """Gemini's findings as issues: one per element and kind over each run of neighbouring moments."""
    found: dict[tuple[int, str], list[tuple[int, dict]]] = {}
    for problem in answer.get("problems") or []:
        index, column = int(problem.get("sheet", 0)) - 1, int(problem.get("column", 0)) - 1
        if 0 <= index < len(sheets) and 0 <= column < len(sheets[index]["times"]):
            found.setdefault((index, problem.get("problem", "changed")), []).append((column, problem))
    issues = []
    for (index, kind), hits in sorted(found.items()):
        hits.sort(key=lambda hit: hit[0])
        runs = [[hits[0]]]
        for hit in hits[1:]:
            if hit[0] == runs[-1][-1][0] + 1:
                runs[-1].append(hit)
            else:
                runs.append([hit])
        times = sheets[index]["times"]
        for span in runs:
            severity = ("low" if kind in KEEP_MOTION and sheets[index].get("dynamic") else
                        "high" if kind in KEEP_STRICT else
                        max((hit[1].get("severity", "medium") for hit in span), key=lambda s: SEVERITY_RANK.get(s, 0)))
            moments = f"（{len(span)} 个时刻）" if len(span) > 1 else ""
            issues.append({"start": round(max(0.0, times[span[0][0]] - .3), 2), "end": round(times[span[-1][0]] + .3, 2),
                           "category": "added_text" if kind == "garbled" else "artifact", "severity": severity,
                           "where": sheets[index]["what"],
                           "description": f"保留元素（{sheets[index]['what']}）{kind}{moments}："
                                          f"{span[0][1].get('description', '')}"})
    return issues


def keep_check(job: Path, key: str, plan_row: dict, assembled: dict, source_path: str, stem: Path
               ) -> tuple[list[dict], list[dict]]:
    """Gemini compares every kept element (a `ui` item with a box) with the original over time; returns (issues,
    sheets)."""
    sheets = box_sheets([item for item in plan_row.get("ui") or [] if isinstance(item, dict) and item.get("box")],
                        assembled, source_path, stem, "upper: ORIGINAL | lower: DELIVERED (must match)")
    if not sheets:
        return [], []
    parts = [winky.inline(Path(entry["sheet"]), "image/jpeg") for entry in sheets]
    parts.append({"text": keep_prompt(len(sheets))})
    answer = winky.gemini(parts, schema=KEEP_SCHEMA)
    ledger.add(job, kind="gemini", purpose="keep", key=key, seconds=0)
    return keep_issues(answer, sheets), sheets


def judge(issues: list[dict]) -> str:
    high = any(i["severity"] == "high" for i in issues)
    return "fail" if high or sum(i["severity"] == "medium" for i in issues) >= 2 else "pass"


def auto_fps(duration: float) -> float:
    """Frames per second Gemini samples: short ads get 4 so flicker and brief morphs are not missed."""
    return 4.0 if duration <= 75 else 2.0


SEAM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "consistent": {"type": "BOOLEAN"},
        "severity": {"type": "STRING", "enum": ["high", "medium", "low", "none"]},
        "changed": {"type": "ARRAY", "items": {"type": "STRING"}},
        "description": {"type": "STRING"}},
    "required": ["consistent", "severity", "changed", "description"]}
SEAM_WINDOW = 2.0       # seconds on either side of a join that Gemini watches and the agent's stills cover
SEAM_STEP = 1.0         # seconds between the agent's stills of a seam (plus the frames on both sides of the switch)


def seam_prompt(seam: dict, layout: str, switch: float, inside: float | None, length: float) -> str:
    other = "right" if layout == "left" else "bottom"
    base = (f"This {length:.1f}-second clip shows the ORIGINAL game video on the {layout} and an AI-restyled version "
            f"on the {other}, frame-synchronised, at the video's own frame rate. At {switch:.2f} s two separately "
            "generated parts of the restyled video meet"
            + (f"; the later part's own frames start at {inside:.2f} s: watch that moment as closely" if inside is not None
               else "") + ". ")
    if seam["seam"] == "continuous":
        task = ("In the original this is one continuous shot: nothing changes at that moment. Look only at the restyled "
                "side, frame by frame around it, and decide whether a viewer could notice the join: a jump; a dissolve "
                "or a gradual change over a second or so; a step in colour, brightness, contrast or saturation; any "
                "change of terrain, ground, water, rocks, trees, objects, rope or cable colour, textures, lighting or "
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


def seam_moments(switch: float, inside: float | None, fps: float, duration: float) -> list[tuple[float, str]]:
    """The agent's stills of a seam: every SEAM_STEP s from SEAM_WINDOW before the switch to SEAM_WINDOW after it (after
    the moment the later part's own frames start, when that is later), and the last frame before and the first frame
    after the switch (and that moment): (seconds on the delivered clock, label)."""
    last = duration - 1 / fps
    end = max(switch, inside if inside is not None else switch)
    wanted = {round(switch - 1 / fps, 4): "last frame before the switch", round(switch, 4): "first frame after the switch"}
    if inside is not None and abs(inside - switch) > .02:
        wanted.setdefault(round(inside - 1 / fps, 4), "last frame before its own frames")
        wanted.setdefault(round(inside, 4), "first of its own frames")
    step = -SEAM_WINDOW
    while step <= SEAM_WINDOW + 1e-6:
        if step:
            moment = round((switch if step < 0 else end) + step, 4)
            wanted.setdefault(moment, f"{step:+.0f} s")
        step += SEAM_STEP
    return [(t, label) for t, label in sorted(wanted.items()) if 0 <= t <= last]


def seam_folder(job: Path, source_id: str, segment: str) -> Path:
    return job / "review/seams" / f"{source_id}-{segment}"


def seam_check(job: Path, source_id: str, source_path: str, assembled: dict) -> list[dict]:
    """Every join of the delivered video (none beside a live-action stretch): Gemini watches the original and the
    delivery side by side, SEAM_WINDOW s on either side, at the video's own frame rate; the agent gets full-size
    stills (original above, delivered below) every SEAM_STEP s and of the frames on both sides of the switch, on a
    card `review/seams/<id>-<Kxx>/card.md`. Returns one entry per seam with Gemini's answer."""
    import shutil
    from reskin import credentials
    size = tuple(assembled["size"])
    fps = assembled["fps"]
    duration = assembled["duration"]
    keep_start = assembled["keep"][0]
    order = [s["id"] for s in assembled["segments"]]
    live = {s["id"] for s in assembled["segments"] if s.get("live")}
    try:
        credentials.winky_key()
        has_key = True
    except credentials.MissingCredential:
        has_key = False
    layout = "left" if size[1] >= size[0] else "top"
    found = []
    for seam in assembled["seams"]:
        segment = seam["segment"]
        before = order[order.index(segment) - 1] if segment in order and order.index(segment) else None
        if segment in live or before in live:
            continue
        switch = seam["time"]
        inside = (seam.get("inside") or {}).get("time")
        inside = inside if inside is not None and abs(inside - switch) > .02 else None
        a = max(0.0, min(switch, inside if inside is not None else switch) - SEAM_WINDOW)
        b = min(duration, max(switch, inside if inside is not None else switch) + SEAM_WINDOW)
        here = seam_folder(job, source_id, segment)
        shutil.rmtree(here, ignore_errors=True)
        here.mkdir(parents=True, exist_ok=True)
        stills = []
        for number, (t, label) in enumerate(seam_moments(switch, inside, fps, duration), 1):
            title = f"seam {before}|{segment} {label} (video {t:.3f} s)"
            stills.append(str(still_pair((source_path, keep_start + t, f"ORIGINAL {title}"),
                                         (assembled["output"], t, f"DELIVERED {title}"), size,
                                         here / f"{number:02d}-{t:08.3f}.jpg")))
        entry = {**seam, "before": before, "window": [round(a, 3), round(b, 3)], "stills": stills, "answer": None}
        if has_key:
            clip = grid_clip([{"path": source_path, "start": keep_start + a, "end": keep_start + b, "label": "ORIGINAL"},
                              {"path": assembled["output"], "start": a, "end": b, "label": "RESTYLED"}],
                             size, here / "seam.mp4")
            entry["clip"] = str(clip)
            try:
                answer = winky.gemini([video_part(clip, fps), {"text": seam_prompt(
                    seam, layout, switch - a, (inside - a) if inside is not None else None, b - a)}], schema=SEAM_SCHEMA)
                ledger.add(job, kind="gemini", purpose="seam", key=f"{source_id}:{segment}", seconds=round(b - a, 2))
                entry["answer"] = answer
                log(f"{source_id}: seam {segment} at {switch:.2f} s: "
                    + ("consistent" if answer.get("consistent") else
                       f"{answer.get('severity')} ({', '.join(answer.get('changed') or [])})"))
            except (RuntimeError, ValueError) as error:
                entry["error"] = str(error)
                log(f"{source_id}: Gemini seam check {segment} failed ({error}); the stills are on its card")
        entry["flagged"] = bool((entry["answer"] and not entry["answer"].get("consistent")
                                 and entry["answer"].get("severity") in ("medium", "high"))
                                or seam.get("jump") == "visible" or (seam.get("look") or {}).get("jump") == "visible")
        entry["card"] = str(seam_card(here, source_id, entry))
        found.append(entry)
    return found


def seam_card(here: Path, source_id: str, entry: dict) -> Path:
    key = f"{source_id}:{entry['segment']}"
    answer = entry.get("answer") or {}
    lines = [f"# Seam check {source_id} {entry['before']}|{entry['segment']}", "",
             f"switch at {entry['time']:.3f} s of the delivered video ({'inside a shot' if entry['seam'] == 'continuous' else 'at a camera cut'})"
             + (f"; the later segment's own frames start at {entry['inside']['time']:.3f} s"
                if (entry.get("inside") or {}).get("time") not in (None, entry["time"]) else ""),
             f"status: {'FLAGGED' if entry['flagged'] else 'nothing found by the measures or Gemini'} - waiting for the agent",
             "", "## Measured",
             f"- switch: {entry.get('jump', '?')} (mismatch {entry.get('mismatch')} vs motion {entry.get('motion')}, "
             f"local {entry.get('local')})"]
    if entry.get("look"):
        look = entry["look"]
        lines.append(f"- look step over the whole video: {look.get('jump')} (saturation z {look['saturation']['z']}, "
                     f"colour z {look['colour']['z']})")
    lines += ["", "## Gemini (the 4 s around it, at the video's frame rate)"]
    if entry.get("error"):
        lines.append(f"- failed: {entry['error']} (judge from the stills alone)")
    elif answer:
        lines.append(f"- {'consistent' if answer.get('consistent') else 'changes'} [{answer.get('severity')}]: "
                     f"{answer.get('description', '')}" + (f" ({'、'.join(answer['changed'])})" if answer.get("changed") else ""))
    else:
        lines.append("- not asked (no Winky key)")
    lines += ["", "## Stills (upper original, lower delivered; every one, at full size)", *[f"- {p}" for p in entry["stills"]],
              "", "## What to look for",
              "- Inside a shot the delivered picture must not change at the switch or where the later segment's own frames "
              "start: compare the last frame before and the first frame after, region by region (water colour and "
              "saturation, snow, trees at the edges, ground texture, every object and character), then the stills a "
              "second apart for a gradual change (a fade, a slow regrade). A change the original also shows is no flaw.",
              "- At a camera cut: the same look on both sides (colour grade, brightness, materials, realism, the same "
              "subjects rendered the same way).",
              "", "## Report (a sub-agent writes this back)",
              "`pass` or one line per flaw: `<still file> | <what changes, where in the frame> | <high / medium / low>`.",
              "", "## Verdict",
              f"- no visible change: `accept --seam {key}`" + (" --reason \"<why the finding is none>\"" if entry["flagged"] else ""),
              f"- a confirmed jump: `redo --segment {key} --reason \"<what>\"` (the segment after the seam)"]
    path = here / "card.md"
    write_text(path, "\n".join(lines) + "\n")
    return path


def seam_record_path(job: Path, source_id: str) -> Path:
    return job / "state/checks" / f"{source_id}-seams.json"


def seam_verdicts(job: Path, source_id: str) -> dict:
    path = seam_record_path(job, source_id)
    return read(path) if path.is_file() else {}


def pending_seams(job: Path, source_id: str, report: dict | None = None) -> list[dict]:
    """The seam checks of the newest review round that the agent has not passed for the current delivery."""
    report = report if report is not None else last_round(job, source_id)
    if not report:
        return []
    verdicts = seam_verdicts(job, source_id)
    return [entry for entry in report.get("seam_checks") or []
            if not (verdicts.get(entry["segment"], {}).get("assembled") == report.get("assembled")
                    and verdicts.get(entry["segment"], {}).get("accepted"))]


def accept_seam(job: Path, source_id: str, segment: str, note: str = "") -> dict:
    """The agent looked at a seam's stills and passes it, for the current delivery (a new assembly asks again); a
    flagged seam needs the reason its finding is none."""
    report = last_round(job, source_id)
    entry = next((e for e in (report or {}).get("seam_checks") or [] if e["segment"] == segment), None)
    if entry is None:
        raise ValueError(f"{source_id}:{segment} has no seam check in the last review round (review first)")
    record = read(job / "state/assembled" / f"{source_id}.json")
    if record.get("inputs") != report.get("assembled"):
        raise ValueError(f"{source_id} was assembled again after its last review: review it first")
    if entry.get("flagged") and not note.strip():
        raise ValueError(f"{source_id}:{segment} is flagged: accept it only with --reason saying why the stills show no "
                         "jump")
    verdicts = seam_verdicts(job, source_id)
    verdicts[segment] = {"assembled": report.get("assembled"), "accepted": True,
                         "note": note.strip() or "stills checked: no visible change"}
    write(seam_record_path(job, source_id), verdicts)
    return verdicts[segment]


def _frame(path: str, t: float, width: int, height: int) -> Image.Image:
    raw = run([FFMPEG, "-v", "error", "-ss", f"{max(0.0, t):.4f}", "-i", path, "-frames:v", "1", "-vf",
               f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
    return Image.frombytes("RGB", (width, height), raw[: width * height * 3])


def frame_at(path: str, t: float, width: int, height: int) -> Image.Image:
    """The frame at `t` (full quality scaling); a moment past the last frame takes the last one."""
    for moment in (t, t - .05, t - .15, t - .4):
        raw = run([FFMPEG, "-v", "error", "-ss", f"{max(0.0, moment):.4f}", "-i", path, "-frames:v", "1", "-vf",
                   f"scale={width}:{height}:flags=lanczos", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
        if len(raw) >= width * height * 3:
            return Image.frombytes("RGB", (width, height), raw[: width * height * 3])
    raise RuntimeError(f"no frame at {t:.2f} s in {Path(path).name}")


def segment_for(time_a: float, time_b: float, assembled: dict) -> list[str]:
    hits = [s["id"] for s in assembled["segments"] if s["start"] - .25 <= time_b and time_a <= s["end"] + .25]
    return hits or [assembled["segments"][-1]["id"]]


def issue_moment(issue: dict) -> float:
    return round((issue["start"] + issue["end"]) / 2, 2) if issue["end"] - issue["start"] <= 1.0 else issue["start"]


def compare_sheet(source_path: str, source_offset: float, result_path: str, result_duration: float,
                  size: tuple[int, int], entries: list[tuple[str, float]], output: Path, span: float = .3) -> Path:
    """Per entry (title, t on the result's clock): a title band, then the original (upper row) and the restyled
    (lower row) at t-span, t and t+span."""
    width, height = size
    tile_w = 300 if height >= width else 480
    tile_h = round(tile_w * height / width)
    label = font(18)
    band = 30 if any(title for title, _ in entries) else 0
    block = band + 2 * (tile_h + 26)
    canvas = Image.new("RGB", (3 * tile_w, block * len(entries)), "white")
    draw = ImageDraw.Draw(canvas)
    for number, (title, t) in enumerate(entries):
        top = number * block
        if band:
            draw.rectangle([0, top, 3 * tile_w, top + band - 4], fill=(230, 230, 230))
            draw.text((6, top + 5), title, fill="black", font=label)
        moments = [max(0.0, t - span), t, min(result_duration - .02, t + span)]
        for row, (path, offset, name) in enumerate(((source_path, source_offset, "original"),
                                                    (result_path, 0.0, "restyled"))):
            y = top + band + row * (tile_h + 26)
            for column, moment in enumerate(moments):
                raw = run([FFMPEG, "-v", "error", "-ss", f"{moment + offset:.3f}", "-i", path, "-frames:v", "1",
                           "-vf", f"scale={tile_w}:{tile_h}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          binary=True)
                if len(raw) >= tile_w * tile_h * 3:
                    canvas.paste(Image.frombytes("RGB", (tile_w, tile_h), raw[: tile_w * tile_h * 3]),
                                 (column * tile_w, y + 26))
                draw.text((column * tile_w + 4, y + 4), f"{name} {moment:.2f} s", fill="black", font=label)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=85)
    return output


def issue_sheets(source_path: str, source_offset: float, result_path: str, result_duration: float,
                 size: tuple[int, int], issues: list[dict], stem: Path, per_page: int = 4) -> list[Path]:
    """The stills of every issue, a few issues per image: what the agent confirms or dismisses them on."""
    pages = []
    for page, first in enumerate(range(0, len(issues), per_page), 1):
        chunk = issues[first:first + per_page]
        entries = [(f"#{i['n']} [{i['severity']}] {i['category']} {i['start']:.2f}-{i['end']:.2f} s ({i.get('source', '')})",
                    issue_moment(i)) for i in chunk]
        pages.append(compare_sheet(source_path, source_offset, result_path, result_duration, size, entries,
                                   stem.with_name(f"{stem.name}-p{page}.jpg")))
    return pages


def review_source(job: Path, state: dict, source_id: str, plan_row: dict, fps: float | None = None) -> dict:
    record = job / "state/assembled" / f"{source_id}.json"
    newest = max((p.stat().st_mtime for p in (job / "ai").glob(f"{source_id}-*.mp4")), default=0.0)
    if record.stat().st_mtime < newest:
        raise ValueError(f"{source_id}: segments were rendered after the last assemble; run assemble first "
                         "(reviewing now would judge the old video)")
    assembled = read(record)
    fps = fps or auto_fps(assembled["duration"])
    clip, layout = proxy(job, state, source_id, assembled)
    size = clip.stat().st_size
    if size > INLINE_LIMIT:
        raise RuntimeError(f"{source_id}: review proxy is {size / 1e6:.0f} MB, over the {INLINE_LIMIT / 1e6:.0f} MB "
                           "inline limit even at the lowest quality")
    log(f"{source_id}: asking Gemini to review ({size / 1e6:.1f} MB comparison clip, {fps:g} fps)")
    part = video_part(clip, fps)
    answer = winky.gemini([part, {"text": prompt(state, plan_row, assembled, layout,
                                                 known=accepted_notes(job, source_id))}], schema=SCHEMA)
    ledger.add(job, kind="gemini", purpose="review", key=source_id, seconds=assembled["duration"])
    issues = []
    for issue in answer.get("issues") or []:
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        a, b = min(a, b), max(a, b)
        if inside_live(assembled, a, b):
            continue        # the original's own frames, delivered as they are
        issues.append({"n": len(issues) + 1, **issue, "start": round(a, 2), "end": round(b, 2),
                       "segments": segment_for(a, b, assembled), "source": "gemini"})
    def add(**issue):
        issues.append({"n": len(issues) + 1, **issue})
    realism = [r for r in realism_ranges(answer) if not inside_live(assembled, r["start"], r["end"])]
    for issue in realism_issues(realism, issues):
        add(**issue, source="gemini-realism", segments=segment_for(issue["start"], issue["end"], assembled))
    for seam in assembled.get("colour") or []:
        if seam["residual"] > 6:
            when = next((s["time"] for s in assembled["seams"] if s["segment"] == seam["before"]), 0.0)
            add(start=when, end=when, category="seam_color", severity="medium", where="whole frame", source="measured",
                description=f"接缝 {seam['before']} 处颜色校正后仍差 {seam['residual']}（0-255）", segments=[seam["before"]])
    # Measured: how far the two versions differ where the video switches from one to the other. A slight jump alone
    # is low (it rarely shows in playback); it counts when the Gemini close-up sees a change there too.
    for seam in assembled["seams"]:
        look = seam.get("look") or {}
        if look.get("jump") in ("visible", "slight"):
            add(start=seam["time"], end=seam["time"], category="seam_color",
                severity="high" if look["jump"] == "visible" else "medium", where="whole frame", source="measured",
                description=f"接缝 {seam['segment']} 处画面观感突变：饱和度变化 {look['saturation']['step']}（平时 "
                            f"{look['saturation']['usual']}，z {look['saturation']['z']}），颜色变化 {look['colour']['step']}"
                            f"（平时 {look['colour']['usual']}，z {look['colour']['z']}），已扣除镜头运动和原片自身的变化",
                segments=[seam["segment"]])
        if seam.get("jump") in ("visible", "slight") and look.get("jump") != seam.get("jump"):
            inside = seam.get("inside") or {}
            add(start=seam["time"], end=max(seam["time"], inside.get("time", seam["time"])), category="seam_color",
                severity="high" if seam["jump"] == "visible" else "low", where="whole frame", source="measured",
                description=f"接缝 {seam['segment']} 处两段画面差异 {seam['mismatch']}（局部 {seam.get('local', 0)}），画面自身运动只有 {seam['motion']}"
                            + (f"；本段自己的画面开始处（{inside['time']:.2f} s）逐帧变化 {inside['mismatch']}，平时 {inside['motion']}"
                               if inside.get("jump") in ("visible", "slight") else "")
                            + f"（{'明显跳变' if seam['jump'] == 'visible' else '轻微跳变'}）",
                segments=[seam["segment"]])
    # Measured: corners a logo was wiped from that came back as a smooth patch (see logos.quality).
    for wipe in assembled.get("wipes") or []:
        if wipe.get("verdict") in ("smeared", "soft"):
            moments = wipe.get("smeared_at") or [wipe["start"]]
            add(start=moments[0], end=moments[-1], category="artifact",
                severity="high" if wipe["verdict"] == "smeared" else "low", where=wipe["what"], source="measured",
                description=f"去logo区域（{wipe['what']}）{'留下模糊的涂抹色块' if wipe['verdict'] == 'smeared' else '偏模糊'}："
                            f"细节只有周围场景的 {wipe['share']}",
                segments=segment_for(moments[0], moments[-1], assembled))
    # The same characters and objects from the first second to the last (stills beside the styled keyframes).
    source = next(r for r in state["sources"] if r["id"] == source_id)
    number = len((read(job / "state/review" / f"{source_id}.json") if (job / "state/review" / f"{source_id}.json").is_file()
                  else {"rounds": []})["rounds"]) + 1
    size = tuple(assembled["size"])
    frames = identity_frames(identity_references(job, source_id, plan_row), size, source_path=source["path"],
                             source_offset=assembled["keep"][0], result_path=assembled["output"],
                             times=identity_times(0.0, assembled["duration"]))
    subjects, found, identity_sheets = identity_check(job, source_id, frames, plan_row, "a whole restyled video", size,
                                                      job / "review" / f"{source_id}-identity-r{number}")
    for issue in found:
        add(**issue, source="gemini-identity", segments=segment_for(issue["start"], issue["end"], assembled))
    log(f"{source_id}: identity check: {len(subjects)} subjects, {len(found)} changes")
    # Every corner a logo was wiped from, over time: leftover letters, copied or flipped scene, smears.
    wiped, wipe_sheet_list = wipe_check(job, source_id, plan_row, assembled, source["path"],
                                        job / "review" / f"{source_id}-wipes-r{number}")
    for issue in wiped:
        add(**issue, source="gemini-wipe", segments=segment_for(issue["start"], issue["end"], assembled))
    if wipe_sheet_list:
        log(f"{source_id}: wipe check: {len(wipe_sheet_list)} corners, {len(wiped)} problems")
    # Every kept logo, warning line, counter and screen text (a `ui` item with a box) against the original,
    # character by character.
    kept_found, keep_sheet_list = keep_check(job, source_id, plan_row, assembled, source["path"],
                                             job / "review" / f"{source_id}-kept-r{number}")
    for issue in kept_found:
        add(**issue, source="gemini-keep", segments=segment_for(issue["start"], issue["end"], assembled))
    if keep_sheet_list:
        log(f"{source_id}: kept-element check: {len(keep_sheet_list)} elements, {len(kept_found)} problems")
    # Every join: Gemini watches the 4 s around it at the video's frame rate; the agent gets its stills (a card).
    seam_checks = seam_check(job, source_id, source["path"], assembled)
    for seam in seam_checks:
        reply = seam.get("answer") or {}
        if reply and not reply.get("consistent") and reply.get("severity") not in (None, "none"):
            add(start=round(seam["time"] - .3, 2), end=round(seam["time"] + .3, 2), category="seam_color",
                severity=reply.get("severity", "medium"), where="whole frame", source="gemini-seam",
                description=f"接缝 {seam['segment']}：{reply.get('description', '')}"
                            + (f"（变化：{'、'.join(reply['changed'])}）" if reply.get("changed") else ""),
                segments=[seam["segment"]])
    verdict = "fail" if answer.get("verdict") == "fail" or judge(issues) == "fail" else "pass"
    rounds_path = job / "state/review" / f"{source_id}.json"
    history = read(rounds_path) if rounds_path.is_file() else {"rounds": []}
    sheets = issue_sheets(source["path"], assembled["keep"][0], assembled["output"], assembled["duration"],
                          tuple(assembled["size"]), issues, job / "review/issues" / f"{source_id}-r{number}")
    result = {"source": source_id, "round": number, "output": assembled["output"], "verdict": verdict,
              "summary": answer.get("summary", ""), "realism": realism, "identity": subjects,
              "identity_sheets": [str(p) for p in identity_sheets],
              "wipe_sheets": [entry["sheet"] for entry in wipe_sheet_list],
              "keep_sheets": [entry["sheet"] for entry in keep_sheet_list], "issues": issues, "compare": str(clip),
              "sheets": [str(p) for p in sheets], "assembled": assembled.get("inputs"),
              "seam_checks": [{"segment": s["segment"], "before": s["before"], "time": s["time"], "seam": s["seam"],
                               "flagged": s["flagged"], "card": s["card"], "stills": s["stills"],
                               "gemini": (s.get("answer") or {}).get("severity"), "error": s.get("error")}
                              for s in seam_checks]}
    history["rounds"].append({k: result[k] for k in ("round", "verdict", "summary", "assembled")} | {"issues": len(issues)})
    write(rounds_path, history)
    write(job / "reports/review" / f"{source_id}-r{number}.json", result)
    write_text(job / "reports/review" / f"{source_id}-r{number}.md", markdown(result))
    return result


def last_round(job: Path, source_id: str) -> dict | None:
    """The newest review report of a source (with its issues), or None."""
    rounds_path = job / "state/review" / f"{source_id}.json"
    if not rounds_path.is_file():
        return None
    rounds = read(rounds_path)["rounds"]
    report = job / "reports/review" / f"{source_id}-r{rounds[-1]['round']}.json" if rounds else None
    return read(report) if report and report.is_file() else None


def describe_issue(issue: dict) -> str:
    return (f"#{issue['n']} [{issue['severity']}] {issue['category']} {issue['start']:.2f}-{issue['end']:.2f} s "
            f"({', '.join(issue['segments'])}; {issue.get('where', '')}; {issue['source']}): {issue['description']}")


def markdown(result: dict) -> str:
    lines = [f"# Review {result['source']} round {result['round']}: {result['verdict']}", "", result["summary"], "",
             f"Realism (Gemini, 5 = real camera footage): {realism_line(result.get('realism') or [])}", "",
             *([f"Identity sheets (close-ups of each recurring subject, whole frames, beside the styled keyframes): "
                f"{', '.join(Path(p).name for p in result['identity_sheets'])}"] if result.get("identity_sheets") else []),
             *[f"- {s.get('what', '')}: {s.get('reference_look', '')}; "
               + ", ".join(f"{c.get('label')} {c.get('difference')}" for c in s.get("columns") or [])
               for s in result.get("identity") or []], ""]
    lines += [f"- {describe_issue(issue)}" for issue in result["issues"]]
    if result.get("sheets"):
        lines += ["", "Stills of every issue (upper row original, lower row restyled): "
                  + ", ".join(Path(p).name for p in result["sheets"])]
    if result.get("wipe_sheets"):
        lines += ["", "Wiped corners over time (upper row original with the logo, lower row delivered): "
                  + ", ".join(Path(p).name for p in result["wipe_sheets"])]
    if result.get("keep_sheets"):
        lines += ["", "Kept logos, warning lines, counters and screen text over time (upper row original, lower "
                  "row delivered, must match): " + ", ".join(Path(p).name for p in result["keep_sheets"])]
    if result.get("seam_checks"):
        lines += ["", "Seam checks (the agent passes each on its stills: accept --seam <id>:<Kxx>):"]
        lines += [f"- {s['before']}|{s['segment']} at {s['time']:.2f} s ({s['seam']}): "
                  f"{'FLAGGED' if s['flagged'] else 'nothing found'}, Gemini {s.get('gemini') or s.get('error') or '-'}; "
                  f"card {s['card']}" for s in result["seam_checks"]]
    return "\n".join(lines) + "\n"


def stills(job: Path, state: dict, source_id: str, times: list[float], span: float = .3) -> list[Path]:
    """For each time: original (top row) and restyled (bottom row) at t-span, t, t+span."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    return [compare_sheet(source["path"], assembled["keep"][0], assembled["output"], assembled["duration"],
                          tuple(assembled["size"]), [("", t)], job / "review/frames" / f"{source_id}-{t:07.2f}.jpg",
                          span=span)
            for t in times]
