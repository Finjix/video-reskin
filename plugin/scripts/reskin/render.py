"""Seedance 2.5 video editing on Lumos, segment by segment: cut the clip, upload, submit, poll, download, check.

Segments inside one camera shot form a chain: the first starts from its styled keyframe, every later one from the
previous result's own frame at the moment it begins, so the look carries across the seam instead of being
re-imagined. A chain renders one segment after another; separate chains (other shots, other sources) in parallel.

Every segment's inputs (frames, reference picture, attempt) are hashed; a re-run reuses finished segments, resumes
generations still running on Lumos, and renders only what changed or was sent back with `redo` (which also renders
the rest of that chain again, since each segment starts from the one before).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reskin import credentials, lumos, net
from reskin.common import FFMPEG, PROMPTS, digest, log, read, run, sha, video_info, write
from reskin.prompts import compose

POLL_SECONDS = 15
GENERATION_TIMEOUT = 75 * 60
SUBMIT_PATIENCE = 20 * 60      # how long to keep retrying a refused submission (busy queue, rate limits)


def state_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}.json"


def redo_log(job: Path) -> dict:
    path = job / "config/redo.json"
    return read(path) if path.is_file() else {}


def chains(plan_row: dict) -> list[list[dict]]:
    """Segments grouped by camera shot: a new chain starts at the first segment and after every hard cut."""
    groups: list[list[dict]] = []
    for seg in plan_row["segments"]:
        if groups and seg["seam"] == "continuous":
            groups[-1].append(seg)
        else:
            groups.append([seg])
    return groups


def cut_clip(source: Path, frames: list[int], fps: float, width: int, height: int, output: Path) -> Path:
    """The segment as a clean H.264 clip (no audio), at most 1080p on the short side."""
    scale = []
    if min(width, height) > 1080:
        factor = 1080 / min(width, height)
        scale = ["-vf", f"scale={round(width * factor / 2) * 2}:{round(height * factor / 2) * 2}:flags=lanczos"]
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial.mp4")
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, (frames[0] - .5) / fps):.6f}", "-i", source,
         "-frames:v", str(frames[1] - frames[0]), *scale, "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "15",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", partial])
    partial.replace(output)
    return output


def chain_frame(job: Path, source_id: str, previous: dict, prev_seg: dict, seg: dict, fps: float) -> Path:
    """The previous result's frame at the moment this segment starts (its clip is a uniformly compressed copy)."""
    offset = (seg["render_frames"][0] - prev_seg["render_frames"][0]) / fps
    length = (prev_seg["render_frames"][1] - prev_seg["render_frames"][0]) / fps
    moment = offset * previous["ai"]["duration"] / length
    output = job / "keyframes" / f"{source_id}-{seg['id']}-chain.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, moment - .5 / previous['ai']['fps']):.6f}",
         "-i", previous["output"], "-frames:v", "1", output])
    return output


def inputs_for(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict,
               previous: dict | None = None) -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    key = f"{source_id}:{seg['id']}"
    redo = redo_log(job).get(key, {})
    span = (seg["render_frames"][0] / plan_row["fps"], seg["render_frames"][1] / plan_row["fps"])
    extra = redo.get("prompt", "")
    if seg["seam"] == "continuous":
        if previous is None or not previous.get("generation_id"):
            raise ValueError(f"{key}: the segment before it in this shot is not rendered yet")
        # Chained: the only reference is the previous result itself (Seedance copies objects from any picture it
        # gets, so nothing else goes along).
        reference = {"chain": previous["generation_id"]}
        extra = " ".join(part for part in (read(PROMPTS)["chain"], extra) if part)
    else:
        keyframe = job / "keyframes" / f"{source_id}-{seg['id']}.png"
        if not keyframe.is_file():
            raise ValueError(f"{key}: keyframe {keyframe.name} is missing; run keyframes first")
        reference = {"keyframe": sha(keyframe)}
    prompt = compose(style, "video", plan_row, extra=extra, span=span)
    return {"source": source["sha256"], "frames": seg["render_frames"], "fps": plan_row["fps"], **reference,
            "prompt": prompt, "model": lumos.VIDEO_MODEL, "resolution": state["options"]["resolution"],
            "attempt": redo.get("attempt", 1)}


def core(inputs: dict) -> str:
    """What makes a finished segment current: its reference, frames, model and attempt. A prompt change alone applies
    to new and redone segments (a re-render costs minutes and money; `redo` asks for one explicitly)."""
    return digest({k: v for k, v in inputs.items() if k != "prompt"})


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
        cut_clip(source, seg["render_frames"], plan_row["fps"], plan_row["width"], plan_row["height"], clip)
        video = lumos.upload(clip, "video-to-video")
        picture = (chain_frame(job, source_id, previous, prev_seg, seg, plan_row["fps"]) if "chain" in inputs
                   else job / "keyframes" / f"{source_id}-{seg['id']}.png")
        pictures = [lumos.upload(picture, "video-to-video")]
        started = time.monotonic()
        while True:
            try:
                submitted = lumos.submit_video_edit(video, pictures, inputs["prompt"], resolution=inputs["resolution"])
                break
            except (RuntimeError, net.HttpError) as error:
                if time.monotonic() - started > SUBMIT_PATIENCE or isinstance(error, credentials.MissingCredential):
                    raise
                log(f"{key}: submission refused ({error}); retrying in 60 s")
                time.sleep(60)
        record = {"digest": want, "generation_id": submitted["generation_id"], "status": "submitted",
                  "attempt": inputs["attempt"], "prompt": inputs["prompt"], "reference": picture.name,
                  "submitted_at": time.strftime("%H:%M:%S")}
        write(record_path, record)
        log(f"{key}: submitted {submitted['generation_id']} ({seg['seconds']:.1f} s, from {picture.name})")

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
        raise
    net.download(lumos.result_urls(finished)[0], output, timeout=300)
    info = video_info(output)
    expected = seg["seconds"]
    problems = []
    if not .8 < info["duration"] / expected < 1.2:
        problems.append(f"length {info['duration']:.2f} s for a {expected:.2f} s clip")
    if inputs["resolution"] == "1080p" and min(info["width"], info["height"]) < 1000:
        problems.append(f"resolution {info['width']}x{info['height']} is below 1080p")
    record.update(status="done" if not problems else "rejected", output=str(output), problems=problems,
                  ai={k: info[k] for k in ("width", "height", "fps", "duration", "frames")},
                  finished_at=time.strftime("%H:%M:%S"))
    write(record_path, record)
    if problems:
        raise RuntimeError(f"{key}: Seedance result unusable: {'; '.join(problems)}")
    log(f"{key}: done ({info['width']}x{info['height']}, {info['duration']:.2f} s)")
    return {**record, "key": key, "reused": False}


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None, workers: int = 4) -> list[dict]:
    groups = [(sid, row, members) for sid, row in plan["sources"].items() for members in chains(row)
              if not only or sid in only or any(f"{sid}:{seg['id']}" in only for seg in members)]
    todo = [(sid, row, seg) for sid, row, members in groups for seg in members]
    log(f"rendering {len(todo)} segments in {len(groups)} chains, {workers} chains at a time "
        "(segments of one chain follow each other; each takes a few minutes on Lumos)")
    done = threading.Event()

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
        for seg in members:
            if previous is not None and previous.get("error"):
                results.append({"key": f"{sid}:{seg['id']}", "error": f"waits for {previous['key']}"})
                continue
            previous, prev_seg = one(sid, row, seg, previous, prev_seg), seg
            results.append(previous)
        return results
    try:
        with ThreadPoolExecutor(max(1, workers)) as executor:
            return [result for results in executor.map(chain, groups) for result in results]
    finally:
        done.set()


_REDO_LOCK = threading.Lock()


def bump(job: Path, key: str, reason: str, prompt: str | None = None) -> dict:
    """Queue another attempt; corrective sentences accumulate, so a later redo keeps the earlier fixes."""
    with _REDO_LOCK:
        path = job / "config/redo.json"
        data = read(path) if path.is_file() else {}
        row = data.get(key, {"attempt": 1, "reasons": []})
        row["attempt"] += 1
        row["reasons"].append(reason)
        if prompt and prompt.strip():
            sentences = [s for s in (row.get("prompt") or "").split("\n") if s]
            if prompt.strip() not in sentences:
                sentences.append(prompt.strip())
            row["prompt"] = "\n".join(sentences)
        data[key] = row
        write(path, data)
        return row
