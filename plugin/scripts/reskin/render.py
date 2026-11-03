"""Seedance 2.5 video editing on Lumos, segment by segment: cut the clip, upload, submit, poll, download, check.

Every segment's inputs (frames, keyframes, prompt, attempt) are hashed; a re-run reuses finished segments, resumes
generations still running on Lumos, and renders only what changed or was sent back with `redo`.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reskin import credentials, lumos, net
from reskin.common import FFMPEG, digest, log, read, run, sha, video_info, write
from reskin.prompts import compose

POLL_SECONDS = 15
GENERATION_TIMEOUT = 75 * 60
SUBMIT_PATIENCE = 20 * 60      # how long to keep retrying a refused submission (busy queue, rate limits)


def state_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}.json"


def redo_log(job: Path) -> dict:
    path = job / "config/redo.json"
    return read(path) if path.is_file() else {}


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


def inputs_for(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict) -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    key = f"{source_id}:{seg['id']}"
    redo = redo_log(job).get(key, {})
    # Only the segment's own keyframe goes along: it already carries the anchor's look, and Seedance copies objects
    # from any reference picture into the video (an anchor from elsewhere in the ad leaks its objects).
    keyframe = job / "keyframes" / f"{source_id}-{seg['id']}.png"
    if not keyframe.is_file():
        raise ValueError(f"{key}: keyframe {keyframe.name} is missing; run keyframes first")
    span = (seg["render_frames"][0] / plan_row["fps"], seg["render_frames"][1] / plan_row["fps"])
    prompt = compose(style, "video", plan_row, extra=redo.get("prompt", ""), span=span)
    return {"source": source["sha256"], "frames": seg["render_frames"], "fps": plan_row["fps"],
            "keyframe": sha(keyframe), "prompt": prompt, "model": lumos.VIDEO_MODEL,
            "resolution": state["options"]["resolution"], "attempt": redo.get("attempt", 1)}


def core(inputs: dict) -> str:
    """What makes a finished segment current: its pictures, frames, model and attempt. A prompt change alone applies
    to new and redone segments (a re-render costs minutes and money; `redo` asks for one explicitly)."""
    return digest({k: v for k, v in inputs.items() if k != "prompt"})


def render_segment(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict) -> dict:
    key = f"{source_id}:{seg['id']}"
    record_path = state_path(job, key)
    record = read(record_path) if record_path.is_file() else {}
    inputs = inputs_for(job, state, plan_row, source_id, seg, style)
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
        pictures = [lumos.upload(job / "keyframes" / f"{source_id}-{seg['id']}.png", "video-to-video")]
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
                  "attempt": inputs["attempt"], "prompt": inputs["prompt"], "submitted_at": time.strftime("%H:%M:%S")}
        write(record_path, record)
        log(f"{key}: submitted {submitted['generation_id']} ({seg['seconds']:.1f} s)")

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
    todo = [(sid, row, seg) for sid, row in plan["sources"].items() for seg in row["segments"]
            if not only or sid in only or f"{sid}:{seg['id']}" in only]
    log(f"rendering {len(todo)} segments, {workers} at a time (each takes a few minutes on Lumos)")
    done = threading.Event()

    def heartbeat(started=time.monotonic()):
        while not done.wait(120):
            states = [read(state_path(job, f"{s}:{g['id']}")).get("status", "pending")
                      if state_path(job, f"{s}:{g['id']}").is_file() else "pending" for s, _, g in todo]
            counts = {name: states.count(name) for name in sorted(set(states))}
            log(f"still rendering ({(time.monotonic() - started) / 60:.0f} min): {counts}")
    threading.Thread(target=heartbeat, daemon=True).start()

    def one(item):
        sid, row, seg = item
        for attempt in range(2):
            try:
                return render_segment(job, state, row, sid, seg, style)
            except lumos.GenerationFailed as error:
                if attempt == 0:
                    log(f"{sid}:{seg['id']}: {error}; trying once more")
                    bump(job, f"{sid}:{seg['id']}", f"automatic retry after: {error}")
                    continue
                return {"key": f"{sid}:{seg['id']}", "error": str(error)}
            except Exception as error:     # the summary lists every failed segment; the rest continue
                return {"key": f"{sid}:{seg['id']}", "error": f"{type(error).__name__}: {error}"}
    try:
        with ThreadPoolExecutor(max(1, workers)) as executor:
            return list(executor.map(one, todo))
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
