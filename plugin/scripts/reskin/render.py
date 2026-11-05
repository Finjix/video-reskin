"""Seedance 2.5 video editing on Lumos, segment by segment: cut the clip, upload, submit, poll, download, check.

Segments inside one camera shot form a chain: the first starts from its styled keyframe, every later one from the
previous result itself. Its clip begins with the previous result's own last second (on the source clock) and its
reference picture is the first of those frames, so Seedance continues the frames it already made instead of
re-imagining the scene from one picture; `assemble` switches between the two inside that second, where both show the
same pixels. A chain renders one segment after another; separate chains (other shots, other sources) in parallel,
the longest first.

Every segment's inputs (frames, reference picture, attempt) are hashed; a re-run reuses finished segments, resumes
generations still running on Lumos, and renders only what changed or was sent back with `redo` (which also renders
the rest of that chain again, since each segment starts from the one before). That is why each segment with a
successor is checked (`gate`) before the next one starts: a flaw held there costs one segment, not the chain.
Every submission goes into the spend ledger with its cause.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reskin import ledger, lumos
from reskin.common import FFMPEG, PROMPTS, digest, log, read, run, sha, video_info, write
from reskin.job import RESOLUTION
from reskin.prompts import compose, is_wiped

POLL_SECONDS = 15
GENERATION_TIMEOUT = 30 * 60
MIN_WIPE = .1            # seconds: a box touching a segment for less (a rounding sliver) does not count


def state_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}.json"


def redo_log(job: Path) -> dict:
    path = job / "config/redo.json"
    return read(path) if path.is_file() else {}


def corrections(job: Path, key: str, seg: dict) -> str:
    """The corrective sentences of a segment (`redo --prompt`), one per line: those of its `like` twin's segment over
    the same seconds first (fixes found on the twin, until `redo --replace-prompt` here drops them), then its own."""
    log = redo_log(job)
    own = log.get(key, {})
    sentences = []
    if seg.get("like") and own.get("inherit", True):
        sentences += [s for s in (log.get(seg["like"], {}).get("prompt") or "").split("\n") if s]
    sentences += [s for s in (own.get("prompt") or "").split("\n") if s and s not in sentences]
    return "\n".join(sentences)


def twin_waits(job: Path, state: dict, plan: dict, only: set[str] | None = None) -> dict[str, str]:
    """`like` sources whose twin is not settled yet: not delivered, not reviewed since its last change, or with redos
    queued. They render afterwards, so the twin's fixes (corrective sentences, split times) reach them before they
    are paid for. A source named in `only` renders anyway."""
    from reskin import assemble as ASSEMBLE, review as REVIEW
    log, waits = redo_log(job), {}
    for sid, row in plan["sources"].items():
        twin = row.get("like")
        if not twin or twin not in plan["sources"]:
            continue
        if only and any(k == sid or k.startswith(f"{sid}:") for k in only):
            continue
        trow = plan["sources"][twin]
        pending = []
        for seg in (s for s in trow["segments"] if not s.get("live")):
            key = f"{twin}:{seg['id']}"
            record = read(state_path(job, key)) if state_path(job, key).is_file() else {}
            if log.get(key, {}).get("attempt", 1) > record.get("attempt", 1):
                pending.append(seg["id"])
        if pending:
            waits[sid] = f"waits for the redos of {twin} ({', '.join(pending)}) and their review"
            continue
        assembled = ASSEMBLE.current(job, state, twin, trow)
        last = REVIEW.last_round(job, twin)
        if assembled is None or not last or last.get("assembled") != assembled.get("inputs"):
            waits[sid] = f"waits until {twin} is delivered and reviewed"
    return waits


def chains(plan_row: dict) -> list[list[dict]]:
    """Segments grouped by camera shot: a new chain starts at the first segment and after every hard cut."""
    groups: list[list[dict]] = []
    for seg in plan_row["segments"]:
        if groups and seg["seam"] == "continuous":
            groups[-1].append(seg)
        else:
            groups.append([seg])
    return groups


def clean_boxes(plan_row: dict, start: float, end: float, logos: dict[int, dict] | None = None) -> list[list]:
    """`remove` items with `"wipe": "before"`, wiped from the clip Seedance gets during [start, end) of the source, as
    [x0, y0, x1, y1, a, b, "glyph:<mask hash>"] with a, b in seconds into the clip (masks from logos.masks). Not the
    default: Seedance kept every wiped corner as a blurred patch."""
    boxes = []
    for n, item in enumerate(plan_row.get("remove") or []):
        if not is_wiped(item):
            continue
        a, b = max(start, float(item.get("start", 0))), min(end, float(item.get("end", float("inf"))))
        if b - a >= MIN_WIPE:
            if not logos or n not in logos:
                raise ValueError(f"remove item {n} ({item.get('what', '')}): its logo mask is missing")
            boxes.append([*[float(v) for v in item["box"]], round(a - start, 3), round(b - start, 3),
                          f"glyph:{logos[n]['hash']}"])
    return boxes


def lead_logos(plan_row: dict, seg: dict, logos: dict[int, dict] | None) -> list[list]:
    """Boxed `remove` items left for Seedance to remove (the default, `"wipe": "none"`) on screen during a chained
    segment's lead (its first frames, taken from the previous result, which has them removed): their original pixels
    are painted into those frames, [x0, y0, x1, y1, a, b, "keep:<mask hash>"] with a, b in seconds into the clip.
    Without them the logo 'appeared' where the clip's own part starts and Seedance kept it as new content
    (GOF3D10012 K02, all three corners); in a first segment, where the logo shows from the first frame and the
    reference picture has none, it removed every one."""
    fps = plan_row["fps"]
    start, end = seg["render_frames"][0] / fps, seg["frames"][0] / fps
    found = []
    for n, item in enumerate(plan_row.get("remove") or []):
        if not (isinstance(item, dict) and item.get("box")) or is_wiped(item):
            continue
        a, b = max(start, float(item.get("start", 0))), min(end, float(item.get("end", float("inf"))))
        if b - a >= MIN_WIPE:
            if not logos or n not in logos:
                raise ValueError(f"remove item {n} ({item.get('what', '')}): its logo mask is missing")
            found.append([*[float(v) for v in item["box"]], round(a - start, 3), round(b - start, 3),
                          f"keep:{logos[n]['hash']}"])
    return found


def clip_size(width: int, height: int, resolution: str = RESOLUTION) -> tuple[int, int]:
    """The size of the clip Seedance gets: the job resolution on the short side (never larger than the source)."""
    short = int(resolution.rstrip("p"))
    if min(width, height) > short:
        factor = short / min(width, height)
        return round(width * factor / 2) * 2, round(height * factor / 2) * 2
    return width // 2 * 2, height // 2 * 2


def lead_frames(previous: Path, span: int, count: int, fps: float, size: tuple[int, int]) -> bytes:
    """The last `count` frames of the previous segment's result on the source clock, at the clip size, as raw RGB:
    the start of a chained segment's clip. The result is a uniformly compressed copy of the `span` source frames its
    clip had (see chain_frame), so it is stretched back to them first."""
    info = video_info(previous)
    factor = span / fps / info["duration"]
    raw = run([FFMPEG, "-v", "error", "-i", previous, "-vf",
               f"setpts=(PTS-STARTPTS)*{factor:.8f},fps={fps:.6f},scale={size[0]}:{size[1]}:flags=lanczos,setsar=1,"
               f"tpad=stop_mode=clone:stop={count},trim=start_frame={span - count}:end_frame={span}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
    frame_bytes = size[0] * size[1] * 3
    if len(raw) < count * frame_bytes:
        raise RuntimeError(f"the previous result {previous.name} gave {len(raw) // frame_bytes} of {count} lead frames")
    return raw[: count * frame_bytes]


def cut_clip(source: Path, frames: list[int], fps: float, width: int, height: int, output: Path,
             clean: list[list] | None = None, logos: dict[int, dict] | None = None,
             lead: dict | None = None, resolution: str = RESOLUTION, keep: list[list] | None = None,
             head: list[int] | None = None, head_clean: list[list] | None = None) -> Path:
    """The segment as a clean H.264 clip (no audio) at the clip size, with the `clean` boxes wiped frame by frame
    (masks in `logos`). `lead` ({"path", "span", "frames"}): the clip's first frames come from the previous segment's
    result instead of the source (see lead_frames), with the source's pixels of the `keep` boxes painted in. `head`:
    source frames [a, b) put before everything (the shot's opening; `head_clean` its wipes, on its own clock)."""
    import subprocess
    import numpy as np
    from reskin import logos as LOGOS
    size = clip_size(width, height, resolution)
    total = frames[1] - frames[0]
    ahead = int(lead["frames"]) if lead else 0
    scale = ["-vf", f"scale={size[0]}:{size[1]}:flags=lanczos"] if size != (width, height) else []
    decode = [FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, (frames[0] - .5) / fps):.6f}", "-i", source,
              "-frames:v", str(total), *scale, "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    encode = ["-c:v", "libx264", "-preset", "fast", "-crf", "15", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    by_hash = {entry["hash"]: entry for entry in (logos or {}).values()}
    wipes = []
    for box in clean or []:
        entry = by_hash.get(str(box[6]).rpartition(":")[2])
        if entry is None:
            raise ValueError(f"logo mask {box[6]} is missing")
        wipes.append((LOGOS.load(entry["path"]), entry["region"], round(box[4] * fps), round(box[5] * fps)))
    kept = []
    for box in keep or []:
        entry = by_hash.get(str(box[6]).rpartition(":")[2])
        if entry is None:
            raise ValueError(f"logo mask {box[6]} is missing")
        kept.append((LOGOS.load(entry["path"]), entry["region"], round(box[4] * fps), round(box[5] * fps)))
    leading = lead_frames(Path(lead["path"]), int(lead["span"]), ahead, fps, size) if ahead else b""
    opening_frames = []
    if head:
        raw = run([FFMPEG, "-v", "error", "-ss", f"{max(0.0, (head[0] - .5) / fps):.6f}", "-i", source, "-frames:v",
                   str(head[1] - head[0]), *scale, "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
        count = len(raw) // (size[0] * size[1] * 3)
        if count < head[1] - head[0]:
            raise RuntimeError(f"the shot's opening gave {count} of {head[1] - head[0]} frames")
        opening_wipes = []
        for box in head_clean or []:
            entry = by_hash.get(str(box[6]).rpartition(":")[2])
            if entry is None:
                raise ValueError(f"logo mask {box[6]} is missing")
            opening_wipes.append((LOGOS.load(entry["path"]), entry["region"], round(box[4] * fps), round(box[5] * fps)))
        for index in range(count):
            data = raw[index * size[0] * size[1] * 3:(index + 1) * size[0] * size[1] * 3]
            live = [w for w in opening_wipes if w[2] <= index < w[3]]
            if live:
                frame = np.frombuffer(data, np.uint8).reshape(size[1], size[0], 3).copy()
                for soft, rect, _, _ in live:
                    LOGOS.wipe(frame, rect, soft)
                data = frame.tobytes()
            opening_frames.append(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial.mp4")
    flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    frame_bytes = size[0] * size[1] * 3
    written = 0
    log_path = partial.with_suffix(".log")
    reader = subprocess.Popen([str(p) for p in decode], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=flags)
    with open(log_path, "wb") as errors:
        writer = subprocess.Popen([str(p) for p in [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt",
                                                    "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", f"{fps:g}", "-i", "-",
                                                    "-an", *encode, partial]],
                                  stdin=subprocess.PIPE, stderr=errors, creationflags=flags)
        try:
            for data in opening_frames:
                writer.stdin.write(data)
            for index in range(total):
                data = reader.stdout.read(frame_bytes)
                if len(data) < frame_bytes:
                    break
                if index < ahead:
                    # a lead frame: the previous result, with the source's logos that Seedance removes itself
                    made = leading[index * frame_bytes:(index + 1) * frame_bytes]
                    live = [k for k in kept if k[2] <= index < k[3]]
                    if live:
                        frame = np.frombuffer(made, np.uint8).reshape(size[1], size[0], 3).astype(np.float32)
                        original = np.frombuffer(data, np.uint8).reshape(size[1], size[0], 3).astype(np.float32)
                        for soft, (x0, y0, x1, y1), _, _ in live:
                            alpha = soft[..., None]
                            frame[y0:y1, x0:x1] = frame[y0:y1, x0:x1] * (1 - alpha) + original[y0:y1, x0:x1] * alpha
                        made = np.clip(frame + .5, 0, 255).astype(np.uint8).tobytes()
                    writer.stdin.write(made)
                    written += 1
                    continue
                if wipes:
                    frame = np.frombuffer(data, np.uint8).reshape(size[1], size[0], 3).copy()
                    for soft, rect, a, b in wipes:
                        if a <= index < b:
                            LOGOS.wipe(frame, rect, soft)
                    data = frame.tobytes()
                writer.stdin.write(data)
                written += 1
        finally:
            writer.stdin.close()
            code = writer.wait()
            reader.stdout.close()
            reader.kill()
            reader.wait()
    detail = log_path.read_text("utf-8", errors="replace").strip()
    log_path.unlink(missing_ok=True)
    if code or written < total:
        raise RuntimeError(f"cutting {output.name} failed ({code}, {written} of {total} frames): {detail[-1500:]}")
    partial.replace(output)
    return output


def chain_frame(job: Path, source_id: str, previous: dict, prev_seg: dict, seg: dict, fps: float) -> Path:
    """The previous result's frame at the moment this segment starts (its clip is a uniformly compressed copy, its
    own opening frames first when it had them)."""
    head = int(previous.get("head_frames") or 0)
    offset = (head + seg["render_frames"][0] - prev_seg["render_frames"][0]) / fps
    length = (head + prev_seg["render_frames"][1] - prev_seg["render_frames"][0]) / fps
    moment = offset * previous["ai"]["duration"] / length
    output = job / "keyframes" / f"{source_id}-{seg['id']}-chain.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, moment - .5 / previous['ai']['fps']):.6f}",
         "-i", previous["output"], "-frames:v", "1", output])
    return output


def opening(plan_row: dict, seg: dict) -> tuple[dict, list[int]]:
    """The first segment of `seg`'s shot and the source frames of its opening (HEAD_SECONDS from its first frame,
    the frame its keyframe was made from)."""
    from reskin.plan import HEAD_SECONDS
    first = next(group[0] for group in chains(plan_row) if any(s["id"] == seg["id"] for s in group))
    a = first["render_frames"][0]
    return first, [a, a + round(HEAD_SECONDS * plan_row["fps"])]


def lead_count(seg: dict) -> int:
    """Source frames a chained segment renders before its own part: the overlap with the segment before it."""
    return seg["frames"][0] - seg["render_frames"][0] if seg["seam"] == "continuous" else 0


def inputs_for(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict,
               previous: dict | None = None) -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    key = f"{source_id}:{seg['id']}"
    redo = redo_log(job).get(key, {})
    span = (seg["render_frames"][0] / plan_row["fps"], seg["render_frames"][1] / plan_row["fps"])
    extra = corrections(job, key, seg)
    lead = {}
    if seg["seam"] == "continuous":
        if previous is None or not previous.get("generation_id"):
            raise ValueError(f"{key}: the segment before it in this shot is not rendered yet")
        # Chained: the reference is the previous result itself (Seedance copies objects from any picture it gets, so
        # nothing else goes along). With chain_prefix (the default) the clip also starts with the previous result's
        # own last frames: two renders of the same moment then differ little where assemble switches between them.
        # (With one reference picture only, the GOF3D10374 seam at 33.6 s changed textures, logs and water.)
        reference = {"chain": previous["generation_id"]}
        texts = read(PROMPTS)
        prefix = plan_row.get("chain_prefix")
        if (state["options"].get("chain_prefix", True) if prefix is None else prefix) and lead_count(seg):
            # The clip: the shot's opening (its keyframe is the reference picture, so Seedance reads the whole edit
            # from that pair), the previous result's last second, then this segment's own frames.
            first, head = opening(plan_row, seg)
            keyframe = job / "keyframes" / f"{source_id}-{first['id']}.png"
            if not keyframe.is_file():
                raise ValueError(f"{key}: keyframe {keyframe.name} of its shot is missing; run keyframes first")
            fps = plan_row["fps"]
            sentence = texts["chain_prefix"].format(head=f"{(head[1] - head[0]) / fps:.1f}",
                                                    seconds=f"{lead_count(seg) / fps:.1f}")
            masks = logo_masks(job, state, plan_row, source_id)
            kept = lead_logos(plan_row, seg, masks)
            wiped_head = clean_boxes(plan_row, head[0] / fps, head[1] / fps, logos=masks)
            lead = {"lead": lead_count(seg), "head": head, "keyframe": sha(keyframe),
                    **({"lead_logos": kept} if kept else {}), **({"head_clean": wiped_head} if wiped_head else {})}
        else:
            sentence = texts["chain"]
        extra = " ".join(part for part in (sentence, extra) if part)
    else:
        keyframe = job / "keyframes" / f"{source_id}-{seg['id']}.png"
        if not keyframe.is_file():
            raise ValueError(f"{key}: keyframe {keyframe.name} is missing; run keyframes first")
        reference = {"keyframe": sha(keyframe)}
    prompt = compose(style, "video", plan_row, extra=extra, span=span)
    clean = clean_boxes(plan_row, *span, logos=logo_masks(job, state, plan_row, source_id))
    return {"source": source["sha256"], "frames": seg["render_frames"], "fps": plan_row["fps"], **reference, **lead,
            **({"clean": clean} if clean else {}), "prompt": prompt, "model": lumos.VIDEO_MODEL,
            "resolution": state["options"]["resolution"], "attempt": redo.get("attempt", 1)}


def logo_masks(job: Path, state: dict, plan_row: dict, source_id: str) -> dict[int, dict]:
    """The letter-by-letter masks of the row's wiped `remove` items (made once, cached; see logos.py)."""
    from reskin import logos as LOGOS
    if not any(isinstance(i, dict) and i.get("box") for i in plan_row.get("remove") or []):
        return {}
    source = Path(next(r for r in state["sources"] if r["id"] == source_id)["path"])
    size = clip_size(plan_row["width"], plan_row["height"], state["options"]["resolution"])
    return LOGOS.masks(job, source_id, source, plan_row, size)


def core(inputs: dict) -> str:
    """What makes a finished segment current: its reference, frames, model and attempt. A prompt change alone applies
    to new and redone segments (a re-render costs minutes and money; `redo` asks for one explicitly)."""
    return digest(core_inputs(inputs))


def core_inputs(inputs: dict) -> dict:
    return {k: v for k, v in inputs.items() if k != "prompt"}


def cause_of(record: dict, inputs: dict, redo: dict) -> str:
    """Why a segment is sent to Seedance now (see ledger.CAUSES): what differs from its last render."""
    if not record:
        return "new"
    if record.get("digest") == core(inputs):
        return "unusable"           # the same inputs again: the last result was rejected or the generation failed
    old = record.get("inputs")
    if not old:
        return "redo" if inputs["attempt"] != record.get("attempt", 1) else "plan"
    if old.get("attempt") != inputs["attempt"]:
        reasons = redo.get("reasons") or []
        return "auto-retry" if reasons and str(reasons[-1]).startswith("automatic retry") else "redo"
    if old.get("chain") != inputs.get("chain"):
        return "upstream"
    if old.get("keyframe") != inputs.get("keyframe"):
        return "keyframe"
    if old.get("clean") != inputs.get("clean"):
        return "wipe"
    if any(old.get(k) != inputs.get(k) for k in ("lead", "lead_logos", "head", "head_clean")):
        return "plan"
    if old.get("resolution") != inputs["resolution"]:
        return "resolution"
    if old.get("model") != inputs["model"]:
        return "model"
    return "plan"


def render_segment(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict,
                   previous: dict | None = None, prev_seg: dict | None = None) -> dict:
    key = f"{source_id}:{seg['id']}"
    record_path = state_path(job, key)
    record = read(record_path) if record_path.is_file() else {}
    inputs = inputs_for(job, state, plan_row, source_id, seg, style, previous)
    want = core(inputs)
    output = job / "ai" / f"{source_id}-{seg['id']}.mp4"
    if record.get("digest") == want and record.get("status") == "done" and output.is_file():
        return {**record, "key": key, "reused": True, "prompt_changed": record.get("prompt") != inputs["prompt"]}
    resume = record.get("digest") == want and record.get("generation_id") and record.get("status") in ("submitted", "running")
    source = Path(next(r for r in state["sources"] if r["id"] == source_id)["path"])
    clip = job / "segments" / f"{source_id}-{seg['id']}.mp4"
    if not resume:
        cause = cause_of(record, inputs, redo_log(job).get(key, {}))
        lead = ({"path": previous["output"], "frames": inputs["lead"],
                 "span": int(previous.get("head_frames") or 0) + prev_seg["render_frames"][1] - prev_seg["render_frames"][0]}
                if inputs.get("lead") else None)
        cut_clip(source, seg["render_frames"], plan_row["fps"], plan_row["width"], plan_row["height"], clip,
                 clean=inputs.get("clean"), logos=logo_masks(job, state, plan_row, source_id), lead=lead,
                 resolution=inputs["resolution"], keep=inputs.get("lead_logos"), head=inputs.get("head"),
                 head_clean=inputs.get("head_clean"))
        video = lumos.upload(clip, "video-to-video")
        if inputs.get("head"):
            picture = job / "keyframes" / f"{source_id}-{opening(plan_row, seg)[0]['id']}.png"
        elif "chain" in inputs:
            picture = chain_frame(job, source_id, previous, prev_seg, seg, plan_row["fps"])
        else:
            picture = job / "keyframes" / f"{source_id}-{seg['id']}.png"
        submitted = lumos.submit_video_edit(video, [lumos.upload(picture, "video-to-video")], inputs["prompt"],
                                            resolution=inputs["resolution"], log=lambda text: log(f"{key}: {text}"))
        record = {"digest": want, "inputs": core_inputs(inputs), "generation_id": submitted["generation_id"],
                  "status": "submitted", "attempt": inputs["attempt"], "prompt": inputs["prompt"],
                  "reference": picture.name, "cause": cause, "submitted_at": time.strftime("%H:%M:%S"),
                  "head_frames": (inputs["head"][1] - inputs["head"][0]) if inputs.get("head") else 0}
        write(record_path, record)
        ledger.add(job, kind="video", event="submit", key=key, seconds=seg["seconds"],
                   resolution=inputs["resolution"], cause=cause, attempt=inputs["attempt"],
                   generation_id=submitted["generation_id"], model=inputs["model"])
        log(f"{key}: submitted {submitted['generation_id']} ({seg['seconds']:.1f} s, {cause}, from {picture.name}"
            + (f", its clip: the shot's opening, the previous result's last {inputs['lead']} frames, its own"
               if inputs.get("head") else f", its clip starting with the previous result's last {inputs['lead']} frames"
               if inputs.get("lead") else "") + ")")

    def on_poll(value):
        status = value.get("status")
        if status == "Running" and record.get("status") != "running":
            record["status"] = "running"
            write(record_path, record)
    try:
        finished = lumos.wait(record["generation_id"], timeout=GENERATION_TIMEOUT, poll=POLL_SECONDS, on_poll=on_poll)
    except lumos.GenerationFailed as error:
        record.update(status="failed", error=str(error))
        write(record_path, record)
        ledger.add(job, kind="video", event="failed", key=key, generation_id=record["generation_id"])
        raise
    from reskin import net
    net.download(lumos.result_urls(finished)[0], output, timeout=300)
    info = video_info(output)
    expected = seg["seconds"]              # the clip's whole length, its opening included
    problems = []
    if not .8 < info["duration"] / expected < 1.2:
        problems.append(f"length {info['duration']:.2f} s for a {expected:.2f} s clip")
    wanted = min(clip_size(plan_row["width"], plan_row["height"], inputs["resolution"]))
    if min(info["width"], info["height"]) < .9 * wanted:
        problems.append(f"resolution {info['width']}x{info['height']} is below {inputs['resolution']}")
    record.update(status="done" if not problems else "rejected", output=str(output), problems=problems,
                  ai={k: info[k] for k in ("width", "height", "fps", "duration", "frames")},
                  finished_at=time.strftime("%H:%M:%S"))
    record.pop("gate", None)
    write(record_path, record)
    ledger.add(job, kind="video", event="done" if not problems else "rejected", key=key,
               generation_id=record["generation_id"])
    if problems:
        raise RuntimeError(f"{key}: Seedance result unusable: {'; '.join(problems)}")
    log(f"{key}: done ({info['width']}x{info['height']}, {info['duration']:.2f} s)")
    return {**record, "key": key, "reused": False}


def plan_order(plan: dict, only: set[str] | None = None,
               hold: set[str] | None = None) -> list[tuple[str, dict, list[dict]]]:
    """Chains to render, the longest first, so the batch is not left waiting on one long chain started last; the
    sources in `hold` (see twin_waits) are left out."""
    groups = [(sid, row, members) for sid, row in plan["sources"].items() for members in chains(row)
              if sid not in (hold or ()) and not all(seg.get("live") for seg in members)
              and (not only or sid in only or any(f"{sid}:{seg['id']}" in only for seg in members))]
    return sorted(groups, key=lambda group: -sum(seg["seconds"] for seg in group[2]))


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None, workers: int = 4,
            gate: bool = True, on_source_done=None, hold: set[str] | None = None) -> list[dict]:
    """Render every selected chain. With `gate`, each segment that has a successor is checked before the successor
    starts; a held segment stops its chain (`held`, the rest `waiting`). `on_source_done(source_id, results)` is
    called as soon as every chain of a source has finished, so its assembly and review need not wait for the batch."""
    from reskin import gate as GATE
    groups = plan_order(plan, only, hold)
    todo = [(sid, row, seg) for sid, row, members in groups for seg in members]
    log(f"rendering {len(todo)} segments in {len(groups)} chains, {workers} chains at a time, longest first "
        "(segments of one chain follow each other; each takes a few minutes on Lumos)"
        + ("; every segment with a successor is checked before it" if gate else ""))
    done = threading.Event()
    remaining = {}
    for sid, _, _ in groups:
        remaining[sid] = remaining.get(sid, 0) + 1
    collected: dict[str, list[dict]] = {}
    lock = threading.Lock()

    def heartbeat(started=time.monotonic()):
        while not done.wait(120):
            states = [read(state_path(job, f"{s}:{g['id']}")).get("status", "pending")
                      if state_path(job, f"{s}:{g['id']}").is_file() else "pending" for s, _, g in todo]
            counts = {name: states.count(name) for name in sorted(set(states))}
            log(f"still rendering ({(time.monotonic() - started) / 60:.0f} min): {counts}")
    threading.Thread(target=heartbeat, daemon=True).start()

    def one(sid, row, seg, previous, prev_seg):
        for attempt in range(2):
            try:
                return render_segment(job, state, row, sid, seg, style, previous, prev_seg)
            except lumos.GenerationFailed as error:
                if attempt == 0:
                    log(f"{sid}:{seg['id']}: {error}; trying once more")
                    bump(job, f"{sid}:{seg['id']}", f"automatic retry after: {error}")
                    continue
                return {"key": f"{sid}:{seg['id']}", "error": str(error)}
            except Exception as error:     # the summary lists every failed segment; the rest continue
                return {"key": f"{sid}:{seg['id']}", "error": f"{type(error).__name__}: {error}"}

    def chain(group):
        sid, row, members = group
        results, previous, prev_seg = [], None, None
        for position, seg in enumerate(members):
            key = f"{sid}:{seg['id']}"
            if previous is not None and (previous.get("error") or previous.get("held") or previous.get("waiting")):
                blocker = previous.get("blocker") or previous["key"]
                why = "held by the segment check" if previous.get("held") or previous.get("waiting") else "failed"
                results.append({"key": key, "waiting": True, "blocker": blocker,
                                "error" if previous.get("error") else "note": f"waits for {blocker} ({why})"})
                previous = results[-1]
                continue
            result = one(sid, row, seg, previous, prev_seg)
            if gate and not result.get("error") and position + 1 < len(members):
                index = next(i for i, s in enumerate(row["segments"]) if s["id"] == seg["id"])
                try:
                    verdict = GATE.check(job, state, row, sid, index)
                    result["gate"] = verdict
                    result["held"] = GATE.holds(verdict)
                except Exception as error:      # a failed check never blocks production; say so
                    log(f"{key}: segment check failed ({type(error).__name__}: {error}); continuing unchecked")
                    result["gate_error"] = f"{type(error).__name__}: {error}"
            previous, prev_seg = result, seg
            results.append(result)
        finish(sid, results)
        return results

    def finish(sid, results):
        if on_source_done is None:
            return
        with lock:
            collected.setdefault(sid, []).extend(results)
            remaining[sid] -= 1
            complete = remaining[sid] == 0
        if complete:
            try:
                on_source_done(sid, collected[sid])
            except Exception as error:
                log(f"{sid}: follow-up after rendering failed: {type(error).__name__}: {error}")
    try:
        with ThreadPoolExecutor(max(1, workers)) as executor:
            return [result for results in executor.map(chain, groups) for result in results]
    finally:
        done.set()


_REDO_LOCK = threading.Lock()


def bump(job: Path, key: str, reason: str, prompt: str | None = None, replace: bool = False) -> dict:
    """Queue another attempt; corrective sentences accumulate, so a later redo keeps the earlier fixes. `replace`
    drops the earlier sentences first (one of them made things worse); with no `prompt` it only drops them."""
    with _REDO_LOCK:
        path = job / "config/redo.json"
        data = read(path) if path.is_file() else {}
        row = data.get(key, {"attempt": 1, "reasons": []})
        row["attempt"] += 1
        row["reasons"].append(reason)
        if replace:
            row.pop("prompt", None)
            row["inherit"] = False      # a `like` twin's sentences go too
        if prompt and prompt.strip():
            sentences = [s for s in (row.get("prompt") or "").split("\n") if s]
            if prompt.strip() not in sentences:
                sentences.append(prompt.strip())
            row["prompt"] = "\n".join(sentences)
        data[key] = row
        write(path, data)
        return row
