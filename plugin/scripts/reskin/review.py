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
    "identity": "a recurring character or object looks different in another part of the video (clothes or their "
                "colours, headwear, body proportions, face, species, material, realism level) while the original keeps "
                "it the same: compare across the whole clip, the first seconds with the last, not only neighbouring "
                "moments; a gradual drift counts",
    "not_realistic": "an object or area on the restyled side still looks like a game or cartoon instead of real camera "
                     "footage: smooth rounded blob shapes, toy-like or plastic vehicles and machines, doll, chibi or "
                     "figurine people, plastic, clay or candy-coloured surfaces, flat fills, simple gradients, outlines, "
                     "oversaturated colours (name the objects)",
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


def side_by_side(source_path: str, start: float, end: float, result_path: str, size: tuple[int, int],
                 output: Path) -> tuple[Path, str]:
    """Original (left or top) and restyled (right or bottom) on one small clip, the same clock for both: the source
    from `start` to `end`, the result from its first frame."""
    width, height = size
    portrait = height >= width
    side = (480, round(480 * height / width / 2) * 2) if portrait else (854, round(854 * height / width / 2) * 2)
    output.parent.mkdir(parents=True, exist_ok=True)
    stack = "hstack" if portrait else "vstack"
    graph = (f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,fps=15,scale={side[0]}:{side[1]}:flags=area"
             f"{_drawtext('ORIGINAL')}[a];"
             f"[1:v]setpts=PTS-STARTPTS,fps=15,scale={side[0]}:{side[1]}:flags=area{_drawtext('RESTYLED')}[b];"
             f"[a][b]{stack}=inputs=2:shortest=1,format=yuv420p[v]")
    run([FFMPEG, "-v", "error", "-y", "-i", source_path, "-i", result_path, "-filter_complex", graph,
         "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", output])
    return output, ("left" if portrait else "top")


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


def _goal(state: dict, plan_row: dict, layout: str, what: str) -> list[str]:
    other = "right" if layout == "left" else "bottom"
    removal = describe(plan_row["remove"]) or "nothing named"
    hud = "; ".join(item.get("what") or "HUD box" for item in plan_row.get("hud") or [])
    burned = describe(plan_row.get("overlays") or [])
    style = state["style"] if isinstance(state["style"], str) else state["style"].get("label")
    return [
        "You are a strict quality reviewer for AI-restyled game advertising videos.",
        f"The {what} shows the ORIGINAL on the {layout} and the RESTYLED version on the {other}, frame-synchronised "
        f"(same timestamps). The restyle goal: {describe(plan_row['materials'])} — game-rendered cartoon materials replaced by "
        f"realistic real-world materials (style preset: {style}) while composition, camera motion, characters, object "
        "positions, counts and timing stay exactly as in the original. HUD numbers, icons and buttons must stay.",
        f"Removed on purpose (must NOT appear on the restyled side): {removal}. The closing card was cut off on purpose.",
        *([f"Screen-fixed HUD copied from the original afterwards (it may look wrong on the restyled side here; do not "
           f"report its digits): {hud}."] if hud and what == "clip of one segment" else
          [f"Screen-fixed HUD copied from the original (identical on both sides by design): {hud}."] if hud else []),
        *([f"UI and text burned in by the program afterwards (copied from the original or drawn; the models leave these "
           f"areas without them on purpose): {burned}."] if burned else []),
    ]


def _rules() -> list[str]:
    return [
        "Report every defect on the RESTYLED side, using the original only as the reference for what should be there. "
        "Do not report flaws that the original shows identically (game physics, clipping already in the original), "
        "and do not report the intended material change itself, nor objects rebuilt as their real-world counterparts "
        "(new shape details, proportions and surfaces are wanted) as long as position, count, colour and motion match.",
        "Realism is part of the check: the restyle must look like footage filmed by a real camera, more real than a "
        "good game render. Judge the RESTYLED side against real-world footage, not against the original. Report each "
        "object or area that still looks game-like as not_realistic (high when a large or prominent object or most "
        "of the frame looks like a game, medium otherwise). HUD, icons, price tags, bubbles and other UI are exempt.",
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


def prompt(state: dict, plan_row: dict, assembled: dict, layout: str, known: list[str] | None = None) -> str:
    seams = ", ".join(f"{s['time']:.2f} s ({'hard cut' if s['seam'] == 'cut' else 'inside a shot'})"
                      for s in assembled["seams"]) or "none"
    lines = [
        *_goal(state, plan_row, layout, "video"),
        f"The restyled video was rendered in segments and joined at: {seams}. Check these moments closely: inside a "
        "shot the restyled scene must continue unchanged (same terrain, objects, rope and cable colours, textures, "
        "light); any change, dissolve or double image there is a seam_color defect.",
        *_rules(), *_known(known),
    ]
    return "\n".join(lines)


def segment_prompt(state: dict, plan_row: dict, layout: str, known: list[str] | None = None) -> str:
    """For the check right after one segment renders (before the next segment of its shot starts from it)."""
    lines = [*_goal(state, plan_row, layout, "clip of one segment"),
             "This is one separately rendered segment, checked before the rest of the shot is rendered from it: look "
             "hardest for wrong or vanished objects, changed colours, leftover cartoon look (in its later seconds too: "
             "what comes into view later must be as real as the first frame), garbled or added text, leftover logos "
             "and warning lines, and morphing.", *_rules(), *_known(known)]
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


def realism_issues(ranges: list[dict], issues: list[dict]) -> list[dict]:
    """A stretch rated 3 or lower that no not_realistic issue covers becomes one (3: medium, 1-2: high), so a segment
    that drifts back to the game look is held like any other flaw. Neighbouring ranges with the same score make one
    issue (the timeline keeps the detail)."""
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
        if item["score"] > 3 or item["end"] - item["start"] < .5:
            continue
        if any(i["category"] == "not_realistic" and i["start"] <= item["end"] and item["start"] <= i["end"]
               for i in issues):
            continue
        objects = "、".join(item["cartoonish"][:6]) or "画面"
        found.append({"start": item["start"], "end": item["end"], "category": "not_realistic",
                      "severity": "medium" if item["score"] == 3 else "high", "where": objects,
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
    from reskin.overlay import _font
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


def detect_prompt(count: int) -> str:
    return (f"These are {count} frames of one game video, each after its number. Pick the recurring subjects whose "
            "look must stay the same through the video: always the player character (the one the player controls: "
            f"it walks around, fishes, carries and sells things), and up to {SUBJECTS - 1} more that show in several "
            "frames (e.g. the customers of a queue, a vehicle, a machine, an animal). Name each in Chinese in `name` "
            "(`主角`, `排队的顾客`, ...) and describe it in `description`. For every frame list the box of each "
            "subject visible in it: box_2d = [ymin, xmin, ymax, xmax], normalised to 0-1000 on that frame; for a "
            "group (a queue) box the nearest two or three members. Leave a subject out of a frame it is not in.")


def detect(job: Path, key: str, frames: list[dict]) -> dict:
    """Gemini finds the recurring subjects on the original frames: {"subjects": [...], "boxes": {index: {name: box}}}."""
    import io
    parts = []
    for number, frame in enumerate(frames, 1):
        buffer = io.BytesIO()
        frame["original"].save(buffer, "JPEG", quality=85)
        parts.append({"text": f"Frame {number}:"})
        parts.append(winky.inline_bytes(buffer.getvalue(), "image/jpeg"))
    parts.append({"text": detect_prompt(len(frames))})
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
    "game_cg = a 3D game model or CG render; cartoon), colours (the main colours in Chinese, e.g. 深蓝外套、灰色毛领). "
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
    from reskin.overlay import _font
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
        found = detect(job, key, frames)
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
    keep_start, keep_end = assembled["keep"]
    size = tuple(assembled["size"])
    found = []
    for item in plan_row.get("remove") or []:
        if not (isinstance(item, dict) and item.get("box")):
            continue
        a = max(keep_start, float(item.get("start", keep_start))) - keep_start
        b = min(keep_end, float(item.get("end", keep_end))) - keep_start
        times = wipe_times(a, b)
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
        scale = min(1.0, 300 / w)
        tile = (max(2, round(w * scale)), max(2, round(h * scale)))
        number = len(found) + 1
        sheet = _pairs_sheet(columns, tile, 4, f"sheet {number}: {item.get('what', '')} | upper: ORIGINAL (with the "
                                               f"logo) | lower: DELIVERED (logo wiped)")
        path = Path(f"{stem}-{number}.jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(path, quality=90)
        found.append({"sheet": str(path), "what": item.get("what", ""), "times": [_seconds(c[0]) for c in columns],
                      "box": item["box"]})
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
        piece = job / "review" / f"{source_id}-seam-{seam['segment'].replace(':', '-')}.mp4"
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{2 * SEAM_WINDOW:.3f}", "-i", clip,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-an", piece])
        part = winky.inline(piece, "video/mp4")
        part["videoMetadata"] = {"fps": 10}
        answer = winky.gemini([part, {"text": seam_prompt({**seam, "time": seam["time"] - start}, layout)}],
                              schema=SEAM_SCHEMA)
        ledger.add(job, kind="gemini", purpose="seam", key=f"{source_id}:{seam['segment']}", seconds=2 * SEAM_WINDOW)
        found.append({**seam, "answer": answer})
        log(f"{source_id}: seam {seam['segment']} at {seam['time']:.2f} s: "
            + ("consistent" if answer.get("consistent") else f"changes ({', '.join(answer.get('changed') or [])})"))
    return found


def segment_for(time_a: float, time_b: float, assembled: dict) -> list[str]:
    hits = [s["id"] for s in assembled["segments"] if s["start"] - .25 <= time_b and time_a <= s["end"] + .25]
    return hits or [assembled["segments"][-1]["id"]]


TRIAGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"issues": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"},
        "visible": {"type": "BOOLEAN"},
        "original_same": {"type": "BOOLEAN"},
        "note": {"type": "STRING"}},
        "required": ["n", "visible", "original_same", "note"]}}},
    "required": ["issues"]}


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


def triage(job: Path, key: str, issues: list[dict], sheets: list[Path]) -> None:
    """A second look by Gemini at the stills of each reported issue (is it visible, does the original show the same?):
    a hint for the agent, who still decides on the stills."""
    if not issues or not sheets:
        return
    listing = "\n".join(f"#{i['n']} [{i['severity']}] {i['category']} {i['start']:.2f}-{i['end']:.2f} s: {i['description']}"
                        for i in issues)
    parts = [winky.inline(sheet, "image/jpeg") for sheet in sheets]
    parts.append({"text": (
        "These images show stills for reported defects of an AI-restyled video. For each issue (#n in the grey band): "
        "the upper row is the ORIGINAL, the lower row the RESTYLED video, at t-0.3 s, t and t+0.3 s. Decide per issue: "
        "visible = the described defect can be seen in the lower row; original_same = the upper row shows the same "
        "thing (so it is not a defect of the restyle). Seam issues compare the left and right stills of the lower row. "
        "Write `note` in Chinese, one short sentence.\nIssues:\n" + listing)})
    try:
        answer = winky.gemini(parts, schema=TRIAGE_SCHEMA)
    except (RuntimeError, ValueError) as error:
        log(f"{key}: triage skipped ({error})")
        return
    ledger.add(job, kind="gemini", purpose="triage", key=key, seconds=0)
    verdicts = {row.get("n"): row for row in answer.get("issues") or []}
    for issue in issues:
        row = verdicts.get(issue["n"])
        if row:
            hint = ("likely dismiss: the original shows the same" if row.get("original_same") else
                    "likely real" if row.get("visible") else "not visible on the stills")
            issue["triage"] = {"hint": hint, "note": row.get("note", "")}


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
    if size > 18 * 1024 * 1024:
        raise RuntimeError(f"{source_id}: review proxy is {size / 1e6:.0f} MB, too large to send inline")
    log(f"{source_id}: asking Gemini to review ({size / 1e6:.1f} MB comparison clip, {fps:g} fps)")
    part = winky.inline(clip, "video/mp4")
    part["videoMetadata"] = {"fps": fps}
    answer = winky.gemini([part, {"text": prompt(state, plan_row, assembled, layout,
                                                 known=accepted_notes(job, source_id))}], schema=SCHEMA)
    ledger.add(job, kind="gemini", purpose="review", key=source_id, seconds=assembled["duration"])
    issues = []
    for number, issue in enumerate(answer.get("issues") or [], 1):
        a, b = float(issue.get("start", 0)), float(issue.get("end", issue.get("start", 0)))
        a, b = min(a, b), max(a, b)
        issues.append({"n": number, **issue, "start": round(a, 2), "end": round(b, 2),
                       "segments": segment_for(a, b, assembled), "source": "gemini"})
    def add(**issue):
        issues.append({"n": len(issues) + 1, **issue})
    realism = realism_ranges(answer)
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
        if seam.get("jump") in ("visible", "slight"):
            add(start=seam["time"], end=seam["time"], category="seam_color",
                severity="high" if seam["jump"] == "visible" else "low", where="whole frame", source="measured",
                description=f"接缝 {seam['segment']} 处两段画面差异 {seam['mismatch']}（局部 {seam.get('local', 0)}），画面自身运动只有 {seam['motion']}"
                            f"（{'明显跳变' if seam['jump'] == 'visible' else '轻微跳变'}）",
                segments=[seam["segment"]])
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
    # Gemini close-up of every join.
    for seam in seam_checks(job, source_id, assembled, clip, layout):
        reply = seam["answer"]
        if not reply.get("consistent") and reply.get("severity") != "none":
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
    triage(job, source_id, issues, sheets)
    result = {"source": source_id, "round": number, "output": assembled["output"], "verdict": verdict,
              "summary": answer.get("summary", ""), "realism": realism, "identity": subjects,
              "identity_sheets": [str(p) for p in identity_sheets],
              "wipe_sheets": [entry["sheet"] for entry in wipe_sheet_list], "issues": issues, "compare": str(clip),
              "sheets": [str(p) for p in sheets], "assembled": assembled.get("inputs")}
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
    hint = f" [triage: {issue['triage']['hint']}: {issue['triage']['note']}]" if issue.get("triage") else ""
    return (f"#{issue['n']} [{issue['severity']}] {issue['category']} {issue['start']:.2f}-{issue['end']:.2f} s "
            f"({', '.join(issue['segments'])}; {issue.get('where', '')}; {issue['source']}): {issue['description']}{hint}")


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
    return "\n".join(lines) + "\n"


def stills(job: Path, state: dict, source_id: str, times: list[float], span: float = .3) -> list[Path]:
    """For each time: original (top row) and restyled (bottom row) at t-span, t, t+span."""
    assembled = read(job / "state/assembled" / f"{source_id}.json")
    source = next(r for r in state["sources"] if r["id"] == source_id)
    return [compare_sheet(source["path"], assembled["keep"][0], assembled["output"], assembled["duration"],
                          tuple(assembled["size"]), [("", t)], job / "review/frames" / f"{source_id}-{t:07.2f}.jpg",
                          span=span)
            for t in times]
