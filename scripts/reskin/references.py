"""Reference pictures of a later segment, taken from what the earlier segments of the same video already made.

Seedance draws a clip after its reference pictures. A segment that continues a shot gets the previous result's
frame at its first moment (it continues from there). Besides that, every later segment gets the earlier results'
frames of the places it shows again: the source frames it shows are matched against the source frames the earlier
segments delivered, and the best matches' restyled frames go along, so a riverbank seen again at 36 s looks as it
did at 3 s (with the previous frame alone it fell back to the game's cartoon trees). Objects no earlier segment shows
come from reveal frames that GPT Image restyles ahead (plan.mark_reveals, keyframes).

Seedance smooths fine texture (snow grain, soil, wood) well below its reference picture's (GOF3D10232, 2026-11-06:
snow detail 40 on GPT Image frames, 10-20 in the videos), so frames cut from earlier results carry an already softened
look. A continuing segment therefore also gets its own GPT Image keyframe as the material-detail reference (after
Image 1, so the seam still follows the previous result).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from reskin.common import FFMPEG, read, run, write

THUMB = (32, 56)          # thumbnail size (portrait; swapped for landscape) the source frames are matched on
SAMPLE_FPS = 2.0          # source frames per second that are compared
EARLIER = 3               # earlier results' frames one segment gets at most
SPACING = 3.0             # seconds: earlier frames picked at least this far apart (and from the segment's start)
MAX_PICTURES = 6          # reference pictures one Seedance segment gets at most (Image 1 included)


def thumbnails(job: Path, source_id: str, source: Path, width: int, height: int, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """Small RGB frames of the source every 1/SAMPLE_FPS s and their source frame numbers (cached)."""
    folder = job / "state/thumbs"
    cache = folder / f"{source_id}.npz"
    if cache.is_file():
        data = np.load(cache)
        return data["pixels"], data["frames"]
    w, h = THUMB if height >= width else THUMB[::-1]
    raw = run([FFMPEG, "-v", "error", "-i", source, "-vf", f"fps={SAMPLE_FPS},scale={w}:{h}:flags=area,format=rgb24",
               "-f", "rawvideo", "-"], binary=True)
    pixels = np.frombuffer(raw, np.uint8)
    count = pixels.size // (w * h * 3)
    pixels = pixels[: count * w * h * 3].reshape(count, h, w, 3).astype(np.float32)
    frames = np.array([round(i / SAMPLE_FPS * fps) for i in range(count)])
    folder.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, pixels=pixels, frames=frames)
    return pixels, frames


def earlier_moments(pixels: np.ndarray, frames: np.ndarray, plan_row: dict, seg: dict, fps: float,
                    limit: int = EARLIER) -> list[dict]:
    """Source frames delivered by the segments before `seg` that together look most like the frames `seg` shows:
    picked one by one, each the one that best covers the frames the pictures so far cover worst (the segment's first
    rendered frame counts as picked: it goes along anyway), at least SPACING apart. A riverbank seen again late in
    the segment so gets the earlier riverbank even when most of the segment shows the camp."""
    ids = [s["id"] for s in plan_row["segments"]]
    earlier = plan_row["segments"][: ids.index(seg["id"])]
    if not earlier:
        return []
    mine = (frames >= seg["frames"][0]) & (frames < seg["frames"][1])
    before = np.zeros(len(frames), bool)
    for other in earlier:
        before |= (frames >= other["frames"][0]) & (frames < other["frames"][1])
    if not mine.any() or not before.any():
        return []
    queries, candidates = pixels[mine], pixels[before]
    query_frames, candidate_frames = frames[mine], frames[before]
    distance = np.abs(queries[:, None] - candidates[None]).mean(axis=(2, 3, 4))      # queries x candidates
    start = seg["render_frames"][0]
    nearest_start = int(np.abs(frames - start).argmin())
    covered = np.abs(queries - pixels[nearest_start][None]).mean(axis=(1, 2, 3))
    picked: list[dict] = []
    for _ in range(limit):
        allowed = np.array([abs(int(f) - start) >= SPACING * fps and all(abs(int(f) - p["frame"]) >= SPACING * fps
                                                                        for p in picked) for f in candidate_frames])
        if not allowed.any():
            break
        gain = np.maximum(covered[:, None] - distance, 0).sum(axis=0)
        gain[~allowed] = -1
        c = int(gain.argmax())
        if gain[c] <= 0:
            break
        helped = distance[:, c] < covered
        target = int(query_frames[helped][np.argmax(covered[helped] - distance[helped, c])])
        picked.append({"frame": int(candidate_frames[c]), "for": target, "gain": round(float(gain[c]), 1)})
        covered = np.minimum(covered, distance[:, c])
    return picked


def segment_of(plan_row: dict, frame: int) -> dict:
    for seg in plan_row["segments"]:
        if seg["frames"][0] <= frame < seg["frames"][1]:
            return seg
    return plan_row["segments"][-1]


def result_frame(job: Path, plan_row: dict, source_id: str, frame: int, output: Path, owner: dict | None = None) -> Path:
    """The restyled frame at source frame `frame`, cut from the result of the segment that rendered it (`owner`, or
    the one delivering it). Seedance returns its clips slightly shorter at an even pace: the moment is found by
    proportion. Rewritten only when the result or the moment changes."""
    seg = owner or segment_of(plan_row, frame)
    record = read(job / "state/render" / f"{source_id}-{seg['id']}.json")
    ai = job / "ai" / f"{source_id}-{seg['id']}.mp4"
    a, b = seg["render_frames"]
    at = (frame - a) / (b - a) * record["ai"]["duration"]
    stamp = output.with_suffix(".json")
    want = {"after": record["generation_id"], "at": round(at, 4)}
    if not (output.is_file() and stamp.is_file() and read(stamp) == want):
        output.parent.mkdir(parents=True, exist_ok=True)
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, at - .5 / record['ai']['fps']):.6f}", "-i", ai,
             "-frames:v", "1", output])
        write(stamp, want)
    return output
