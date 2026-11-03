"""Put the restyled segments back on the source's clock: retime, scale to 1080p, match colours across seams inside a
shot, cross-fade their overlaps, join, and lay the source's own audio under it."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from reskin.common import FFMPEG, log, read, run, video_info, write

GAIN_LIMITS = (.8, 1.25)
OFFSET_LIMIT = 40.0


def target_size(width: int, height: int, resolution: str) -> tuple[int, int]:
    short = 1080 if resolution == "1080p" else 720
    if width >= height:
        return round(short * width / height / 2) * 2, short
    return short, round(short * height / width / 2) * 2


def timeline(row: dict, out_fps: float) -> list[dict]:
    """Output frame ranges of every segment on one global clock, so rounding never drifts."""
    fps = row["fps"]
    first = round(row["keep"][0] * fps)
    position = lambda frame: round((frame - first) * out_fps / fps)
    rows = []
    for seg in row["segments"]:
        a, b = seg["render_frames"]
        rows.append({"id": seg["id"], "seam": seg["seam"], "out": [position(a), position(b)],
                     "body": [position(seg["frames"][0]), position(seg["frames"][1])]})
    return rows


def normalise(ai: Path, frames: int, seconds: float, out_fps: float, size: tuple[int, int], output: Path) -> Path:
    """Retime the AI clip to the source duration exactly, at the output rate and size, `frames` frames long."""
    info = video_info(ai)
    factor = seconds / info["duration"]
    chain = (f"setpts=(PTS-STARTPTS)*{factor:.8f},fps={out_fps},scale={size[0]}:{size[1]}:flags=lanczos,setsar=1,"
             f"tpad=stop_mode=clone:stop=4,trim=end_frame={frames},setpts=PTS-STARTPTS,format=yuv420p")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial.mp4")
    run([FFMPEG, "-v", "error", "-y", "-i", ai, "-vf", chain, "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "12",
         "-r", str(out_fps), partial])
    partial.replace(output)
    return output


def frames_rgb(path: Path, start: int, count: int, out_fps: float, width: int = 96) -> np.ndarray:
    raw = run([FFMPEG, "-v", "error", "-i", path, "-vf",
               f"select='between(n\\,{start}\\,{start + count - 1})',scale={width}:-2:flags=area,format=rgb24",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-"], binary=True)
    info = video_info(path)
    height = round(width * info["height"] / info["width"] / 2) * 2
    data = np.frombuffer(raw, np.uint8)
    n = data.size // (width * height * 3)
    return data[: n * width * height * 3].reshape(n, height, width, 3).astype(np.float32)


def stats(pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    flat = pixels.reshape(-1, 3)
    return flat.mean(axis=0), flat.std(axis=0) + 1e-3


def lut(gain: np.ndarray, offset: np.ndarray) -> str:
    parts = [f"{c}='clip(val*{g:.5f}{o:+.3f}\\,0\\,255)'" for c, g, o in zip("rgb", gain, offset)]
    return "lutrgb=" + ":".join(parts)


SEAM_WEIGHT = 1.0      # pixel levels: how much a seam mismatch costs
OFFSET_WEIGHT = .15    # a 10-level offset costs as much as a 1.5-level seam mismatch
GAIN_WEIGHT = 15.0     # a 0.1 gain change likewise


def solve_run(boundaries: list[dict], count: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Gain/offset per clip of one run of seams inside a shot, by least squares: the colours on both sides of every
    seam meet, while every clip stays as close to its own look as it can (no drift towards the first clip)."""
    gains, offsets = np.ones((count, 3)), np.zeros((count, 3))
    for channel in range(3):
        rows, targets = [], []
        for seam in boundaries:
            k = seam["index"]
            row = np.zeros(2 * count)
            row[2 * (k - 1)], row[2 * (k - 1) + 1] = seam["mean_before"][channel], 1
            row[2 * k], row[2 * k + 1] = -seam["mean_after"][channel], -1
            rows.append(row * SEAM_WEIGHT); targets.append(0.0)
            row = np.zeros(2 * count)
            row[2 * (k - 1)], row[2 * k] = seam["std_before"][channel], -seam["std_after"][channel]
            rows.append(row * SEAM_WEIGHT); targets.append(0.0)
        for k in range(count):
            row = np.zeros(2 * count); row[2 * k] = GAIN_WEIGHT
            rows.append(row); targets.append(GAIN_WEIGHT)
            row = np.zeros(2 * count); row[2 * k + 1] = OFFSET_WEIGHT
            rows.append(row); targets.append(0.0)
        solution = np.linalg.lstsq(np.array(rows), np.array(targets), rcond=None)[0]
        gains[:, channel] = np.clip(solution[0::2], *GAIN_LIMITS)
        offsets[:, channel] = np.clip(solution[1::2], -OFFSET_LIMIT, OFFSET_LIMIT)
    return [(gains[k], offsets[k]) for k in range(count)]


def match_colours(clips: list[Path], lines: list[dict], out_fps: float) -> tuple[list[tuple], list[dict]]:
    """Gain/offset per clip so the colours meet across every seam inside a shot (hard cuts may change the look)."""
    corrections: list[tuple] = [(np.ones(3), np.zeros(3)) for _ in clips]
    runs: list[list[int]] = []
    for index, line in enumerate(lines):
        if index and line["seam"] == "continuous":
            runs[-1].append(index)
        else:
            runs.append([index])
    seams = []
    for members in runs:
        if len(members) < 2:
            continue
        boundaries = []
        for position, index in enumerate(members[1:], 1):
            line, prev_line = lines[index], lines[index - 1]
            count = max(2, line["body"][0] - line["out"][0])
            prev_count = prev_line["out"][1] - prev_line["out"][0]
            mean_b, std_b = stats(frames_rgb(clips[index - 1], prev_count - count, count, out_fps))
            mean_a, std_a = stats(frames_rgb(clips[index], 0, count, out_fps))
            boundaries.append({"index": position, "id": line["id"], "mean_before": mean_b, "std_before": std_b,
                               "mean_after": mean_a, "std_after": std_a})
        solved = solve_run(boundaries, len(members))
        for position, index in enumerate(members):
            corrections[index] = solved[position]
        for seam in boundaries:
            (gb, ob), (ga, oa) = solved[seam["index"] - 1], solved[seam["index"]]
            before = gb * seam["mean_before"] + ob
            after = ga * seam["mean_after"] + oa
            seams.append({"before": seam["id"],
                          "raw_difference": round(float(np.abs(seam["mean_after"] - seam["mean_before"]).mean()), 2),
                          "residual": round(float(np.abs(after - before).mean()), 2),
                          "gain": [round(float(g), 3) for g in ga], "offset": [round(float(o), 2) for o in oa]})
    return corrections, seams


def assemble_source(job: Path, state: dict, source_id: str, row: dict, style: dict) -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    info = video_info(Path(source["path"]))
    out_fps = min(float(state["options"]["fps"]), round(row["fps"], 3))
    size = target_size(row["width"], row["height"], state["options"]["resolution"])
    lines = timeline(row, out_fps)
    clips = []
    for line in lines:
        ai = job / "ai" / f"{source_id}-{line['id']}.mp4"
        record_path = job / "state/render" / f"{source_id}-{line['id']}.json"
        record = read(record_path) if record_path.is_file() else {}
        if not ai.is_file() or record.get("status") != "done":
            raise ValueError(f"{source_id}:{line['id']} is not rendered yet; run render first")
        frames = line["out"][1] - line["out"][0]
        seconds = frames / out_fps
        work = job / "work" / f"{source_id}-{line['id']}.mp4"
        stamp = {"ai": record.get("generation_id"), "frames": frames, "fps": out_fps, "size": list(size)}
        stamp_path = work.with_suffix(".json")
        if not (work.is_file() and stamp_path.is_file() and read(stamp_path) == stamp):
            normalise(ai, frames, seconds, out_fps, size, work)
            write(stamp_path, stamp)
        clips.append(work)
    corrections, seams = match_colours(clips, lines, out_fps)
    # One filter graph: corrected clips, cross-faded inside shots, joined at cuts, with the source audio.
    inputs, graph = [], []
    for index, (clip, (gain, offset)) in enumerate(zip(clips, corrections)):
        inputs += ["-i", clip]
        chain = f"[{index}:v]setpts=PTS-STARTPTS"
        if not (np.allclose(gain, 1) and np.allclose(offset, 0)):
            chain += f",format=rgb24,{lut(gain, offset)},format=yuv420p"
        graph.append(chain + f"[c{index}]")
    runs: list[list[int]] = []
    for index, line in enumerate(lines):
        if index and line["seam"] == "continuous":
            runs[-1].append(index)
        else:
            runs.append([index])
    labels = []
    for number, members in enumerate(runs):
        label = f"c{members[0]}"
        length = (lines[members[0]]["out"][1] - lines[members[0]]["out"][0]) / out_fps
        for index in members[1:]:
            overlap = (lines[index]["body"][0] - lines[index]["out"][0]) / out_fps
            clip_length = (lines[index]["out"][1] - lines[index]["out"][0]) / out_fps
            if overlap > 0:
                graph.append(f"[{label}][c{index}]xfade=transition=fade:duration={overlap:.6f}:offset={length - overlap:.6f}[x{index}]")
                length = length + clip_length - overlap
            else:
                graph.append(f"[{label}][c{index}]concat=n=2:v=1:a=0[x{index}]")
                length += clip_length
            label = f"x{index}"
        labels.append(label)
    total = lines[-1]["out"][1] - lines[0]["out"][0]
    if len(labels) > 1:
        graph.append("".join(f"[{label}]" for label in labels) + f"concat=n={len(labels)}:v=1:a=0[joined]")
        final = "joined"
    else:
        final = labels[0]
    graph.append(f"[{final}]trim=end_frame={total},setpts=PTS-STARTPTS[vout]")
    keep_start, keep_end = row["keep"]
    audio = []
    if info["audio"]:
        index = len(clips)
        inputs += ["-i", source["path"]]
        fade = []
        if keep_end < info["duration"] - .1:
            fade.append(f"afade=t=out:st={max(0.0, keep_end - keep_start - .35):.3f}:d=0.35")
        if keep_start > .05:
            fade.append("afade=t=in:st=0:d=0.15")
        graph.append(f"[{index}:a]atrim=start={keep_start:.6f}:end={keep_end:.6f},asetpts=PTS-STARTPTS"
                     + "".join("," + f for f in fade) + "[aout]")
        audio = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    delivery = Path(state["delivery_dir"])
    names = [r["name"] for r in state["sources"]]
    name = source["name"] + ("" if names.count(source["name"]) == 1 else f"_{source_id}")
    output = delivery / f"{name}_{style.get('label', 'reskin')}.mp4"
    partial = output.with_name(output.stem + ".partial.mp4")
    run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", *audio,
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-profile:v", "high", "-pix_fmt", "yuv420p",
         "-r", str(out_fps), "-movflags", "+faststart", "-shortest", partial])
    partial.replace(output)
    made = video_info(output)
    expected = total / out_fps
    problems = []
    if abs(made["duration"] - expected) > 2 / out_fps + .05:
        problems.append(f"duration {made['duration']:.3f} s, expected {expected:.3f} s")
    if (made["width"], made["height"]) != size:
        problems.append(f"size {made['width']}x{made['height']}, expected {size[0]}x{size[1]}")
    seam_times = [{"segment": line["id"], "seam": line["seam"],
                   "time": round((line["body"][0] - lines[0]["out"][0]) / out_fps, 3)} for line in lines[1:]]
    result = {"source": source_id, "output": str(output), "duration": round(made["duration"], 3),
              "size": [made["width"], made["height"]], "fps": out_fps, "seams": seam_times, "colour": seams,
              "problems": problems, "keep": row["keep"],
              "segments": [{"id": line["id"], "start": round((line["body"][0] - lines[0]["out"][0]) / out_fps, 3),
                            "end": round((line["body"][1] - lines[0]["out"][0]) / out_fps, 3)} for line in lines]}
    write(job / "state/assembled" / f"{source_id}.json", result)
    log(f"{source_id}: assembled {output.name} ({made['width']}x{made['height']}, {made['duration']:.2f} s)")
    return result
