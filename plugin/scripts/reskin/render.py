"""Seedance 2.5 video editing on Lumos, segment by segment: cut the clip, upload, submit, poll, download, check.

Every segment's inputs (frames, keyframes, prompt, attempt) are hashed; a re-run reuses finished segments, resumes
generations still running on Lumos, and renders only what changed or was sent back with `redo`.

The segments of one source render one after another and each takes its look from the results before it: a segment
that continues a shot (seam "continuous") gets the previous result's frame at its first moment as Image 1 instead of
a separately restyled keyframe, so both draw the overlap alike; every later segment also gets the earlier results'
frames of the places it shows again, and GPT Image reveal frames of objects no earlier segment shows
(references.py). Only pictures are chained: a lead of the earlier result in the clip itself made Seedance keep the
source's look in the rest (archive branch, 2026-11-05). A redone segment therefore re-renders the segments after it.
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


def previous(plan_row: dict, seg: dict) -> dict | None:
    """The segment a continuing segment is chained to (None at a shot start or a cut)."""
    if seg["seam"] != "continuous":
        return None
    ids = [s["id"] for s in plan_row["segments"]]
    return plan_row["segments"][ids.index(seg["id"]) - 1]


def references(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict) -> list[dict]:
    """The segment's reference pictures in order (Image 1, 2, ...): its start (the previous result's frame there when
    it continues a shot, else its keyframe), the reveal frames of objects new in it, then the earlier results' frames
    of the places it shows again (references.py). Each: {"path", "role", ...}."""
    from reskin import references as REFS
    key = f"{source_id}:{seg['id']}"
    ids = [s["id"] for s in plan_row["segments"]]
    earlier = plan_row["segments"][: ids.index(seg["id"])]
    for other in earlier:
        record_path = state_path(job, f"{source_id}:{other['id']}")
        record = read(record_path) if record_path.is_file() else {}
        if record.get("status") != "done" or not (job / "ai" / f"{source_id}-{other['id']}.mp4").is_file():
            raise ValueError(f"{key}: follows {source_id}:{other['id']}, which is not rendered yet")
    folder = job / "keyframes"
    stem = f"{source_id}-{seg['id']}"
    prev = previous(plan_row, seg)
    if prev is not None:
        # The previous result's own frame, untouched: a GPT redraw of it (refreshed) or the segment's separately
        # converted keyframe as a second look reference differ in shading and snow from what the previous segment
        # drew, and the seam jumped (GOF3D10232, 2026-11-06). Seam continuity comes first.
        chained = REFS.result_frame(job, plan_row, source_id, seg["render_frames"][0], folder / f"{stem}-chain.png", prev)
        pictures = [{"path": chained, "role": "start",
                     "after": read(state_path(job, f"{source_id}:{prev['id']}"))["generation_id"]}]
    else:
        keyframe = folder / f"{stem}.png"
        if not keyframe.is_file():
            raise ValueError(f"{key}: keyframe {keyframe.name} is missing; run keyframes first")
        pictures = [{"path": keyframe, "role": "keyframe"}]
    for reveal in seg.get("reveals") or []:
        path = folder / f"{source_id}-{reveal['id']}.png"
        if not path.is_file():
            raise ValueError(f"{key}: reveal frame {path.name} is missing; run keyframes first")
        pictures.append({"path": path, "role": "reveal", "what": reveal["what"], "time": reveal["time"]})
    room = REFS.MAX_PICTURES - len(pictures)
    if earlier and room > 0:
        source = Path(next(r for r in state["sources"] if r["id"] == source_id)["path"])
        pixels, frames = REFS.thumbnails(job, source_id, source, plan_row["width"], plan_row["height"], plan_row["fps"])
        for number, moment in enumerate(REFS.earlier_moments(pixels, frames, plan_row, seg, plan_row["fps"],
                                                             min(room, REFS.EARLIER)), 1):
            path = REFS.result_frame(job, plan_row, source_id, moment["frame"], folder / f"{stem}-ref{number}.png")
            pictures.append({"path": path, "role": "earlier", "time": round(moment["frame"] / plan_row["fps"], 3),
                             "for": round(moment["for"] / plan_row["fps"], 3), "gain": moment["gain"]})
    return pictures


def references_clause(pictures: list[dict], clip_start: float) -> str:
    """What each reference picture after Image 1 is for, in the video prompt."""
    parts = []
    for number, picture in enumerate(pictures, 1):
        if picture["role"] == "start" and number == 1 and picture.get("after"):
            parts.append("Image 1 is this video's previous part at the clip's first moment: continue exactly from it, "
                         "with the same materials, textures, lighting, brightness and colour grading for the whole clip "
                         "and no gradual change of look.")
        if picture["role"] == "look":
            parts.append(f"Image {number} is the same first moment converted as a still: it sets the material detail "
                         "for the whole clip. Match its surfaces exactly and keep them that detailed to the last frame: "
                         "snow with crisp visible grain, sparkle and an uneven packed surface, ground with clear soil "
                         "texture, wood with visible grain, fabric with weave; never let surfaces turn smooth, soft or "
                         "flat. Take only the surface detail from it: colours, brightness and every object's look follow Image 1, "
                         "which stays the exact starting picture.")
    reveals = [(n, p) for n, p in enumerate(pictures, 1) if p["role"] == "reveal"]
    earlier = [n for n, p in enumerate(pictures, 1) if p["role"] == "earlier"]
    for number, picture in reveals:
        parts.append(f"Image {number} is this video at {max(0.0, picture['time'] - clip_start):.1f} s of the clip, "
                     f"converted ahead, showing {', '.join(picture['what'])} in realistic materials: when they come "
                     f"into view, render them exactly as Image {number} shows them.")
    if earlier:
        names = (f"Image {earlier[0]}" if len(earlier) == 1 else
                 "Images " + ", ".join(str(n) for n in earlier[:-1]) + f" and {earlier[-1]}")
        parts.append(f"{names} {'is a frame' if len(earlier) == 1 else 'are frames'} of this same video converted "
                     "earlier: wherever the same places, objects and characters appear in this clip (ground, water, "
                     "trees, buildings, props, people), render them exactly as there, with the same realistic "
                     "materials, textures, lighting and level of realism; never fall back to the game's cartoon look.")
    if parts:
        parts.append("All reference images are for look only: the composition, camera, motion and content come from "
                     "the input video; do not add anything from them that the input video does not show.")
    return " ".join(parts)


def inputs_for(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict) -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    key = f"{source_id}:{seg['id']}"
    redo = redo_log(job).get(key, {})
    pictures = references(job, state, plan_row, source_id, seg, style)
    span = (seg["render_frames"][0] / plan_row["fps"], seg["render_frames"][1] / plan_row["fps"])
    prompt = compose(style, "video", plan_row, extra=redo.get("prompt", ""), span=span,
                     refs=references_clause(pictures, span[0]))
    return {"source": source["sha256"], "frames": seg["render_frames"], "fps": plan_row["fps"],
            "pictures": [sha(p["path"]) for p in pictures], "after": pictures[0].get("after"),
            "references": [{**p, "path": str(p["path"])} for p in pictures], "prompt": prompt,
            "model": lumos.VIDEO_MODEL, "resolution": state["options"]["resolution"], "attempt": redo.get("attempt", 1)}


def core(inputs: dict) -> str:
    """What makes a finished segment current: its pictures, frames, model and attempt. A prompt change alone applies
    to new and redone segments (a re-render costs minutes and money; `redo` asks for one explicitly)."""
    return digest({k: v for k, v in inputs.items() if k not in ("prompt", "references")})


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
        pictures = [lumos.upload(Path(p["path"]), "video-to-video") for p in inputs["references"]]
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
                  "references": inputs["references"],
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
    # The segments of one source follow each other (each takes pictures from the results before it), so a change
    # upstream reaches them too; sources run side by side.
    chains: list[list] = []
    for item in todo:
        if chains and chains[-1][-1][0] == item[0]:
            chains[-1].append(item)
        else:
            chains.append([item])
    log(f"rendering {len(todo)} segments of {len(chains)} sources, {workers} sources at a time (segments of one "
        "source follow each other; each takes a few minutes on Lumos)")
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
    def chain(items):
        results = []
        for item in items:
            if results and results[-1].get("error"):
                results.append({"key": f"{item[0]}:{item[2]['id']}", "error": f"waits for {results[-1]['key']}"})
                continue
            results.append(one(item))
        return results
    try:
        with ThreadPoolExecutor(max(1, workers)) as executor:
            return [r for rows in executor.map(chain, chains) for r in rows]
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
