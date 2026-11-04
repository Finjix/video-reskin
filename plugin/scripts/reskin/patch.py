"""Local patches: render a short stretch (4-8 s) inside a finished segment again instead of the whole segment.

A redo renders the whole segment (up to 30 s) and, inside a shot, every later segment of the chain too. A patch
starts from the segment's own frame at its first moment (like a chained segment), renders the flawed stretch plus
half a second on both sides, and `assemble` switches into it and back out of it at the best-matching overlap frames,
with the colours of the patch matched to the segment. The rest of the segment and the rest of the chain stay as they
are. Both joins are measured like seams (clean, slight, visible); a patch is only kept when both are clean or
slight, else the segment needs a redo. A patch belongs to the segment's render it was made for: when the segment is
rendered again (redo, upstream change) its patches are dropped.

Whole-segment redo stays the default; a patch suits a confirmed local flaw (an object morphing for a second, a
leftover logo for a moment) in a long segment, in a stretch without a camera cut or fast motion.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from reskin import ledger, lumos
from reskin.common import FFMPEG, PROMPTS, digest, log, read, run, video_info, write

MIN_SECONDS = 4.0      # Seedance edits clips of at least 4 s
MAX_SECONDS = 8.0
OVERLAP = 0.5          # rendered on both sides of the replaced stretch; the joins are chosen inside it
EDGE = 0.25            # distance a patch keeps from the edges of its segment's delivered part


def config_path(job: Path) -> Path:
    return job / "config/patches.json"


def load(job: Path) -> dict:
    path = config_path(job)
    return read(path) if path.is_file() else {}


def segment_record(job: Path, key: str) -> dict:
    path = job / "state/render" / f"{key.replace(':', '-')}.json"
    return read(path) if path.is_file() else {}


def window(row: dict, seg: dict, start: float, end: float, following: bool, last: bool = False) -> list[int]:
    """Source frames to render for a flaw from `start` to `end` (source seconds): the flaw with a margin, at least
    MIN_SECONDS with the overlaps, inside the segment's delivered part (clear of the overlap the next segment uses).
    In the video's `last` segment a patch may run to the end: it then has no join out (nothing follows it)."""
    fps = row["fps"]
    lo = seg["frames"][0] + round(EDGE * fps)
    hi = seg["frames"][1] - (0 if last else round((EDGE + (OVERLAP if following else 0)) * fps))
    a, b = round((start - OVERLAP) * fps), round((end + OVERLAP) * fps)
    minimum = round(MIN_SECONDS * fps)
    if b - a < minimum:
        grow = minimum - (b - a)
        a, b = a - grow // 2, b + grow - grow // 2
    if a < lo:
        a, b = lo, b + (lo - a)
    if b > hi:
        a, b = a - (b - hi), hi
    if a < lo or b - a > round(MAX_SECONDS * fps) + 1:
        raise ValueError(f"a patch for {start:.2f}-{end:.2f} s does not fit inside {seg['id']} "
                         f"({seg['frames'][0] / fps:.2f}-{seg['frames'][1] / fps:.2f} s, keeping {EDGE:g} s from its "
                         f"edges and at most {MAX_SECONDS:g} s long): redo the segment instead")
    return [a, b]


def add(job: Path, plan: dict, key: str, start: float, end: float, reason: str, prompt: str | None) -> dict:
    """`start`/`end` are seconds in the delivered video (like review times)."""
    source_id, _, segment_id = key.partition(":")
    row = plan["sources"].get(source_id)
    segments = row["segments"] if row else []
    index = next((i for i, s in enumerate(segments) if s["id"] == segment_id), None)
    if index is None:
        raise ValueError(f"unknown segment {key}; segments look like S01:K02 (see reports/plan.md)")
    record = segment_record(job, key)
    if record.get("status") != "done":
        raise ValueError(f"{key} is not rendered; a patch needs the finished segment")
    seg = segments[index]
    following = index + 1 < len(segments) and segments[index + 1]["seam"] == "continuous"
    last = index + 1 == len(segments)
    a, b = sorted((start, end))
    frames = window(row, seg, row["keep"][0] + a, row["keep"][0] + b, following, last)
    data = load(job)
    current = [p for p in data.get(key, []) if p.get("base") == record["generation_id"]]
    for other in current:
        if frames[0] < other["render_frames"][1] and other["render_frames"][0] < frames[1]:
            raise ValueError(f"overlaps patch {key}:{other['id']}; drop it first (patch --drop {key}:{other['id']})")
    number = 1 + max([int(p["id"][1:]) for p in data.get(key, [])] or [0])
    entry = {"id": f"P{number}", "base": record["generation_id"], "render_frames": frames, "reason": reason,
             "prompt": (prompt or "").strip(), "attempt": 1}
    data[key] = [*current, entry]
    write(config_path(job), data)
    return {**entry, "seconds": round((frames[1] - frames[0]) / row["fps"], 2),
            "start": round(frames[0] / row["fps"] - row["keep"][0], 2),
            "end": round(frames[1] / row["fps"] - row["keep"][0], 2)}


def drop(job: Path, key: str) -> bool:
    segment_key, _, patch_id = key.rpartition(":")
    data = load(job)
    before = len(data.get(segment_key, []))
    data[segment_key] = [p for p in data.get(segment_key, []) if p["id"] != patch_id]
    write(config_path(job), data)
    return len(data[segment_key]) < before


def active(job: Path, key: str) -> list[dict]:
    """Patches made for the segment's current render, in time order."""
    record = segment_record(job, key)
    return sorted((p for p in load(job).get(key, []) if p.get("base") == record.get("generation_id")),
                  key=lambda p: p["render_frames"][0])


def state_path(job: Path, key: str, patch_id: str) -> Path:
    return job / "state/render" / f"{key.replace(':', '-')}-{patch_id}.json"


def output_path(job: Path, key: str, patch_id: str) -> Path:
    return job / "ai" / f"{key.replace(':', '-')}-{patch_id}.mp4"


def render_patch(job: Path, state: dict, row: dict, source_id: str, seg: dict, entry: dict, style: dict) -> dict:
    """Render one patch (reused when nothing changed); returns its record."""
    from reskin.prompts import compose
    from reskin.render import clean_boxes, cut_clip, logo_masks
    key = f"{source_id}:{seg['id']}"
    record_path = state_path(job, key, entry["id"])
    record = read(record_path) if record_path.is_file() else {}
    base = segment_record(job, key)
    logos = logo_masks(job, state, row, source_id)
    clean = clean_boxes(row, entry["render_frames"][0] / row["fps"], entry["render_frames"][1] / row["fps"], logos)
    inputs = {"base": entry["base"], "frames": entry["render_frames"], "attempt": entry.get("attempt", 1),
              "resolution": state["options"]["resolution"], "model": lumos.VIDEO_MODEL,
              **({"clean": clean, "wipe": "feather"} if clean else {})}
    want = digest(inputs)
    output = output_path(job, key, entry["id"])
    if record.get("digest") == want and record.get("status") == "done" and output.is_file():
        return {**record, "reused": True}
    fps = row["fps"]
    frames = entry["render_frames"]
    if not (record.get("digest") == want and record.get("status") in ("submitted", "running")):
        source = Path(next(r for r in state["sources"] if r["id"] == source_id)["path"])
        clip = cut_clip(source, frames, fps, row["width"], row["height"],
                        job / "segments" / f"{source_id}-{seg['id']}-{entry['id']}.mp4", clean=clean,
                        logos=logos)
        # The segment's own frame at the patch's first moment (its result is a uniformly compressed copy).
        moment = (frames[0] - seg["render_frames"][0]) / fps
        length = (seg["render_frames"][1] - seg["render_frames"][0]) / fps
        at = moment * base["ai"]["duration"] / length
        picture = job / "keyframes" / f"{source_id}-{seg['id']}-{entry['id']}-start.png"
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, at - .5 / base['ai']['fps']):.6f}", "-i", base["output"],
             "-frames:v", "1", picture])
        texts = read(PROMPTS)
        redo = (read(job / "config/redo.json") if (job / "config/redo.json").is_file() else {}).get(key, {})
        extra = " ".join(p for p in (texts["chain"], redo.get("prompt", ""), entry.get("prompt", "")) if p)
        prompt = compose(style, "video", row, extra=extra, span=(frames[0] / fps, frames[1] / fps))
        video = lumos.upload(clip, "video-to-video")
        reference = lumos.upload(picture, "video-to-video")
        submitted = lumos.submit_video_edit(video, [reference], prompt, resolution=inputs["resolution"],
                                            log=lambda text: log(f"{key}:{entry['id']}: {text}"))
        record = {"digest": want, "generation_id": submitted["generation_id"], "status": "submitted",
                  "prompt": prompt, "reference": picture.name}
        write(record_path, record)
        seconds = round((frames[1] - frames[0]) / fps, 3)
        ledger.add(job, kind="video", event="submit", key=f"{key}:{entry['id']}", seconds=seconds,
                   resolution=inputs["resolution"], cause="patch", attempt=inputs["attempt"],
                   generation_id=submitted["generation_id"], model=inputs["model"])
        log(f"{key}:{entry['id']}: submitted patch {submitted['generation_id']} ({seconds:.1f} s)")
    try:
        finished = lumos.wait(record["generation_id"], timeout=30 * 60, poll=15)
    except lumos.GenerationFailed as error:
        record.update(status="failed", error=str(error))
        write(record_path, record)
        ledger.add(job, kind="video", event="failed", key=f"{key}:{entry['id']}", generation_id=record["generation_id"])
        raise
    from reskin import net
    net.download(lumos.result_urls(finished)[0], output, timeout=300)
    info = video_info(output)
    record.update(status="done", output=str(output),
                  ai={k: info[k] for k in ("width", "height", "fps", "duration", "frames")})
    write(record_path, record)
    ledger.add(job, kind="video", event="done", key=f"{key}:{entry['id']}", generation_id=record["generation_id"])
    log(f"{key}:{entry['id']}: patch done ({info['duration']:.2f} s)")
    return {**record, "reused": False}


def render_all(job: Path, state: dict, row: dict, source_id: str, seg: dict, style: dict) -> list[dict]:
    key = f"{source_id}:{seg['id']}"
    stale = [p for p in load(job).get(key, []) if p.get("base") != segment_record(job, key).get("generation_id")]
    for entry in stale:
        log(f"{key}:{entry['id']}: patch was made for an earlier render of {seg['id']}; dropped")
    results = []
    for entry in active(job, key):
        try:
            results.append({"key": f"{key}:{entry['id']}", **render_patch(job, state, row, source_id, seg, entry, style)})
        except Exception as error:
            results.append({"key": f"{key}:{entry['id']}", "error": f"{type(error).__name__}: {error}"})
    return results


def colour_fit(base: np.ndarray, patch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Gain/offset per channel that makes the patch's overlap frames look like the segment's."""
    from reskin.assemble import GAIN_LIMITS, OFFSET_LIMIT, stats
    mean_b, std_b = stats(base)
    mean_p, std_p = stats(patch)
    gain = np.clip(std_b / std_p, *GAIN_LIMITS)
    offset = np.clip(mean_b - gain * mean_p, -OFFSET_LIMIT, OFFSET_LIMIT)
    return gain, offset


def apply(job: Path, source_id: str, row: dict, line: dict, clip: Path, out_fps: float, size: tuple[int, int],
          ignore: list[list[float]]) -> tuple[Path, list[dict]]:
    """The segment's work clip with its patches switched in; returns the clip and the measured joins per patch
    (times in frames of the delivered video's clock, via `line`)."""
    from reskin.assemble import BLEND_FRAMES, choose_join, frames_rgb, jump_verdict, lut, normalise
    key = f"{source_id}:{line['id']}"
    def rendered(entry):
        path = state_path(job, key, entry["id"])
        return path.is_file() and read(path).get("status") == "done"
    entries = [entry for entry in active(job, key) if rendered(entry)]
    if not entries:
        return clip, []
    fps = row["fps"]
    first = round(row["keep"][0] * fps)
    position = lambda frame: round((frame - first) * out_fps / fps)
    total = line["out"][1] - line["out"][0]
    half = BLEND_FRAMES // 2
    overlap = max(2, round(OVERLAP * out_fps))
    current, joins = clip, []
    applied = []
    for entry in entries:
        record = read(state_path(job, key, entry["id"]))
        pa, pb = position(entry["render_frames"][0]), position(entry["render_frames"][1])
        local0, local1 = pa - line["out"][0], pb - line["out"][0]
        length = pb - pa
        work = job / "work" / f"{source_id}-{line['id']}-{entry['id']}.mp4"
        pstamp = {"ai": record["generation_id"], "frames": length, "fps": out_fps, "size": list(size)}
        if not (work.is_file() and work.with_suffix(".json").is_file() and read(work.with_suffix(".json")) == pstamp):
            normalise(Path(record["output"]), length, length / out_fps, out_fps, size, work)
            write(work.with_suffix(".json"), pstamp)
        tail = local1 >= total          # the patch runs to the end of the video: no join out
        base_in = frames_rgb(current, local0, overlap, out_fps)
        patch_in = frames_rgb(work, 0, overlap, out_fps)
        if tail:
            gain, offset = colour_fit(base_in, patch_in)
        else:
            base_out = frames_rgb(current, local1 - overlap, overlap, out_fps)
            patch_out = frames_rgb(work, length - overlap, overlap, out_fps)
            gain, offset = colour_fit(np.concatenate([base_in, base_out]), np.concatenate([patch_in, patch_out]))
        identity = (np.ones(3), np.zeros(3))
        join_in = choose_join(base_in, patch_in, identity, (gain, offset), ignore)
        join_out = None if tail else choose_join(patch_out, base_out, (gain, offset), identity, ignore)
        a_end = local0 + join_in["frame"] + half
        p_start = join_in["frame"] - half
        p_end = length if tail else length - (overlap - join_out["frame"] - half)
        blend = BLEND_FRAMES / out_fps
        len_a, len_p = a_end, p_end - p_start
        graph = (f"[0:v]trim=start_frame=0:end_frame={a_end},setpts=PTS-STARTPTS[a];"
                 f"[1:v]trim=start_frame={p_start}:end_frame={p_end},setpts=PTS-STARTPTS,format=rgb24,"
                 f"{lut(gain, offset)},format=yuv420p[p];")
        if tail:
            graph += (f"[a][p]xfade=transition=fade:duration={blend:.6f}:offset={(len_a / out_fps) - blend:.6f},"
                      f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={total},setpts=PTS-STARTPTS,"
                      f"format=yuv420p[v]")
        else:
            b_start = local1 - overlap + join_out["frame"] - half
            graph += (f"[0:v]trim=start_frame={b_start},setpts=PTS-STARTPTS[b];"
                      f"[a][p]xfade=transition=fade:duration={blend:.6f}:offset={(len_a / out_fps) - blend:.6f}[ap];"
                      f"[ap][b]xfade=transition=fade:duration={blend:.6f}:"
                      f"offset={((len_a + len_p - BLEND_FRAMES) / out_fps) - blend:.6f},"
                      f"trim=end_frame={total},setpts=PTS-STARTPTS,format=yuv420p[v]")
        applied.append(record["generation_id"])
        patched = job / "work" / f"{source_id}-{line['id']}-patched-{len(applied)}.mp4"
        run([FFMPEG, "-v", "error", "-y", "-i", current, "-i", work, "-filter_complex", graph, "-map", "[v]",
             "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-r", str(out_fps), patched])
        offset_frames = line["out"][0]      # frames on the delivered clock (0 = the kept start)
        joins.append({"patch": entry["id"], "segment": line["id"], "reason": entry.get("reason", ""),
                      "in": {**{k: join_in[k] for k in ("mismatch", "motion", "local")}, "jump": jump_verdict(join_in),
                             "frame": offset_frames + local0 + join_in["frame"]},
                      "out": None if tail else {**{k: join_out[k] for k in ("mismatch", "motion", "local")},
                                                "jump": jump_verdict(join_out),
                                                "frame": offset_frames + local1 - overlap + join_out["frame"]},
                      "start_frame": offset_frames + local0, "end_frame": offset_frames + local1})
        current = patched
    return current, joins
