"""Command-line entry point and the style workflow: survey, plan, references, render, review, assembly."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

from reskin import job as JOB, lumos, net, review, runtime
from reskin.common import FFMPEG, FPS, ROOT, digest, read, run, sha, video_info, write

MAX_FRAMES = 720               # Seedance's 30 s input maximum
MIN_FRAMES = lumos.VIDEO_MIN_SECONDS * FPS
SLIVER = 12                    # frames: gameplay this short between original stretches is survey timing noise
MAX_RETRIES = 2
TRANSITION_FRAMES = 4          # Gameplay joins only; original/non-game/end-card boundaries are cuts.
SHORT_SIDE = 1080              # working copies at most, deliveries exactly (Seedance 2.5's largest render size)
CRF = 16                       # every intermediate x264 encode
DELIVERY = ["-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p"]
IMAGE_MODEL = "gpt-image-2.6-sunburst"     # on Lumos
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}
UPLOAD_LIMIT = 8_000_000       # bytes: a larger PNG is uploaded as a JPEG


def prompt(kind):
    return read(ROOT / "assets/prompts.json")[kind]


def look(job):
    """The job style's prompts: its target description and its image and video prompts."""
    return prompt("styles")[read(job / JOB.STATE)["style"]]


def style_picture(job):
    """The style's example picture (assets/styles/<style>.jpg), the look of a source's first converted frame; or
    None."""
    path = ROOT / "assets/styles" / (read(job / JOB.STATE)["style"] + ".jpg")
    return path if path.is_file() else None


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


def ascii_only(value):
    return all(ord(c) < 128 for c in json.dumps(value, ensure_ascii=False))


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


def frame_at(seconds, count):
    if not math.isfinite(seconds) or not 0 <= seconds < count / FPS:
        raise ValueError(f"time must be within the clip (0 to {count / FPS:g} seconds)")
    return min(count - 1, round(seconds * FPS))


def survey(job, state, sid, at=None):
    info = saved(job, f"catalog/{sid}") or analyze(job, state, sid)
    source = working(job, sid)
    folder = job / "review" / f"{sid}-survey"
    if at is not None:
        frame = frame_at(at, info["frames"])
        return {"at": frame / FPS, "image": str(still(source, frame, folder / f"frame-{frame}.png"))}
    return {"source_video": str(source), "duration": info["frames"] / FPS,
            **review.sheets(source, 0, info["frames"], (info["width"], info["height"]), folder)}


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


def save_plan(job, sid, survey=None):
    info = saved(job, f"catalog/{sid}")
    if not info:
        raise ValueError("run analyze first")
    key = f"survey/{sid}"
    previous = saved(job, key)
    if survey is None:
        if not previous:
            raise ValueError("run survey and submit its findings with plan --input")
        survey = previous
    if not isinstance(survey, dict) or not all(k in survey for k in ("non_game", "end_card_start", "cuts", "inventory")):
        raise ValueError("survey needs non_game, end_card_start, cuts and inventory")
    ids = [item["id"] for item in survey["inventory"]]
    if len(ids) != len(set(ids)):
        raise ValueError("survey inventory IDs must be unique")
    plan = build_plan(info, survey)
    if previous and previous != survey and any(
        next((job / folder).glob(f"{sid}-K*.json"), None)
        for folder in ("state", "image-tasks", "references")
    ):
        raise ValueError("this source already has segment data; use a new job to change its survey")
    put(job, key, survey)
    return put(job, f"plans/{sid}", plan)


def record(job, sid, seg):
    return saved(job, f"state/{sid}-{seg['id']}")


def predecessor(job, sid, plan, seg):
    """The generation a continuing segment follows, or None."""
    i = plan["segments"].index(seg)
    return record(job, sid, plan["segments"][i - 1]).get("generation_id") if seg["continuous"] and i else None


def check_token(job, sid, plan, seg, generation_id=None):
    i = plan["segments"].index(seg)
    previous = record(job, sid, plan["segments"][i - 1]).get("generation_id") if i else None
    return digest([generation_id or record(job, sid, seg).get("generation_id"), previous, seg["frames"]])


def approved(job, sid, plan, seg):
    r = record(job, sid, seg)
    return bool(r.get("generation_id") and r.get("approved") == check_token(job, sid, plan, seg))


def ready(job, sid, plan, seg):
    for prev in plan["segments"][:plan["segments"].index(seg)]:
        if not approved(job, sid, plan, prev):
            raise ValueError(f"waiting for {sid}:{prev['id']}: check and approve first")


def tail_images(job, sid, plan, seg, previous):
    prev = plan["segments"][plan["segments"].index(seg) - 1]
    key = f"{sid}-{seg['id']}"
    folder = job / "review" / f"{key}-tail-{previous}"
    tail = folder / f"{key}-tail.png"
    first = folder / "first-source.png"
    if not tail.is_file():
        still(job / "ai" / f"{sid}-{prev['id']}.mp4", prev["frames"][1] - prev["frames"][0] - 1, tail)
    if not first.is_file():
        still(working(job, sid), seg["frames"][0], first)
    return {"tail": str(tail), "first": str(first)}


def image_paths(job, rid):
    folder = job / "references"
    return {"original": str(folder / f"{rid}-source.png"), "result": str(folder / f"{rid}.png")}


def convert(job, sid, rid, at, inventory, available):
    """Generate one full-frame reference and return its inspection images or accepted coverage."""
    images = image_paths(job, rid)
    full, out = Path(images["original"]), Path(images["result"])
    if not full.is_file():
        still(working(job, sid), at, full)
    task = saved(job, f"image-tasks/{rid}")
    if not task:
        needed = {x["id"] for x in inventory}
        selected = []
        for ref in reversed(available):
            if needed.intersection(ref["covers"]):
                selected.append(ref)
                needed.difference_update(ref["covers"])
            if not needed or len(selected) == 15:
                break
        selected = selected or available[-1:]
        ids = [ref["id"] for ref in selected]
        looks = [Path(ref["path"]) for ref in selected]
        example = None if available else style_picture(job)
        looks += [example] if example else []
        uploaded = [lumos.upload(upload_copy(p), "image-to-image") for p in [full, *looks]]
        info = saved(job, f"catalog/{sid}")
        started = lumos.submit_image_to_image(uploaded, look(job)["image"], model=IMAGE_MODEL,
                                              aspect_ratio=aspect_for(info["width"], info["height"]),
                                              resolution="2K", quality="medium")
        task = put(job, f"image-tasks/{rid}", {**started, "dependencies": ids,
                                                **({"style_picture": example.name} if example else {})})
    if not out.is_file():
        try:
            result = lumos.wait(task["generation_id"])
        except lumos.GenerationFailed as error:
            service_failure(job, rid, task["generation_id"], error)
            (job / "image-tasks" / f"{rid}.json").unlink()
            raise RuntimeError(f"{error}; recorded as a service failure: rerun refs to submit again") from None
        net.download(lumos.result_urls(result)[0], out)
    coverage = saved(job, f"references/{rid}-coverage")
    entry = {"id": rid, "path": str(out), "at": at / FPS, "dependencies": task.get("dependencies", []),
             "generation_id": task["generation_id"]}
    if coverage.get("sha256") != sha(out):
        return {**entry, "status": "inspect_reference", "target_style": look(job)["target"],
                "images": images, "inventory": inventory}
    if not coverage["ok"]:
        raise ValueError(f"reference {rid} failed: {coverage['note']}; run refs --redo")
    return {**entry, "covers": coverage["covers"]}


def accept_reference(job, frame_ids, generation_id, covers, inventory):
    for rid in frame_ids:
        if saved(job, f"image-tasks/{rid}").get("generation_id") != generation_id:
            continue
        images = image_paths(job, rid)
        if not all(Path(p).is_file() for p in images.values()):
            raise ValueError("run refs to prepare the reference images first")
        if not set(covers) <= {item["id"] for item in inventory}:
            raise ValueError("--covers must list inventory IDs for this segment")
        key = f"references/{rid}-coverage"
        old = saved(job, key)
        checksum = sha(Path(images["result"]))
        if old.get("sha256") == checksum:
            if not old["ok"]:
                raise ValueError(f"reference {rid} failed: run refs --redo")
            if set(old["covers"]) != set(covers):
                raise ValueError(f"reference {rid} already has recorded coverage")
            return
        put(job, key, {"ok": True, "covers": list(dict.fromkeys(covers)), "sha256": checksum})
        return
    raise ValueError("unknown reference generation; use the generation_id printed by refs")


def refs(job, sid, plan, seg, selection=None, accept=None, covers=()):
    ready(job, sid, plan, seg)
    key = f"{sid}-{seg['id']}"
    previous = predecessor(job, sid, plan, seg)
    existing = saved(job, f"references/{key}")
    if (selection is None and not accept and existing
            and saved(job, f"references/{key}-dependency").get("previous") == previous
            and all(Path(r["path"]).is_file() for r in existing)):
        return existing
    survey = saved(job, f"survey/{sid}")
    a, b = seg["frames"]
    inventory = [x for x in survey["inventory"] if x["start"] < b / FPS and x["end"] > a / FPS]
    registry_key = f"references/{sid}-registry"
    registry = {r["id"]: r for r in saved(job, registry_key, []) if Path(r["path"]).is_file()}
    selection_key = f"references/{key}-frames"
    supplied = selection is not None
    if supplied:
        if not isinstance(selection, dict):
            raise ValueError("reference selection must be a JSON object")
        selection = {**selection, "previous": previous}
    else:
        selection = saved(job, selection_key)
    if not selection or selection.get("previous") != previous:
        if accept:
            raise ValueError("select reference frames first")
        return {"status": "select_frames", "source_range": [a / FPS, b / FPS], "inventory": inventory,
                "references": list(registry.values()),
                "continuity_images": tail_images(job, sid, plan, seg, previous) if previous else None}

    use_tail = selection.get("use_tail", False)
    if not isinstance(use_tail, bool) or use_tail and not previous:
        raise ValueError("use_tail requires a continuous predecessor")
    frames = selection.get("frames", [])
    ids = selection.get("references", [])
    if not isinstance(frames, list) or not isinstance(ids, list):
        raise ValueError("frames and references must be lists")
    if any(not isinstance(rid, str) or rid not in registry for rid in ids) or len(set(ids)) != len(ids):
        raise ValueError("invalid existing video reference IDs")
    times = [round(float(spec["at"] if isinstance(spec, dict) else spec) * FPS) for spec in frames]
    if any(not a <= at < b for at in times):
        raise ValueError("reference times must be within the segment on the source timeline")
    if not use_tail:
        times.insert(0, a)
    frame_ids = {f"{key}-f{at}": at for at in times}
    if len(set(ids) | frame_ids.keys()) + use_tail > 30:
        raise ValueError("a segment supports at most 30 reference images")
    if supplied:
        (job / f"references/{key}.json").unlink(missing_ok=True)
        put(job, selection_key, selection)
    if accept:
        accept_reference(job, frame_ids, accept, covers, inventory)

    chosen = []
    if use_tail:
        tail = tail_images(job, sid, plan, seg, previous)["tail"]
        chosen.append({"id": key + "-tail", "path": tail, "covers": [],
                       "at": a / FPS})
    for rid, at in frame_ids.items():
        entry = registry.get(rid)
        if entry is None:
            available = [*registry.values(), *(ref for ref in chosen if ref["id"] not in registry)]
            entry = convert(job, sid, rid, at, inventory, available)
            if entry.get("status") == "inspect_reference":
                return entry
            registry[rid] = entry
            put(job, registry_key, list(registry.values()))
        chosen.append(entry)
    chosen += [registry[rid] for rid in ids if rid not in frame_ids]
    missing = {x["id"] for x in inventory} - {x for r in chosen for x in r["covers"]}
    if missing:
        raise ValueError(f"reference coverage incomplete: {sorted(missing)}; update the selection with refs --input")
    put(job, f"references/{key}-dependency", {"previous": previous})
    return put(job, f"references/{key}", chosen)


def normalize(job, sid, seg, raw, *, input_frames=None, output=None):
    """The generated clip at the segment's frame count: one constant retime without motion warping; short-clip
    padding trimmed."""
    a, b = seg["frames"]
    speed = (input_frames or max(MIN_FRAMES, b - a)) / FPS / video_info(raw)["duration"]
    return ff(["-i", raw, *encode(f"setpts=PTS*{speed:.10f},fps={FPS},tpad=stop_mode=clone:stop_duration=1"),
               "-frames:v", str(b - a)], output or job / "ai" / f"{sid}-{seg['id']}.mp4")


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
    if r.get("status") == "done" and r.get("references") == basis:
        return r
    if r.get("generation_id") and r.get("references") != basis:
        attempt = {**{k: v for k, v in r.items() if k != "history"}, "superseded": "references changed"}
        r = put(job, f"state/{key}", {"history": [*r.get("history", []), attempt]})
    a, b = seg["frames"]
    clip = job / "segments" / f"{key}.mp4"
    input_frames = max(MIN_FRAMES, b - a)
    if not clip.is_file() or (not r.get("generation_id") and video_info(clip)["frames"] != input_frames):
        ff(["-ss", str(a / FPS), "-i", working(job, sid),
            *encode(f"fps={FPS},trim=end_frame={b - a},tpad=stop_mode=clone:stop_duration={lumos.VIDEO_MIN_SECONDS}"),
            "-frames:v", str(input_frames)], clip)
    if clip.stat().st_size > 190_000_000:
        raise ValueError("reference clip exceeds upload limit")
    if not r.get("generation_id"):
        uses_tail = references[0]["id"] == f"{key}-tail"
        text = look(job)["video"] + (" " + prompt("continuation") if uses_tail else "")
        started = lumos.submit_video_edit(lumos.upload(clip, "video-to-video"),
                                          [lumos.upload(upload_copy(Path(x["path"])), "video-to-video")
                                           for x in references], text)
        r.update(generation_id=started["generation_id"], status="submitted", prompt=text, frames=seg["frames"],
                 references=basis, after=previous, input_frames=input_frames)
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
    normalize(job, sid, seg, raw, input_frames=r.get("input_frames"))
    r.update(status="done")
    return put(job, f"state/{key}", r)


def pieces(plan):
    rows = [{"frames": x, "original": True} for x in plan["originals"]]
    rows += [{**s, "original": False} for s in plan["segments"]]
    return sorted(rows, key=lambda r: r["frames"][0])


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
        args += ["-i", original["path"], "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-profile:a", "aac_low",
                 "-ar", "48000", "-ac", "2", "-b:a", "128k", "-af", "apad"]
    count = plan["total"]
    ff([*args, *DELIVERY, "-r", str(FPS), "-t", str(count / FPS), "-movflags", "+faststart"], output)
    if abs(video_info(output)["duration"] - count / FPS) > 1 / FPS + .001:
        raise ValueError("assembled duration differs by more than one frame")
    return output


def check(job, sid, plan, seg, generation_id=None, at=None):
    ready(job, sid, plan, seg)
    r = record(job, sid, seg)
    if r.get("status") != "done":
        raise ValueError("render first")
    if r.get("after") != predecessor(job, sid, plan, seg):
        raise ValueError("the previous segment changed after this render: run refs and render again")
    key = f"{sid}-{seg['id']}"
    current = r["generation_id"]
    generation_id = generation_id or current
    token = check_token(job, sid, plan, seg, generation_id)
    folder = job / "review" / f"{key}-{generation_id}-{token[:12]}"
    source = working(job, sid)
    result = job / "ai" / f"{key}.mp4"
    if generation_id != current:
        prior = next((item for item in r.get("history", []) if item.get("generation_id") == generation_id), {})
        if prior.get("status") != "done" or prior.get("superseded"):
            raise ValueError("select a completed generation of this segment")
        result = folder / "result.mp4"
        if not result.is_file():
            normalize(job, sid, seg, job / "raw" / f"{key}-{generation_id}.mp4", input_frames=prior.get("input_frames"), output=result)
    a, b = seg["frames"]
    if at is not None:
        frame = frame_at(at, b - a)
        original = still(source, a + frame, folder / f"frame-{frame}-source.png")
        generated = still(result, frame, folder / f"frame-{frame}-result.png")
        return {"at": frame / FPS, "images": {"original": str(original), "result": str(generated)}}
    info = saved(job, f"catalog/{sid}")
    evidence = review.sheets(source, a, b, (info["width"], info["height"]), folder, result)
    data = {"token": token, "generation_id": generation_id, "source_video": str(source),
            "source_range": [a / FPS, b / FPS], "result_video": str(result), "target_style": look(job)["target"], **evidence}
    if generation_id == current:
        put(job, f"checks/{key}", data)
    return data


def approve(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    c = saved(job, f"checks/{sid}-{seg['id']}")
    token = check_token(job, sid, plan, seg)
    r = record(job, sid, seg)
    sheets = c.get("sheets")
    if r.get("status") != "done" or c.get("token") != token or not sheets or not all(Path(p).is_file() for p in sheets):
        raise ValueError("run check and review the current generation before approving")
    r["approved"] = token
    return put(job, f"state/{sid}-{seg['id']}", r)


def redo(job, sid, plan, seg, reason):
    reason = reason_text(reason)
    r = record(job, sid, seg)
    if not r.get("generation_id"):
        raise ValueError("no generation to redo")
    if r.get("retries", 0) >= MAX_RETRIES:
        raise ValueError("two retries exhausted: compare the three versions and use select-best")
    history = [*r.get("history", []), {**{k: v for k, v in r.items() if k != "history"}, "review_note": reason}]
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
    normalize(job, sid, seg, raw, input_frames=finished[generation_id].get("input_frames"))
    put(job, f"review/{sid}-{seg['id']}-best-selection",
        {"generation_id": generation_id, "reason": reason, "attempts": attempts,
         "check_at_selection": saved(job, f"checks/{sid}-{seg['id']}")})
    r.update(generation_id=generation_id, status="done", input_frames=finished[generation_id].get("input_frames"),
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
    for suffix in ("", "-dependency"):
        (job / "references" / (key + suffix + ".json")).unlink(missing_ok=True)
    registry = saved(job, f"references/{sid}-registry", [])
    put(job, f"references/{sid}-registry", [r for r in registry if not r["id"].startswith(key + "-f")])


def segment_command(command, job, sid, plan, seg, args):
    if args.redo:
        if command != "refs":
            raise ValueError("--redo is only for refs")
        ready(job, sid, plan, seg)
        reset_refs(job, sid, plan, seg)
    if command == "refs":
        selection = read(args.input) if args.input else None
        return refs(job, sid, plan, seg, selection, args.accept, args.covers)
    if command == "check":
        return check(job, sid, plan, seg, args.generation_id, args.at)
    if command == "redo":
        return redo(job, sid, plan, seg, args.reason)
    if command == "select-best":
        if not args.generation_id:
            raise ValueError("select-best requires --generation-id and --reason")
        return select_best(job, sid, plan, seg, args.generation_id, args.reason)
    return {"render": render, "approve": approve}[command](job, sid, plan, seg)


def source_command(command, job, state, sid, args, selected):
    if command == "analyze":
        return analyze(job, state, sid)
    if command == "survey":
        return survey(job, state, sid, args.at)
    if command == "plan":
        return save_plan(job, sid, read(args.input) if args.input else None)
    plan = saved(job, f"plans/{sid}")
    if not plan:
        raise ValueError("run plan first")
    if command == "assemble":
        if not all(approved(job, sid, plan, s) for s in plan["segments"]):
            raise ValueError("all segments must be approved before delivery")
        return str(assemble(job, state, sid, plan))
    if command == "status":
        return [{**record(job, sid, s), "segment": s["id"], "approved": approved(job, sid, plan, s)}
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
    p.add_argument("--input", type=Path, help="survey findings for plan, or frame selection for refs")
    p.add_argument("--segment")
    p.add_argument("--generation-id")
    p.add_argument("--at", type=float, help="full-size frame at seconds from the start of the source (survey) or segment (check)")
    p.add_argument("--only", nargs="*", help="source/segment IDs, or V01 V02 ... for element variants")
    p.add_argument("--accept", help="record the inspected reference's generation ID (refs)")
    p.add_argument("--covers", nargs="*", default=[], help="inventory IDs visible in the accepted reference")
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
        if not args.job_dir:
            raise ValueError("--job-dir is required")
        job = args.job_dir.resolve()
        state = load(job)
        if state["style"] == "elements":
            from reskin import elements
            runtime.ensure("media")
            return elements.dispatch(args, job, state)
        if args.command in ("approve", "redo", "select-best") and not args.segment:
            raise ValueError("--segment is required for approve, redo and select-best")
        if args.input and not (
            (args.command == "plan" and len(args.only or []) == 1 and ":" not in args.only[0])
            or (args.command == "refs" and args.segment)
        ):
            raise ValueError("--input requires plan --only <source> or refs --segment <segment>")
        if args.accept and (args.command != "refs" or not args.segment or args.input or args.redo):
            raise ValueError("--accept requires refs --segment <segment> after inspecting the images")
        if args.covers and not args.accept:
            raise ValueError("--covers requires --accept")
        if args.at is not None and args.command not in ("survey", "check"):
            raise ValueError("--at applies to survey or check")
        runtime.ensure("media")
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
