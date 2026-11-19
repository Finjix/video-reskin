"""The skill entry point: a small, sequential frame-reference workflow and its job commands."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path

from reskin import job as JOB, lumos, net, runtime, winky
from reskin.common import FFMPEG, ROOT, digest, log, read, run, sha, video_info, write

FPS = 24
MAX_FRAMES = 720               # Seedance's 30 s input maximum
MIN_FRAMES = 48                # Seedance's 2 s input minimum; shorter segments are padded and trimmed back
SLIVER = 12                    # frames: gameplay this short between original stretches is survey timing noise
MAX_RETRIES = 2
TRANSITION_FRAMES = 4          # Gameplay joins only; original/non-game/end-card boundaries are cuts.
SHORT_SIDE = 1080              # working copies at most, deliveries exactly (Seedance 2.5's largest render size)
CRF = 16                       # every intermediate x264 encode
DELIVERY = ["-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv444p"]   # the finished MP4
REVIEW_FPS = 6                 # frames per second Gemini samples from every video
IMAGE_MODEL = "gpt-image-2.6-sunburst"     # on Lumos
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}
UPLOAD_LIMIT = 8_000_000       # bytes: a larger PNG is uploaded as a JPEG
INLINE_LIMIT = 256_000_000     # bytes: Winky's Gemini rejects a larger inline video part (measured)
BUDGET = .93                   # share of INLINE_LIMIT the video bitrate is capped to (container and VBV headroom)
ISSUE_SPAN = .3                # an issue shorter than two spans gets frames at its middle and that far either side
LABEL_FONT = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/arial.ttf"
PLAIN = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                       "\u2212": "-", "\u2026": "...", "\u00d7": "x", "\u00a0": " ", "\u00b0": " deg"})


def obj(**fields):
    return {"type": "OBJECT", "properties": deepcopy(fields), "required": list(fields)}


def arr(item):
    return {"type": "ARRAY", "items": item}


NUM = {"type": "NUMBER"}
STR = {"type": "STRING"}
BOOL = {"type": "BOOLEAN"}
SPAN = obj(start=NUM, end=NUM)
SURVEY = obj(non_game=arr(SPAN), end_card_start=NUM,
             cuts=arr(obj(time=NUM, kind={"type": "STRING", "enum": ["motion", "camera", "hard_cut"]})),
             inventory=arr(obj(id=STR, name=STR, kind=STR, start=NUM, end=NUM, first_clear=NUM)))
FRAMES = obj(frames=arr(obj(at=NUM, covers=arr(STR))), references=arr(STR))
SELECT = obj(references=arr(STR))
COVER = obj(ok=BOOL, covers=arr(STR), note=STR)
TAIL = obj(ok=BOOL, note=STR)
REVIEW = obj(verdict={"type": "STRING", "enum": ["pass", "fail"]}, summary=STR,
             issues=arr(obj(start=NUM, end=NUM, description=STR)))
SURVEY["properties"]["end_card_start"]["description"] = "Seconds; use the full video duration if there is no end card."
SURVEY["properties"]["inventory"]["description"] = "English content entries with unique IDs; all times are source seconds."
FRAMES["properties"]["references"]["description"] = ("IDs of existing generated pictures needed by this segment; "
                                                     "reuse rather than convert duplicates.")
COVER["properties"]["covers"]["description"] = ("Inventory IDs visibly shown in the target style in the result, "
                                                "not merely present in the source.")
TAIL["properties"]["ok"]["description"] = "True only when Image 1 and Image 2 are consecutive views of the same continuous shot."
REVIEW["properties"]["issues"]["description"] = ("Unresolved defects relative to source, in seconds from the comparison "
                                                 "clip start; empty on pass.")


def prompt(kind):
    return read(ROOT / "assets/prompts.json")[kind]


def look(job):
    """The job style's prompts: its target description and its image and video prompts."""
    return prompt("styles")[read(job / JOB.STATE)["style"]]


def style_pictures(job):
    """The style's example pictures (assets/styles/<style>/*.jpg), the look of a source's first converted frame;
    possibly none."""
    return sorted((ROOT / "assets/styles" / read(job / JOB.STATE)["style"]).glob("*.jpg"))


def target_size(width, height):
    """The delivery size: SHORT_SIDE on the short side, the source's aspect, even dimensions."""
    if width >= height:
        return round(SHORT_SIDE * width / height / 2) * 2, SHORT_SIDE
    return SHORT_SIDE, round(SHORT_SIDE * height / width / 2) * 2


def working_size(width, height):
    """The working-copy size: the source size with even dimensions, scaled down to SHORT_SIDE on the short side."""
    if min(width, height) > SHORT_SIDE:
        return target_size(width, height)
    return width // 2 * 2, height // 2 * 2


def aspect_for(width, height):
    """The nearest aspect preset (4K needs one: Lumos offers no 4K for "adaptive")."""
    ratio = width / height
    return min(ASPECTS, key=lambda name: abs(ASPECTS[name] / ratio - 1))


def upload_copy(path):
    """The picture as uploaded: the PNG itself, or a JPEG (quality 95) when the PNG is too large."""
    if path.stat().st_size <= UPLOAD_LIMIT:
        return path
    out = path.with_name(path.stem + ".jpg")
    if not out.is_file() or out.stat().st_mtime < path.stat().st_mtime:
        from PIL import Image
        with Image.open(path) as image:
            image.convert("RGB").save(out, quality=95, subsampling=0)
    return out


def drawtext(text):
    if not LABEL_FONT.is_file():
        return ""
    path = str(LABEL_FONT).replace("\\", "/").replace(":", "\\:")
    return (f",drawtext=fontfile='{path}':text='{text}':x=12:y=12:fontsize=28:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=6")


def ff(args, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    run([FFMPEG, "-v", "error", "-y", *args, partial])
    partial.replace(output)
    return output


def codec(preset="fast"):
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(CRF), "-pix_fmt", "yuv420p"]


def encode(filters, *, preset="fast"):
    return ["-vf", filters, "-an", *codec(preset)]


def fit_inline(args, output, duration):
    """Encode `args` (inputs, filters and mapping) at CRF with the bitrate capped so the clip fits INLINE_LIMIT; a clip
    that still overshoots is encoded again with a lower cap."""
    cap = INLINE_LIMIT * BUDGET * 8 / max(duration, 1 / FPS)       # bits per second
    while True:
        ff([*args, *codec("slow"), "-maxrate", f"{cap:.0f}", "-bufsize", f"{cap:.0f}", "-r", str(FPS)], output)
        size = output.stat().st_size
        if size <= INLINE_LIMIT or cap < 2e6:
            return output
        cap *= INLINE_LIMIT * BUDGET / size


def grid_clip(panels, size, output):
    """Two labelled panels on one clip at `size` each, side by side for portrait, stacked for landscape. A panel is
    {"path", "start", "end", "label"}; both share one clock from their starts."""
    width, height = round(size[0] / 2) * 2, round(size[1] / 2) * 2
    inputs, chains, lengths = [], [], []
    for panel in panels:
        start, end = max(0.0, float(panel["start"])), float(panel["end"])
        inputs += ["-ss", f"{start:.6f}", "-t", f"{max(1 / FPS, end - start):.6f}", "-i", str(panel["path"])]
        lengths.append(max(1 / FPS, end - start))
    longest = max(lengths)
    for index, (panel, length) in enumerate(zip(panels, lengths)):
        chains.append(f"[{index}:v]setpts=PTS-STARTPTS,fps={FPS},scale={width}:{height}:flags=lanczos,setsar=1,"
                      f"tpad=stop_mode=clone:stop_duration={longest - length + .2:.3f},trim=duration={longest:.6f}"
                      f"{drawtext(panel['label'])}[p{index}]")
    stack = "hstack" if height >= width else "vstack"
    graph = ";".join(chains) + f";[p0][p1]{stack}=inputs=2,format=yuv420p[v]"
    return fit_inline([*inputs, "-filter_complex", graph, "-map", "[v]", "-an"], output, longest)


def issue_moments(start, end, last):
    """Three moments that show an issue: its start, middle and end, or for a short one its middle and ISSUE_SPAN
    either side; inside [0, last]."""
    a, b = min(start, end), max(start, end)
    middle = (a + b) / 2
    moments = [a, middle, b] if b - a >= 2 * ISSUE_SPAN else [middle - ISSUE_SPAN, middle, middle + ISSUE_SPAN]
    return [round(min(max(0.0, m), last), 3) for m in moments]


def plain(value):
    """Model text folded to ASCII where a plain form exists: typographic punctuation, symbols and accents."""
    if isinstance(value, str):
        text = unicodedata.normalize("NFKD", value.translate(PLAIN))
        return "".join(c for c in text if not unicodedata.combining(c))
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def ascii_only(value):
    return all(ord(c) < 128 for c in json.dumps(value, ensure_ascii=False))


def ask(kind, schema, context, media=()):
    # All model-facing context must be English; user requests are never copied into prompts.
    if not ascii_only(context):
        raise ValueError("model context must be English ASCII; correct the saved model response")
    parts = [*media, {"text": prompt(kind)}, {"text": json.dumps(context)}]
    for attempt in range(2):
        answer = winky.gemini(parts, schema=schema)
        if not isinstance(answer, dict):
            raise ValueError("expected a structured model response")
        answer = plain(answer)
        if ascii_only(answer):
            return answer
        if not attempt:
            log("gemini answered with non-English text; asking again")
    raise ValueError("model response must use English")


def still(source, frame, out):
    return ff(["-i", source, "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1"], out)


def load(job):
    state = JOB.load(job)
    if state.get("schema") != JOB.SCHEMA:
        raise ValueError("unsupported job schema: init a new job")
    return state


def saved(job, name, default=None):
    path = job / (name + ".json")
    return read(path) if path.is_file() else ({} if default is None else default)


def put(job, name, value):
    write(job / (name + ".json"), value)
    return value


def working(job, sid):
    return job / "work" / f"{sid}-24.mp4"


def reason_text(reason):
    text = (reason or "").strip()
    if not text or not ascii_only(text):
        raise ValueError("--reason needs an English explanation")
    return text


def service_failure(job, key, generation_id, error):
    """Keep a failed generation on record; it does not count as a retry."""
    rows = saved(job, f"service-failures/{key}", [])
    put(job, f"service-failures/{key}", [*rows, {"generation_id": generation_id, "error": str(error)}])


def analyze(job, state, sid):
    source = next(s for s in state["sources"] if s["id"] == sid)
    out = working(job, sid)
    if not out.is_file():
        info = video_info(source["path"])
        width, height = working_size(info["width"], info["height"])
        ff(["-i", source["path"], *encode(f"fps={FPS},scale={width}:{height}:flags=lanczos,setsar=1", preset="slow")],
           out)
    return put(job, f"catalog/{sid}", video_info(out))


def survey_clip(job, sid, info):
    """The working copy itself, or a bitrate-capped copy when it is over the inline limit."""
    path = working(job, sid)
    if path.stat().st_size <= INLINE_LIMIT:
        return path
    out = job / "work" / f"{sid}-survey.mp4"
    if not out.is_file():
        fit_inline(["-i", path, "-an"], out, info["frames"] / FPS)
    return out


def survey(job, state, sid):
    info = saved(job, f"catalog/{sid}") or analyze(job, state, sid)
    if saved(job, f"survey/{sid}"):
        return saved(job, f"survey/{sid}")
    answer = ask("survey", SURVEY, {"duration": info["frames"] / FPS},
                 [winky.inline(survey_clip(job, sid, info), fps=REVIEW_FPS)])
    return put(job, f"survey/{sid}", answer)


def split_run(a, b, candidates):
    """The fewest segments of at most MAX_FRAMES and, when a run needs several, none under MIN_FRAMES. Each cut is
    the latest semantic candidate in the last seven seconds before the limit, else the run is split evenly."""
    result = []
    while a < b:
        count = math.ceil((b - a) / MAX_FRAMES)
        if count == 1:
            end = b
        else:
            hi = min(a + MAX_FRAMES, b - (count - 1) * MIN_FRAMES)
            lo = max(a + MIN_FRAMES, b - (count - 1) * MAX_FRAMES)
            options = [c for c in candidates if max(lo, hi - 7 * FPS) <= c <= hi]
            end = max(options) if options else min(hi, max(lo, a + round((b - a) / count)))
        result.append([a, end])
        a = end
    return result


def build_plan(info, survey):
    total = info["frames"]

    def frame(t):
        return min(total, max(0, round(float(t) * FPS)))
    originals = [[frame(x["start"]), frame(x["end"])] for x in survey["non_game"]]
    card = frame(survey["end_card_start"])
    if card < total:
        originals.append([card, total])
    merged = []
    for a, b in sorted(originals):
        if b <= a:
            continue
        if merged and a - merged[-1][1] < SLIVER:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    if merged and merged[0][0] < SLIVER:
        merged[0][0] = 0
    if merged and total - merged[-1][1] < SLIVER:
        merged[-1][1] = total
    cuts = [frame(x["time"]) for x in survey["cuts"]]
    hard = {frame(x["time"]) for x in survey["cuts"] if x["kind"] == "hard_cut"}
    segments, pos = [], 0
    for a, b in [*merged, [total, total]]:
        for start, end in split_run(pos, a, cuts):
            segments.append({"id": f"K{len(segments) + 1:02d}", "frames": [start, end],
                             "continuous": bool(segments and segments[-1]["frames"][1] == start and start not in hard)})
        pos = b
    return {"total": total, "segments": segments, "originals": merged}


def record(job, sid, seg):
    return saved(job, f"state/{sid}-{seg['id']}")


def predecessor(job, sid, plan, seg):
    """The generation a continuing segment follows, or None."""
    i = plan["segments"].index(seg)
    return record(job, sid, plan["segments"][i - 1]).get("generation_id") if seg["continuous"] and i else None


def check_token(job, sid, plan, seg):
    i = plan["segments"].index(seg)
    previous = record(job, sid, plan["segments"][i - 1]).get("generation_id") if i else None
    return digest([record(job, sid, seg).get("generation_id"), previous, seg["frames"]])


def approved(job, sid, plan, seg):
    r = record(job, sid, seg)
    return bool(r.get("generation_id") and r.get("approved") == check_token(job, sid, plan, seg))


def ready(job, sid, plan, seg):
    for prev in plan["segments"][:plan["segments"].index(seg)]:
        if not approved(job, sid, plan, prev):
            raise ValueError(f"waiting for {sid}:{prev['id']}: check and approve first")


def picture_issue(job, key, original, result, report):
    """Point the agent to the untouched source and generated reference, without a comparison sheet."""
    put(job, f"review/{key}", {**report, "images": {"original": str(Path(original).resolve()),
                                                    "result": str(Path(result).resolve())}})
    return job / "review" / f"{key}.json"


def public(entry):
    return {k: v for k, v in entry.items() if k != "path"}


def tail_check(job, sid, plan, seg, previous):
    """Gemini's same-shot verdict on the predecessor's last frame and this segment's first source frame, kept for
    the predecessor generation it was made for."""
    key = f"{sid}-{seg['id']}"
    image = job / "references" / f"{key}-tail.png"
    tail = saved(job, f"review/{key}-tail")
    if tail.get("previous") == previous and image.is_file():
        return tail
    prev = plan["segments"][plan["segments"].index(seg) - 1]
    result = job / "ai" / f"{sid}-{prev['id']}.mp4"
    still(result, video_info(result)["frames"] - 1, image)
    first = still(working(job, sid), seg["frames"][0], job / "references" / f"{key}-first-source.png")
    # Only shot continuity decides tail use; material quality is reviewed elsewhere.
    tail = ask("tail", TAIL, {"operation": "same_shot_continuity"}, [winky.inline(image), winky.inline(first)])
    return put(job, f"review/{key}-tail", {**tail, "previous": previous})


def select_looks(job, at, full, available):
    """IDs of the earlier generated pictures Gemini picks as the look for converting the frame at `at`."""
    if not available:
        return []
    schema = deepcopy(SELECT)
    schema["properties"]["references"].update(minItems=1, maxItems=min(15, len(available)))
    schema["properties"]["references"]["items"]["enum"] = [r["id"] for r in available]
    ids = ask("frames", schema, {"operation": "select_references", "at": at / FPS,
                                 "available": [public(r) for r in available]},
              [winky.inline(full), *[winky.inline(Path(r["path"])) for r in available]])["references"]
    known = {r["id"] for r in available}
    if not ids or len(ids) > 15 or len(set(ids)) != len(ids) or any(x not in known for x in ids):
        raise ValueError(f"invalid image reference selection (1-15 existing IDs required): {ids!r}; "
                         f"available: {sorted(known)!r}")
    return ids


def convert(job, sid, rid, at, inventory, available):
    """One converted full frame: the image task with its looks, then the strict coverage, logo and text inspection."""
    full = still(working(job, sid), at, job / "references" / f"{rid}-source.png")
    out = job / "references" / f"{rid}.png"
    task = saved(job, f"image-tasks/{rid}")
    if not task:
        needed = {x["id"] for x in inventory}
        available = [r for r in available if needed.intersection(r["covers"])] or available[-1:]
        ids = select_looks(job, at, full, available)
        looks = [Path(next(r for r in available if r["id"] == x)["path"]) for x in ids]
        examples = [] if available else style_pictures(job)
        looks += examples
        uploaded = [lumos.upload(upload_copy(p), "image-to-image") for p in [full, *looks]]
        info = saved(job, f"catalog/{sid}")
        started = lumos.submit_image_to_image(uploaded, look(job)["image"], model=IMAGE_MODEL,
                                              aspect_ratio=aspect_for(info["width"], info["height"]),
                                              resolution="2K", quality="medium")
        task = put(job, f"image-tasks/{rid}", {**started, "dependencies": ids,
                                                **({"style_pictures": [p.name for p in examples]} if examples else {})})
    if not out.is_file():
        try:
            result = lumos.wait(task["generation_id"])
        except lumos.GenerationFailed as error:
            service_failure(job, rid, task["generation_id"], error)
            (job / "image-tasks" / f"{rid}.json").unlink()
            raise RuntimeError(f"{error}; recorded as a service failure: rerun refs to submit again") from None
        net.download(lumos.result_urls(result)[0], out)
    coverage = saved(job, f"references/{rid}-coverage")
    if coverage.get("sha256") != sha(out):
        answer = ask("frames", COVER, {"operation": "inspect_style_coverage", "target_style": look(job)["target"],
                                     "inventory": inventory},
                     [winky.inline(full), winky.inline(out)])
        coverage = put(job, f"references/{rid}-coverage", {**answer, "sha256": sha(out)})
    if not coverage["ok"]:
        evidence = picture_issue(job, rid, full, out, coverage)
        raise ValueError(f"reference {rid} failed: {coverage['note']}; open the original images listed in "
                         f"{evidence}; run refs --redo --segment {sid}:{rid.split('-')[1]}")
    return {"id": rid, "path": str(out), "at": at / FPS, "covers": coverage["covers"],
            "dependencies": task.get("dependencies", []), "generation_id": task["generation_id"]}


def refs(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    key = f"{sid}-{seg['id']}"
    previous = predecessor(job, sid, plan, seg)
    existing = saved(job, f"references/{key}")
    if existing and saved(job, f"references/{key}-dependency").get("previous") == previous:
        return existing
    survey = saved(job, f"survey/{sid}")
    a, b = seg["frames"]
    inventory = [x for x in survey["inventory"] if x["start"] < b / FPS and x["end"] > a / FPS]
    registry = saved(job, f"references/{sid}-registry", [])
    chosen = []
    if previous and tail_check(job, sid, plan, seg, previous)["ok"]:
        chosen.append({"id": key + "-tail", "path": str(job / "references" / f"{key}-tail.png"), "covers": [],
                       "at": a / FPS})
    selection = saved(job, f"references/{key}-frames")
    if not selection:
        selection = put(job, f"references/{key}-frames",
                        ask("frames", FRAMES, {"segment": [a / FPS, b / FPS], "inventory": inventory,
                                               "existing": [public(r) for r in registry]}))
    frames = list(selection["frames"])
    if not chosen:
        frames.insert(0, {"at": a / FPS, "covers": []})
    seen = set()
    for spec in frames:
        at = min(b - 1, max(a, round(spec["at"] * FPS)))
        if at in seen:
            continue
        seen.add(at)
        rid = f"{key}-f{at}"
        entry = next((r for r in registry if r["id"] == rid and Path(r["path"]).is_file()), None)
        if entry is None:
            known = {r["id"] for r in registry}
            entry = convert(job, sid, rid, at, inventory, registry + [x for x in chosen if x["id"] not in known])
            registry = [r for r in registry if r["id"] != rid] + [entry]
            put(job, f"references/{sid}-registry", registry)
        chosen.append(entry)
    ids = selection.get("references", [])
    if len(set(ids)) != len(ids) or any(x not in {r["id"] for r in registry} for x in ids):
        raise ValueError("invalid existing video reference IDs")
    chosen += [next(r for r in registry if r["id"] == rid) for rid in ids if rid not in {r["id"] for r in chosen}]
    missing = {x["id"] for x in inventory} - {x for r in chosen for x in r["covers"]}
    if missing or not chosen or len(chosen) > 30:
        raise ValueError(f"reference coverage incomplete or over 30 images: {sorted(missing)}")
    put(job, f"references/{key}-dependency", {"previous": previous})
    return put(job, f"references/{key}", chosen)


def normalize(job, sid, seg, raw):
    """The generated clip at the segment's frame count: one constant retime without motion warping; short-clip
    padding trimmed."""
    a, b = seg["frames"]
    speed = max(MIN_FRAMES, b - a) / FPS / video_info(raw)["duration"]
    ff(["-i", raw, *encode(f"setpts=PTS*{speed:.10f},fps={FPS},tpad=stop_mode=clone:stop_duration=1"),
        "-frames:v", str(b - a)], job / "ai" / f"{sid}-{seg['id']}.mp4")


def render(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    key = f"{sid}-{seg['id']}"
    r = record(job, sid, seg)
    if r.get("frames") and r["frames"] != seg["frames"]:
        raise ValueError("segment frames changed: explicitly redo this segment")
    if r.get("status") == "done" and approved(job, sid, plan, seg):
        return r
    references = saved(job, f"references/{key}", [])
    previous = predecessor(job, sid, plan, seg)
    if not references or saved(job, f"references/{key}-dependency").get("previous") != previous:
        raise ValueError("run refs first")
    basis = digest([previous, [[x["id"], x.get("generation_id")] for x in references]])
    if r.get("status") == "done":
        if r.get("references") == basis:
            return r
        # Rendered from other references or after another predecessor: a fresh render whose quality retries start
        # again; the earlier attempts stay in the history.
        attempt = {**{k: v for k, v in r.items() if k != "history"}, "superseded": "references changed"}
        r = put(job, f"state/{key}", {"history": [*r.get("history", []), attempt]})
    a, b = seg["frames"]
    clip = job / "segments" / f"{key}.mp4"
    if not clip.is_file():
        ff(["-ss", str(a / FPS), "-i", working(job, sid),
            *encode(f"fps={FPS},trim=end_frame={b - a},tpad=stop_mode=clone:stop_duration=2"),
            "-frames:v", str(max(MIN_FRAMES, b - a))], clip)
    if clip.stat().st_size > 190_000_000:
        raise ValueError("reference clip exceeds upload limit")
    if not r.get("generation_id"):
        uses_tail = references[0]["id"] == f"{key}-tail"
        text = look(job)["video"] + (" " + prompt("continuation") if uses_tail else "")
        started = lumos.submit_video_edit(lumos.upload(clip, "video-to-video"),
                                          [lumos.upload(upload_copy(Path(x["path"])), "video-to-video")
                                           for x in references], text)
        r.update(generation_id=started["generation_id"], status="submitted", prompt=text, frames=seg["frames"],
                 references=basis, after=previous)
        put(job, f"state/{key}", r)
    try:
        result = lumos.wait(r["generation_id"])
    except lumos.GenerationFailed as error:
        service_failure(job, key, r["generation_id"], error)
        put(job, f"state/{key}", {k: v for k, v in r.items() if k not in ("generation_id", "status", "prompt")})
        raise RuntimeError(f"{error}; recorded as a service failure: rerun render to submit again") from None
    raw = job / "raw" / f"{key}-{r['generation_id']}.mp4"
    if not raw.is_file():
        net.download(lumos.result_urls(result)[0], raw)
    normalize(job, sid, seg, raw)
    r.update(status="done")
    return put(job, f"state/{key}", r)


def pieces(plan, upto=None):
    rows = [{"frames": x, "original": True} for x in plan["originals"]]
    rows += [{**s, "original": False} for s in plan["segments"]]
    end = next(s["frames"][1] for s in plan["segments"] if s["id"] == upto) if upto else plan["total"]
    return [r for r in sorted(rows, key=lambda r: r["frames"][0]) if r["frames"][0] < end]


def join_transition_frames(previous, current):
    if previous is None or previous["original"] or current["original"]:
        return 0
    return TRANSITION_FRAMES


def assemble(job, state, sid, plan):
    info = saved(job, f"catalog/{sid}")
    w, h = target_size(info["width"], info["height"])
    clips = []
    previous_row = None
    for row in pieces(plan):
        a, b = row["frames"]
        source = working(job, sid) if row["original"] else job / "ai" / f"{sid}-{row['id']}.mp4"
        out = job / "work/join" / f"{sid}-{a}.mp4"
        if join_transition_frames(previous_row, row) and clips:
            # blend numbers frames from 1 in N: N == count - 1 is the outgoing second-to-last frame, N == 1 the
            # incoming first frame.
            tail = still(clips[-1], video_info(clips[-1])["frames"] - 1, out.with_suffix(".png"))
            head = still(source, 0, out.with_name(out.stem + "-head.png"))
            count = video_info(clips[-1])["frames"]
            outgoing = out.with_name(out.stem + "-outgoing.mp4")
            graph = (f"[0:v]scale={w}:{h},setsar=1[v];[1:v]scale={w}:{h},setsar=1[t];"
                     f"[v][t]blend=all_expr='if(eq(N,{count}-1),A*0.75+B*0.25,if(eq(N,{count}),A*0.5+B*0.5,A))'[o]")
            ff(["-i", clips[-1], "-loop", "1", "-i", head, "-filter_complex", graph, "-map", "[o]", "-an",
                "-frames:v", str(count), "-r", str(FPS), *codec()], outgoing)
            clips[-1] = outgoing
            graph = (f"[0:v]scale={w}:{h},setsar=1[v];[1:v]scale={w}:{h},setsar=1[t];"
                     f"[v][t]blend=all_expr='if(eq(N,1),A*0.5+B*0.5,if(eq(N,2),A*0.75+B*0.25,A))'[o]")
            ff(["-i", source, "-loop", "1", "-i", tail, "-filter_complex", graph, "-map", "[o]", "-an",
                "-frames:v", str(b - a), "-r", str(FPS), *codec()], out)
        else:
            ff(["-ss", str(a / FPS if row["original"] else 0), "-i", source,
                *encode(f"scale={w}:{h},setsar=1,fps={FPS}"), "-frames:v", str(b - a)], out)
        clips.append(out)
        previous_row = row
    manifest = job / "work" / f"{sid}-concat.txt"
    manifest.write_text("".join("file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n"
                                for p in clips), encoding="utf-8")
    original = next(s for s in state["sources"] if s["id"] == sid)
    output = Path(state["delivery_dir"]) / f"{original['name']}_{sid}_{state['style']}.mp4"
    args = ["-f", "concat", "-safe", "0", "-i", manifest]
    if video_info(original["path"])["audio"]:
        args += ["-i", original["path"], "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-af", "apad"]
    count = plan["total"]
    ff([*args, *DELIVERY, "-r", str(FPS), "-t", str(count / FPS), "-movflags", "+faststart"], output)
    if abs(video_info(output)["duration"] - count / FPS) > 1 / FPS + .001:
        raise ValueError("assembled duration differs by more than one frame")
    return output


def review_clip(job, sid, start, end, result, result_start, kind, folder, context):
    info = saved(job, f"catalog/{sid}")
    compare = grid_clip([{"path": working(job, sid), "start": start, "end": end, "label": "ORIGINAL"},
                         {"path": result, "start": result_start, "end": result_start + end - start,
                          "label": "RESULT"}],
                        (info["width"], info["height"]), folder / f"{kind}.mp4")
    answer = ask(kind, REVIEW, context, [winky.inline(compare, fps=REVIEW_FPS)])
    last = max(0.0, end - start - 1 / FPS)
    for issue in answer["issues"]:
        issue["start"], issue["end"] = sorted(min(max(0.0, float(issue[k])), end - start) for k in ("start", "end"))
    if answer["issues"]:
        answer["verdict"] = "fail"      # a pass that lists issues still needs the agent's look
    for index, issue in enumerate(answer["issues"], 1):
        issue["frames"] = []
        for number, moment in enumerate(issue_moments(issue["start"], issue["end"], last), 1):
            original = still(working(job, sid), round((start + moment) * FPS),
                             folder / f"{kind}-issue-{index}-{number}-original.png")
            generated = still(result, round((result_start + moment) * FPS),
                              folder / f"{kind}-issue-{index}-{number}-result.png")
            issue["frames"].append({"at": moment, "original": str(original.resolve()),
                                    "result": str(generated.resolve())})
    return answer


def check(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    r = record(job, sid, seg)
    if r.get("status") != "done":
        raise ValueError("render first")
    if r.get("after") != predecessor(job, sid, plan, seg):
        raise ValueError("the previous segment changed after this render: run refs and render again")
    folder = job / "review" / f"{sid}-{seg['id']}-{r['generation_id']}"
    folder.mkdir(parents=True, exist_ok=True)
    a, b = seg["frames"]
    rows = pieces(plan, seg["id"])
    transition = join_transition_frames(rows[-2] if len(rows) > 1 else None, rows[-1])
    segment = review_clip(job, sid, a / FPS, b / FPS, job / "ai" / f"{sid}-{seg['id']}.mp4", 0, "segment", folder,
                          {"target_style": look(job)["target"]})
    answer = {"token": check_token(job, sid, plan, seg), "seam": None, "segment": segment,
              "verdict": segment["verdict"], "status": "complete",
              "decision": {"transition_frames": transition,
                           "reason": "Four-frame gameplay join; hard cuts at original footage boundaries; "
                                     "no seam review."}}
    return put(job, f"checks/{sid}-{seg['id']}", answer)


def approve(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    c = saved(job, f"checks/{sid}-{seg['id']}")
    token = check_token(job, sid, plan, seg)
    if c.get("token") != token or c.get("verdict") != "pass":
        raise ValueError("approval requires a passed check of the current generation")
    r = record(job, sid, seg)
    r["approved"] = token
    return put(job, f"state/{sid}-{seg['id']}", r)


def redo(job, sid, plan, seg, reason):
    reason = reason_text(reason)
    r = record(job, sid, seg)
    if not r.get("generation_id"):
        raise ValueError("no generation to redo")
    if r.get("retries", 0) >= MAX_RETRIES:
        raise ValueError("two retries exhausted: compare the three versions and use select-best")
    history = r.get("history", []) + [{k: v for k, v in r.items() if k != "history"}]
    return put(job, f"state/{sid}-{seg['id']}", {"retries": r.get("retries", 0) + 1, "history": history,
                                                 "reason": reason})


def select_best(job, sid, plan, seg, generation_id, reason):
    ready(job, sid, plan, seg)
    reason = reason_text(reason)
    r = record(job, sid, seg)
    attempts = [*r.get("history", []), {k: v for k, v in r.items() if k != "history"}]
    finished = {x.get("generation_id"): x for x in attempts if x.get("status") == "done" and not x.get("superseded")}
    if r.get("retries", 0) < MAX_RETRIES or len(finished) < MAX_RETRIES + 1:
        raise ValueError("select-best requires the initial result and two completed quality retries")
    if generation_id not in finished:
        raise ValueError("select a completed generation of the current references")
    raw = job / "raw" / f"{sid}-{seg['id']}-{generation_id}.mp4"
    if not raw.is_file():
        raise ValueError("selected raw result is missing")
    normalize(job, sid, seg, raw)
    put(job, f"review/{sid}-{seg['id']}-best-selection",
        {"generation_id": generation_id, "reason": reason, "attempts": attempts,
         "check_at_selection": saved(job, f"checks/{sid}-{seg['id']}")})
    r.update(generation_id=generation_id, status="done",
             history=[x for x in attempts if x.get("generation_id") != generation_id],
             acceptance={"mode": "best_available", "reason": reason})
    put(job, f"state/{sid}-{seg['id']}", r)
    r["approved"] = check_token(job, sid, plan, seg)
    return put(job, f"state/{sid}-{seg['id']}", r)


def reset_refs(job, sid, plan, seg):
    key = f"{sid}-{seg['id']}"
    for other in plan["segments"]:
        used = [r["id"] for r in saved(job, f"references/{sid}-{other['id']}", []) if r["id"].startswith(key + "-f")]
        if other is not seg and used:
            raise ValueError(f"{sid}:{other['id']} uses {used}: redo that segment's references first")
    for task in (job / "image-tasks").glob(key + "-f*.json"):
        output = job / "references" / (task.stem + ".png")
        if output.is_file():
            archive = job / "reference-history" / f"{task.stem}-{sha(output)[:12]}.png"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output, archive)
            output.unlink()
        task.unlink()
    for path in (job / "references").glob(key + "-f*-coverage.json"):
        path.unlink()
    for suffix in ("", "-frames", "-dependency"):
        (job / "references" / (key + suffix + ".json")).unlink(missing_ok=True)
    registry = saved(job, f"references/{sid}-registry", [])
    put(job, f"references/{sid}-registry", [r for r in registry if not r["id"].startswith(key + "-f")])


def segment_command(command, job, sid, plan, seg, args):
    if args.redo:
        if command != "refs":
            raise ValueError("--redo is only for refs")
        ready(job, sid, plan, seg)
        reset_refs(job, sid, plan, seg)
    if command == "redo":
        return redo(job, sid, plan, seg, args.reason)
    if command == "select-best":
        if not args.generation_id:
            raise ValueError("select-best requires --generation-id and --reason")
        return select_best(job, sid, plan, seg, args.generation_id, args.reason)
    return {"refs": refs, "render": render, "check": check, "approve": approve}[command](job, sid, plan, seg)


def source_command(command, job, state, sid, args, selected):
    if command == "analyze":
        return analyze(job, state, sid)
    if command == "survey":
        return survey(job, state, sid)
    if command == "plan":
        return put(job, f"plans/{sid}", build_plan(saved(job, f"catalog/{sid}"), saved(job, f"survey/{sid}")))
    plan = saved(job, f"plans/{sid}")
    if not plan:
        raise ValueError("run plan first")
    if command == "assemble":
        if not all(approved(job, sid, plan, s) for s in plan["segments"]):
            raise ValueError("all segments must be approved before delivery")
        return str(assemble(job, state, sid, plan))
    if command == "status":
        return [{"segment": s["id"], "approved": approved(job, sid, plan, s), **record(job, sid, s)}
                for s in plan["segments"]]
    wanted = args.segment or next((x for x in selected if x.startswith(sid + ":")), None)
    if wanted:
        seg = next((s for s in plan["segments"] if f"{sid}:{s['id']}" == wanted), None)
        if seg is None:
            raise ValueError(f"unknown segment {wanted}")
    else:
        seg = next((s for s in plan["segments"] if not approved(job, sid, plan, s)), None)
        if seg is None:
            return "all segments approved"
    return segment_command(command, job, sid, plan, seg, args)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["auth", "login", "runtime", "init", "analyze", "survey", "plan", "refs",
                                       "render", "check", "approve", "redo", "select-best", "assemble", "status"])
    p.add_argument("--job-dir", type=Path)
    p.add_argument("--work-order", type=Path)
    p.add_argument("--segment")
    p.add_argument("--generation-id")
    p.add_argument("--only", nargs="*")
    p.add_argument("--reason", help="English explanation (redo, select-best)")
    p.add_argument("--redo", action="store_true", help="explicitly redo this segment's reference images")
    args, extra = p.parse_known_args()
    if args.command in ("auth", "login", "runtime"):
        from reskin.account import main as account_main
        return account_main()
    try:
        if extra:
            raise ValueError(f"unsupported arguments: {extra}")
        if args.command == "init":
            if not args.work_order:
                raise ValueError("--work-order is required")
            order = (json.loads(sys.stdin.buffer.read().decode("utf-8-sig")) if str(args.work_order) == "-"
                     else read(args.work_order))
            job, _ = JOB.init(order, args.job_dir)
            print(f"job_dir: {job}")
            return
        if args.command in ("approve", "redo", "select-best") and not args.segment:
            raise ValueError("--segment is required for approve, redo and select-best")
        if not args.job_dir:
            raise ValueError("--job-dir is required")
        runtime.ensure("media")
        job = args.job_dir.resolve()
        state = load(job)
        errors = []
        selected = args.only or []
        for source in state["sources"]:
            sid = source["id"]
            if selected and not any(x == sid or x.startswith(sid + ":") for x in selected):
                continue
            if args.segment and not args.segment.startswith(sid + ":"):
                continue
            try:
                result = source_command(args.command, job, state, sid, args, selected)
                print(json.dumps({"source": sid, "result": result}, ensure_ascii=False))
            except (ValueError, RuntimeError, OSError, KeyError) as e:
                errors.append(f"{sid}: {e}")
        if errors:
            raise ValueError("; ".join(errors))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
