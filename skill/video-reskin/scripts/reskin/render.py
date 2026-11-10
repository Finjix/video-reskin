"""Seedance 2.5 video editing on Lumos, segment by segment: cut the clip, upload, submit, poll, download, check.

Every segment's inputs (frames, reference pictures, attempt) are hashed; a re-run reuses finished segments, resumes
generations still running on Lumos, and renders only what changed or was sent back with `redo`.

Each segment gets its source clip, the scene map(s) of the location it shows and the three-view sheets of the
subjects on screen (refs). A segment that continues a shot (a `continuous` seam) also gets the previous segment's
result at the moment it begins, cut from the tail of that result, as Image 1: rendered side by side the two drawings
differed too much at the seam (GOF3D10176, 2026-11-10), so the segments of one shot render one after the other and a
redone segment re-renders the segments after it in the same shot. Separate shots (camera cuts) and separate sources
still render side by side (`--workers` at a time). A prompt alone barely changes a video model's look (MiniMax H3, 2026-11-09,
GOF3D10232: the picture stayed cartoon), so the realistic look comes from these pictures.
"""
from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reskin import credentials, lumos, net
from reskin.common import FFMPEG, digest, log, read, run, sha, video_info, write
from reskin.prompts import compose

POLL_SECONDS = 15
GENERATION_TIMEOUT = 60 * 60
SUBMIT_PATIENCE = 20 * 60      # how long to keep retrying a refused submission (busy queue, rate limits)
UPLOAD_BYTES = 190_000_000     # Seedance 2.5 takes reference videos up to 200 MB
MAX_PICTURES = 5               # reference pictures one segment gets (Seedance 2.5 takes up to 30)
# A segment takes at most plan.MAX_SHEETS three-view sheets; the retry after a result that left the source's
# camera takes none (with the scene map alone K01 of GOF3D10232 kept the source's camera, 2026-11-09).
from reskin.plan import MAX_SHEETS
MAX_REDOS = 2                  # a segment goes back at most twice (the user's rule, 2026-11-09)


class ResultRejected(RuntimeError):
    """The result came back but is unusable (wrong length or size, or it left the source's camera and layout)."""


def state_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}.json"


def redo_log(job: Path) -> dict:
    path = job / "config/redo.json"
    return read(path) if path.is_file() else {}


def rejected_path(job: Path, key: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}-rejected.json"


def keep_rejected(job: Path, key: str, output: Path, entry: dict) -> list[dict]:
    """Set a result that left the source aside (ai/<id>-<Kxx>-rejected-<generation>.mp4) and return this redo
    round's set-aside results."""
    kept = output.with_name(f"{output.stem}-rejected-{entry['generation_id'][:8]}.mp4")
    output.replace(kept)
    path = rejected_path(job, key)
    rows = read(path) if path.is_file() else []
    rows.append({**entry, "path": str(kept)})
    write(path, rows)
    return [r for r in rows if r["round"] == entry["round"] and Path(r["path"]).is_file()]


def cut_clip(source: Path, frames: list[int], fps: float, width: int, height: int, output: Path) -> Path:
    """The segment as a clean H.264 clip (no audio), at most 1080p on the short side and under UPLOAD_BYTES."""
    scale = []
    if min(width, height) > 1080:
        factor = 1080 / min(width, height)
        scale = ["-vf", f"scale={round(width * factor / 2) * 2}:{round(height * factor / 2) * 2}:flags=lanczos"]
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial.mp4")
    seconds = (frames[1] - frames[0]) / fps
    rate = []
    for _ in range(3):
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, (frames[0] - .5) / fps):.6f}", "-i", source,
             "-frames:v", str(frames[1] - frames[0]), *scale, "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "15",
             *rate, "-pix_fmt", "yuv420p", "-movflags", "+faststart", partial])
        if partial.stat().st_size <= UPLOAD_BYTES:
            break
        cap = UPLOAD_BYTES * 8 * .9 / seconds
        rate = ["-maxrate", f"{cap:.0f}", "-bufsize", f"{cap:.0f}"]
    partial.replace(output)
    return output


def previous_of(plan_row: dict, seg: dict) -> dict | None:
    """The segment this one continues (the one before it in the same shot), or None after a camera cut."""
    ids = [s["id"] for s in plan_row["segments"]]
    k = ids.index(seg["id"])
    return plan_row["segments"][k - 1] if k and seg.get("seam") == "continuous" else None


def chain_frame(job: Path, plan_row: dict, source_id: str, seg: dict) -> Path:
    """The previous segment's restyled frame at this segment's first rendered moment (inside the overlap at the tail
    of the previous result). Seedance returns its clips slightly shorter at an even pace, so the moment is found by
    proportion. Rewritten only when the previous result or the moment changes."""
    previous = previous_of(plan_row, seg)
    record_path = state_path(job, f"{source_id}:{previous['id']}")
    record = read(record_path) if record_path.is_file() else {}
    ai = job / "ai" / f"{source_id}-{previous['id']}.mp4"
    if record.get("status") != "done" or not ai.is_file():
        raise ValueError(f"{source_id}:{seg['id']}: the previous segment {previous['id']} has no finished result yet")
    a, b = previous["render_frames"]
    at = (seg["render_frames"][0] - a) / (b - a) * record["ai"]["duration"]
    output = job / "refs/chain" / f"{source_id}-{seg['id']}-start.png"
    stamp = output.with_suffix(".json")
    want = {"after": record["generation_id"], "at": round(at, 4)}
    if not (output.is_file() and stamp.is_file() and read(stamp) == want):
        output.parent.mkdir(parents=True, exist_ok=True)
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, at - .5 / record['ai']['fps']):.6f}", "-i", ai,
             "-frames:v", "1", output])
        write(stamp, want)
    return output


def pictures(job: Path, plan_row: dict, source_id: str, seg: dict, sheets: bool = True) -> list[dict]:
    """The segment's reference pictures in order: the previous result's frame where this segment begins (when it
    continues a shot), the scene map(s) of the location it shows, then the three-view sheets of the subjects on
    screen (first seen in this segment first, at most MAX_SHEETS; none when `sheets` is False), at most MAX_PICTURES
    in all."""
    from reskin import refs as REFS
    key = f"{source_id}:{seg['id']}"
    chosen = []
    if previous_of(plan_row, seg):
        chosen.append({"path": REFS.upload_copy(chain_frame(job, plan_row, source_id, seg)), "role": "previous"})
    for k in seg.get("scenes") or [1]:
        path = REFS.path_of(job, REFS.scene_key(source_id, k))
        if not path.is_file():
            raise ValueError(f"{key}: scene map {path.name} is missing; run refs first")
        chosen.append({"path": REFS.upload_copy(path), "role": "scene", "name": plan_row["scenes"][k - 1]["name"]})
    for k in (seg.get("subjects") or [])[: MAX_SHEETS if sheets else 0]:
        if len(chosen) >= MAX_PICTURES:
            break
        path = REFS.path_of(job, REFS.subject_key(source_id, k))
        if not path.is_file():
            raise ValueError(f"{key}: subject sheet {path.name} is missing; run refs first")
        chosen.append({"path": path, "role": "subject", "what": plan_row["subjects"][k - 1]["what"]})
    return chosen[:MAX_PICTURES]


def references_clause(chosen: list[dict]) -> str:
    """What each reference picture is for, in the video prompt."""
    parts = []
    for n, picture in enumerate(chosen, 1):
        if picture["role"] == "previous":
            parts.append(f"Image {n} is the restyled frame where this clip begins, taken from the end of the previous "
                         "part of this same video: the first frame of the output must look exactly like it (every "
                         "object's materials, textures, colours, brightness, lighting and drawing, the ground and the "
                         "snow), and the whole clip keeps exactly that look, so the two parts join without any visible "
                         "change.")
    scenes = [n for n, p in enumerate(chosen, 1) if p["role"] == "scene"]
    subjects = [(n, p) for n, p in enumerate(chosen, 1) if p["role"] == "subject"]
    if scenes:
        names = " and ".join(f"Image {n}" for n in scenes)
        parts.append(f"{names} {'is' if len(scenes) == 1 else 'are'} the whole location of this video already "
                     "converted into realistic materials, seen from the same camera angle: wherever the camera is, "
                     "every place it shows must look exactly as there (ground, water, ice, buildings, props, "
                     "materials, textures, lighting, colour grading, brightness and level of realism); never fall "
                     "back to the game's cartoon look.")
    for n, picture in subjects:
        parts.append(f"Image {n} is a three-view sheet (front, side, back) of {picture['what']}, already converted: "
                     "whenever it is on screen, give it exactly the look of the sheet, at the size and place it has "
                     "in the input video (the sheet shows it large only to show its look).")
    if parts:
        parts.append("The reference images are for look only: composition, camera, motion, timing and content come "
                     "from the input video; do not add anything from them that the input video does not show.")
    return " ".join(parts)


def inputs_for(job: Path, state: dict, plan_row: dict, source_id: str, seg: dict, style: dict) -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    key = f"{source_id}:{seg['id']}"
    redo = redo_log(job).get(key, {})
    # after a result of this round that left the source, the retry goes without sheets
    path = rejected_path(job, key)
    left = path.is_file() and any(r["round"] == redo.get("redos", 0) for r in read(path))
    chosen = pictures(job, plan_row, source_id, seg, sheets=not left)
    span = (seg["render_frames"][0] / plan_row["fps"], seg["render_frames"][1] / plan_row["fps"])
    prompt = compose(style, "video", plan_row, extra=redo.get("prompt", ""), span=span, refs=references_clause(chosen))
    return {"source": source["sha256"], "frames": seg["render_frames"], "fps": plan_row["fps"],
            "duration": seg["render_seconds"], "pictures": [sha(p["path"]) for p in chosen],
            "references": [{**p, "path": str(p["path"])} for p in chosen], "prompt": prompt,
            "model": lumos.VIDEO_MODEL, "resolution": state["options"].get("render_resolution", lumos.VIDEO_RESOLUTION),
            "attempt": redo.get("attempt", 1)}


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
        uploaded = [lumos.upload(Path(p["path"]), "video-to-video") for p in inputs["references"]]
        started = time.monotonic()
        while True:
            try:
                submitted = lumos.submit_video_edit(video, uploaded, inputs["prompt"], resolution=inputs["resolution"])
                break
            except (RuntimeError, net.HttpError) as error:
                if time.monotonic() - started > SUBMIT_PATIENCE or isinstance(error, credentials.MissingCredential):
                    raise
                log(f"{key}: submission refused ({error}); retrying in 60 s")
                time.sleep(60)
        record = {"digest": want, "generation_id": submitted["generation_id"], "status": "submitted",
                  "references": inputs["references"], "attempt": inputs["attempt"], "prompt": inputs["prompt"],
                  "submitted_at": time.strftime("%H:%M:%S")}
        write(record_path, record)
        log(f"{key}: submitted {submitted['generation_id']} ({inputs['duration']:g} s, {len(uploaded)} pictures)")

    def on_poll(value):
        if value.get("status") == "Running" and record.get("status") != "running":
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
    if not .8 < info["duration"] / expected < 1.25:
        problems.append(f"length {info['duration']:.2f} s for a {expected:.2f} s clip")
    if min(info["width"], info["height"]) < 700:
        problems.append(f"resolution {info['width']}x{info['height']} is too small")
    # A video model may set its own camera (MiniMax H3, 2026-11-09: a redo cut to a close-up of the worker 4 s in):
    # a result that no longer shows the source's camera and layout is caught here, not in the review.
    from reskin.assemble import follows
    follow = follows(clip, output) if not problems else {}
    ai = {k: info[k] for k in ("width", "height", "fps", "duration", "frames")}
    if follow and not follow["follows"]:
        problem = (f"does not follow the source's camera and layout (edges {follow['edges']}, layout "
                   f"{follow['structure']} of a random pair)")
        candidates = keep_rejected(job, key, output, {"generation_id": record["generation_id"], "follow": follow,
                                                      "ai": ai, "round": redo_log(job).get(key, {}).get("redos", 0)})
        if len(candidates) < 2:
            record.update(status="rejected", problems=[problem], follow=follow, finished_at=time.strftime("%H:%M:%S"))
            write(record_path, record)
            raise ResultRejected(f"{key}: Seedance result unusable: {problem}")
        # The second result of this round left the source too: keep the closer one rather than block the video
        # (the review lists it; a redo, if any is left, tries again).
        best = min(candidates, key=lambda c: (c["follow"]["structure"], c["follow"]["edges"]))
        shutil.copyfile(best["path"], output)
        record.update(status="done", output=str(output), generation_id=best["generation_id"], follow=best["follow"],
                      ai=best["ai"], kept_rejected=True, finished_at=time.strftime("%H:%M:%S"),
                      problems=[f"both results of this round left the source's camera and layout; kept the closer "
                                f"one (layout {best['follow']['structure']})"])
        write(record_path, record)
        log(f"{key}: {record['problems'][0]}")
        return {**record, "key": key, "reused": False}
    record.update(status="done" if not problems else "rejected", output=str(output), problems=problems,
                  follow=follow, ai=ai, finished_at=time.strftime("%H:%M:%S"))
    write(record_path, record)
    if problems:
        raise ResultRejected(f"{key}: Seedance result unusable: {'; '.join(problems)}")
    log(f"{key}: done ({info['width']}x{info['height']}, {info['duration']:.2f} s)")
    return {**record, "key": key, "reused": False}


def run_all(job: Path, state: dict, plan: dict, style: dict, only: set[str] | None = None, workers: int = 4) -> list[dict]:
    todo = [(sid, row, seg) for sid, row in plan["sources"].items() for seg in row["segments"]
            if not only or sid in only or f"{sid}:{seg['id']}" in only]
    log(f"rendering {len(todo)} segments with Seedance 2.5 (segments of one shot one after the other, shots and "
        f"sources {workers} at a time; each takes a few minutes on Lumos)")
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
            except (lumos.GenerationFailed, ResultRejected) as error:
                if attempt == 0:
                    log(f"{sid}:{seg['id']}: {error}; trying once more")
                    bump(job, f"{sid}:{seg['id']}", f"automatic retry after: {error}", automatic=True)
                    continue
                return {"key": f"{sid}:{seg['id']}", "error": str(error)}
            except Exception as error:     # the summary lists every failed segment; the rest continue
                return {"key": f"{sid}:{seg['id']}", "error": f"{type(error).__name__}: {error}"}
    # Segments of one shot render one after the other (each takes the previous result's last frame); shots and
    # sources run side by side. A segment whose previous one failed is not rendered.
    chains: list[list] = []
    for item in todo:
        sid, row, seg = item
        previous = previous_of(row, seg)
        if chains and previous and chains[-1][-1][0] == sid and chains[-1][-1][2]["id"] == previous["id"]:
            chains[-1].append(item)
        else:
            chains.append([item])

    def chain(items):
        results = []
        for item in items:
            if results and results[-1].get("error"):
                results.append({"key": f"{item[0]}:{item[2]['id']}",
                                "error": f"not rendered: the previous segment {results[-1]['key']} failed"})
                continue
            results.append(one(item))
        return results
    try:
        with ThreadPoolExecutor(max(1, workers)) as executor:
            return [r for rows in executor.map(chain, chains) for r in rows]
    finally:
        done.set()


_REDO_LOCK = threading.Lock()


def bump(job: Path, key: str, reason: str, prompt: str | None = None, automatic: bool = False,
         replace: bool = False) -> dict:
    """Queue another attempt; corrective sentences accumulate, so a later redo keeps the earlier fixes (`replace`
    drops them first: a sentence naming objects can backfire, 2026-11-09). Redos asked for after a review count
    against MAX_REDOS; an automatic retry after a failed generation does not."""
    with _REDO_LOCK:
        path = job / "config/redo.json"
        data = read(path) if path.is_file() else {}
        row = data.get(key, {"attempt": 1, "redos": 0, "reasons": []})
        row.setdefault("redos", 0)
        if not automatic and row["redos"] >= MAX_REDOS:
            raise ValueError(f"{key} was already sent back {MAX_REDOS} times (the limit): deliver it as it is and "
                             "report the open issue")
        row["attempt"] += 1
        row["redos"] += 0 if automatic else 1
        row["reasons"].append(reason)
        if replace:
            row.pop("prompt", None)
        if prompt and prompt.strip():
            sentences = [s for s in (row.get("prompt") or "").split("\n") if s]
            if prompt.strip() not in sentences:
                sentences.append(prompt.strip())
            row["prompt"] = "\n".join(sentences)
        data[key] = row
        write(path, data)
        return row
