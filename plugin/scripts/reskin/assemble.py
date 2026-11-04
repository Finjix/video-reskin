"""Put the restyled segments back on the source's clock: retime, scale to 1080p, match colours across seams inside a
shot, switch between segments at their best-matching frame, copy the screen-fixed HUD back from the original, and
lay the source's own audio under it. Every seam is measured and shown on a sheet."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from reskin import patch as PATCH
from reskin.common import FFMPEG, digest, log, read, run, video_info, write

GAIN_LIMITS = (.8, 1.25)
OFFSET_LIMIT = 40.0
BLEND_FRAMES = 4        # a seam inside a shot switches over this many frames, at the best-matching moment
JUMP_LIMIT = 10.0       # grey levels at the join beyond the picture's own motion that a viewer sees as a jump


def target_size(width: int, height: int, resolution: str) -> tuple[int, int]:
    short = {"480p": 480, "720p": 720, "1080p": 1080}[resolution]
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


def runs_of(lines: list[dict]) -> list[list[int]]:
    """Indices of segments joined by seams inside a shot; a hard cut starts a new run."""
    runs: list[list[int]] = []
    for index, line in enumerate(lines):
        if index and line["seam"] == "continuous":
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


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


def match_colours(clips: list[Path], lines: list[dict], out_fps: float) -> tuple[list[tuple], list[dict], dict]:
    """Gain/offset per clip so the colours meet across every seam inside a shot (hard cuts may change the look).

    Also returns the overlap frames of every such seam (both versions of the same moments) for choosing the join."""
    corrections: list[tuple] = [(np.ones(3), np.zeros(3)) for _ in clips]
    seams, overlaps = [], {}
    for members in runs_of(lines):
        if len(members) < 2:
            continue
        boundaries = []
        for position, index in enumerate(members[1:], 1):
            line, prev_line = lines[index], lines[index - 1]
            count = max(2, line["body"][0] - line["out"][0])
            prev_count = prev_line["out"][1] - prev_line["out"][0]
            before = frames_rgb(clips[index - 1], prev_count - count, count, out_fps)
            after = frames_rgb(clips[index], 0, count, out_fps)
            overlaps[index] = (before, after)
            mean_b, std_b = stats(before)
            mean_a, std_a = stats(after)
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
    return corrections, seams, overlaps


def choose_join(before: np.ndarray, after: np.ndarray, correction_before: tuple, correction_after: tuple,
                ignore: list[list[float]] | None = None) -> dict:
    """The overlap frame where both versions look most alike (after colour matching), and how alike they are.

    `mismatch` is the mean grey difference there (0-255); `motion` the picture's own change from frame to frame,
    so a mismatch well above the motion is a visible jump."""
    def grey(frames, correction):
        gain, offset = correction
        return np.clip(frames * gain + offset, 0, 255).mean(axis=3)
    a, b = grey(before, correction_before), grey(after, correction_after)
    count = min(len(a), len(b))
    half = BLEND_FRAMES // 2
    differences = np.abs(a[:count] - b[:count]).mean(axis=(1, 2))
    lo = min(half, count - 1)
    hi = max(lo + 1, count - half)
    best = lo + int(np.argmin(differences[lo:hi]))
    steps = np.abs(np.diff(a[:count], axis=0)).mean(axis=(1, 2)) if count > 1 else np.zeros(1)
    return {"frame": best, "mismatch": round(float(differences[best]), 2), "motion": round(float(np.median(steps)), 2),
            "worst_in_overlap": round(float(differences.max()), 2),
            "local": local_jump(before[:count], after[:count], best, correction_before, correction_after, ignore)}


def local_jump(before: np.ndarray, after: np.ndarray, frame: int, correction_before: tuple,
               correction_after: tuple, ignore: list[list[float]] | None = None, rows: int = 10,
               columns: int = 6) -> float:
    """The largest colour change of one region of the picture at the switch, beyond that region's own motion: a
    fish that turns from yellow to silver barely moves the whole-frame figure but stands out here."""
    a = np.clip(before * correction_before[0] + correction_before[1], 0, 255)
    b = np.clip(after * correction_after[0] + correction_after[1], 0, 255)
    height, width = a.shape[1:3]
    if height < rows or width < columns:
        return 0.0
    by, bx = height // rows, width // columns
    a, b = a[:, : by * rows, : bx * columns], b[:, : by * rows, : bx * columns]
    blocks = np.abs(a[frame] - b[frame]).reshape(rows, by, columns, bx, 3).mean(axis=(1, 3, 4))
    if len(a) > 1:
        steps = np.abs(np.diff(a, axis=0)).reshape(len(a) - 1, rows, by, columns, bx, 3).mean(axis=(2, 4, 5))
        motion = np.median(steps, axis=0)
    else:
        motion = np.zeros_like(blocks)
    excess = blocks - 1.5 * motion
    # Regions the HUD copy covers do not count: their pixels come from the original anyway.
    for box in ignore or []:
        for r in range(rows):
            for c in range(columns):
                cy, cx = (r + .5) / rows, (c + .5) / columns
                if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                    excess[r, c] = 0.0
    return round(float(excess.max()), 2)


LOCAL_LIMIT = 30.0      # colour levels one region may jump beyond its own motion before a viewer sees it at once


def jump_verdict(join: dict) -> str:
    excess = join["mismatch"] - 1.5 * join["motion"]
    local = join.get("local", 0.0)
    if excess > JUMP_LIMIT or local > LOCAL_LIMIT:
        return "visible"
    return "slight" if excess > JUMP_LIMIT / 2 or local > LOCAL_LIMIT / 2 else "clean"


def seam_sheet(job: Path, source_id: str, clips: list[Path], lines: list[dict], joins: dict,
               trims: list[list[int]], out_fps: float) -> Path | None:
    """Per seam: the last frame before the switch and the first after it, side by side, with the measured jump."""
    from PIL import Image, ImageDraw
    from reskin.analyze import font
    if len(lines) < 2:
        return None
    half = BLEND_FRAMES // 2
    rows = []
    for index in range(1, len(lines)):
        if index in joins:
            prev_frame, next_frame = trims[index - 1][1] - half - 1, trims[index][0] + half
        else:
            prev_frame, next_frame = trims[index - 1][1] - 1, trims[index][0]
        a = frames_rgb(clips[index - 1], prev_frame, 1, out_fps, width=360)[0]
        b = frames_rgb(clips[index], next_frame, 1, out_fps, width=360)[0]
        rows.append((lines[index], joins.get(index), a.astype(np.uint8), b.astype(np.uint8)))
    height, width = rows[0][2].shape[:2]
    label = font(18)
    canvas = Image.new("RGB", (2 * width + 10, len(rows) * (height + 30)), "white")
    draw = ImageDraw.Draw(canvas)
    for number, (line, join, a, b) in enumerate(rows):
        y = number * (height + 30)
        canvas.paste(Image.fromarray(a), (0, y + 28))
        canvas.paste(Image.fromarray(b), (width + 10, y + 28))
        when = (line["body"][0] - lines[0]["out"][0]) / out_fps
        text = (f"{line['id']} ~{when:.2f} s inside a shot: jump {join['mismatch']} vs motion {join['motion']}, local {join['local']} "
                f"({jump_verdict(join)})" if join else f"{line['id']} at {when:.2f} s: hard cut")
        draw.text((4, y + 4), text, fill="black", font=label)
    out = job / "sheets" / f"{source_id}-seams.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=88)
    return out


def output_spec(state: dict, row: dict) -> tuple[float, tuple[int, int]]:
    """Output frame rate and size of a source's delivery (and of its normalised work clips)."""
    return (min(float(state["options"]["fps"]), round(row["fps"], 3)),
            target_size(row["width"], row["height"], state["options"]["resolution"]))


def work_clip(job: Path, source_id: str, line: dict, out_fps: float, size: tuple[int, int]) -> Path:
    """The segment's Seedance result on the source clock (retimed, output size), made once per generation."""
    ai = job / "ai" / f"{source_id}-{line['id']}.mp4"
    record_path = job / "state/render" / f"{source_id}-{line['id']}.json"
    record = read(record_path) if record_path.is_file() else {}
    if not ai.is_file() or record.get("status") != "done":
        raise ValueError(f"{source_id}:{line['id']} is not rendered yet; run render first")
    frames = line["out"][1] - line["out"][0]
    work = job / "work" / f"{source_id}-{line['id']}.mp4"
    stamp = {"ai": record.get("generation_id"), "frames": frames, "fps": out_fps, "size": list(size)}
    stamp_path = work.with_suffix(".json")
    try:
        current = work.is_file() and stamp_path.is_file() and read(stamp_path) == stamp
    except ValueError:                # a cache stamp a crash left unreadable: make the clip again
        current = False
    if not current:
        normalise(ai, frames, frames / out_fps, out_fps, size, work)
        write(stamp_path, stamp)
    return work


def ignore_boxes(row: dict) -> list[list[float]]:
    """Regions whose pixels come from the original anyway (HUD, burned overlays): seam measures skip them."""
    from reskin import overlay as OVERLAY
    return ([item["box"] for item in row.get("hud") or []]
            + [list(OVERLAY.region(item)) for item in row.get("overlays") or []])


def measure_seam(before_clip: Path, after_clip: Path, before_line: dict, after_line: dict, out_fps: float,
                 ignore: list[list[float]] | None = None) -> dict:
    """One seam inside a shot measured on its own (colours matched for this pair only): the join `assemble` would
    pick, how far the two versions differ there, and the verdict (clean, slight, visible)."""
    count = max(2, after_line["body"][0] - after_line["out"][0])
    prev_count = before_line["out"][1] - before_line["out"][0]
    before = frames_rgb(before_clip, prev_count - count, count, out_fps)
    after = frames_rgb(after_clip, 0, count, out_fps)
    mean_b, std_b = stats(before)
    mean_a, std_a = stats(after)
    solved = solve_run([{"index": 1, "id": after_line["id"], "mean_before": mean_b, "std_before": std_b,
                         "mean_after": mean_a, "std_after": std_a}], 2)
    join = choose_join(before, after, solved[0], solved[1], ignore)
    return {**join, "jump": jump_verdict(join)}


def inputs_digest(job: Path, state: dict, source_id: str, row: dict) -> str:
    """What the delivered video is made of: the generations, the plan row and the output options."""
    generations = []
    for seg in row["segments"]:
        record_path = job / "state/render" / f"{source_id}-{seg['id']}.json"
        generations.append(read(record_path).get("generation_id") if record_path.is_file() else None)
        for entry in PATCH.active(job, f"{source_id}:{seg['id']}"):
            patch_path = PATCH.state_path(job, f"{source_id}:{seg['id']}", entry["id"])
            if patch_path.is_file() and read(patch_path).get("status") == "done":
                generations.append(read(patch_path)["generation_id"])
    return digest({"generations": generations, "row": row, "fps": state["options"]["fps"],
                   "resolution": state["options"]["resolution"], "version": ASSEMBLY_VERSION})


def current(job: Path, state: dict, source_id: str, row: dict) -> dict | None:
    """The last assembled result when nothing it is made of has changed since (and the file is still there)."""
    record_path = job / "state/assembled" / f"{source_id}.json"
    if not record_path.is_file():
        return None
    record = read(record_path)
    if record.get("inputs") == inputs_digest(job, state, source_id, row) and Path(record["output"]).is_file():
        return record
    return None


FEATHER = .02            # feathered edge of a post-render wipe, as a fraction of the frame width
ASSEMBLY_VERSION = 4     # raise when the assembly itself changes, so delivered videos are made again


def post_wipes(job: Path, source_id: str, source_path: str, row: dict, size: tuple[int, int]) -> list[dict]:
    """`remove` boxes painted over in the delivered video too: a neighbouring strip of the restyled frame (the
    plainest one on the original frame there), mirrored, slightly larger than the box and feathered, so neither a
    logo the model drew anyway nor the edge of the pre-render wipe shows. Returns per box the patch rectangle, the
    strip to copy, the flip, the delivered-clock times and a feather mask PNG."""
    from reskin import render as RENDER
    width, height = size
    margin = max(4, round(FEATHER * width))
    keep_start, keep_end = row["keep"]
    wipes = []
    for n, item in enumerate(row.get("remove") or []):
        if not (isinstance(item, dict) and item.get("box")):
            continue
        a, b = max(keep_start, float(item.get("start", 0))), min(keep_end, float(item.get("end", keep_end)))
        if b - a < RENDER.MIN_WIPE:
            continue
        if item.get("wipe", "both") != "after":
            continue        # wiped before the render (feathered): nothing to paint over
        x0, y0, x1, y1 = item["box"]
        grown = [max(0.0, x0 - margin / width), max(0.0, y0 - margin / height),
                 min(1.0, x1 + margin / width), min(1.0, y1 + margin / height)]
        raw = run([FFMPEG, "-v", "error", "-ss", f"{(a + b) / 2:.3f}", "-i", source_path, "-frames:v", "1", "-vf",
                   f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
        frame = (np.frombuffer(raw[: width * height * 3], dtype=np.uint8).reshape(height, width, 3)
                 if len(raw) >= width * height * 3 else None)
        sx, sy, flip = RENDER.wipe_sources([grown + [a, b]], width, height, frame)[0]
        x, y, w, h = RENDER._pixels(grown, width, height)
        path = RENDER.feather_mask(x, y, w, h, width, height, job / "work" / f"{source_id}-wipe{n}.png")
        blur = 0            # a blurred fill read as a censored rectangle
        wipes.append({"x": x, "y": y, "w": w, "h": h, "sx": sx, "sy": sy, "flip": flip, "blur": blur,
                      "start": round(a - keep_start, 3), "end": round(b - keep_start, 3), "mask": str(path),
                      "what": item.get("what", "")})
    return wipes


def assemble_source(job: Path, state: dict, source_id: str, row: dict, style: dict) -> dict:
    from reskin import hud as HUD, overlay as OVERLAY
    source = next(r for r in state["sources"] if r["id"] == source_id)
    info = video_info(Path(source["path"]))
    out_fps, size = output_spec(state, row)
    lines = timeline(row, out_fps)
    clips, patches = [], []
    for line in lines:
        clip, measured = PATCH.apply(job, source_id, row, line, work_clip(job, source_id, line, out_fps, size),
                                     out_fps, size, ignore_boxes(row))
        clips.append(clip)
        patches += measured
    corrections, colour, overlaps = match_colours(clips, lines, out_fps)
    # Where each clip is cut: a seam inside a shot switches at its best-matching overlap frame with a short blend
    # (both versions there show the same moment); hard cuts join end to end.
    half = BLEND_FRAMES // 2
    trims = [[0, line["out"][1] - line["out"][0]] for line in lines]
    joins = {}
    for index, (before, after) in overlaps.items():
        join = choose_join(before, after, corrections[index - 1], corrections[index], ignore_boxes(row))
        overlap = lines[index]["body"][0] - lines[index]["out"][0]
        trims[index - 1][1] -= overlap - join["frame"] - half
        trims[index][0] = join["frame"] - half
        joins[index] = join
    inputs, graph = [], []
    for index, (clip, (gain, offset)) in enumerate(zip(clips, corrections)):
        inputs += ["-i", clip]
        chain = f"[{index}:v]trim=start_frame={trims[index][0]}:end_frame={trims[index][1]},setpts=PTS-STARTPTS"
        if not (np.allclose(gain, 1) and np.allclose(offset, 0)):
            chain += f",format=rgb24,{lut(gain, offset)},format=yuv420p"
        graph.append(chain + f"[c{index}]")
    labels = []
    blend = BLEND_FRAMES / out_fps
    for members in runs_of(lines):
        label = f"c{members[0]}"
        length = (trims[members[0]][1] - trims[members[0]][0]) / out_fps
        for index in members[1:]:
            graph.append(f"[{label}][c{index}]xfade=transition=fade:duration={blend:.6f}:offset={length - blend:.6f}[x{index}]")
            length += (trims[index][1] - trims[index][0]) / out_fps - blend
            label = f"x{index}"
        labels.append(label)
    total = lines[-1]["out"][1] - lines[0]["out"][0]
    if len(labels) > 1:
        graph.append("".join(f"[{label}]" for label in labels) + f"concat=n={len(labels)}:v=1:a=0[joined]")
        final = "joined"
    else:
        final = labels[0]
    graph.append(f"[{final}]trim=end_frame={total},setpts=PTS-STARTPTS[v0]")
    keep_start, keep_end = row["keep"]
    source_index = len(clips)
    inputs += ["-i", source["path"]]
    # Screen-fixed HUD comes back from the original, frame by frame, through a matte of its still pixels.
    huds = [HUD.matte(Path(source["path"]), row["keep"], item, size, job / "work" / f"{source_id}-hud{n}.png")
            for n, item in enumerate(row.get("hud") or [], 1)]
    current = "v0"
    # Logos and warning lines: painted over after the render as well (see post_wipes).
    wipes = post_wipes(job, source_id, source["path"], row, size)
    for n, item in enumerate(wipes):
        inputs += ["-loop", "1", "-framerate", str(out_fps), "-t", f"{total / out_fps + 1:.3f}", "-i", item["mask"]]
        mask_index = sum(1 for part in inputs if part == "-i") - 1
        graph.append(f"[{current}]split[wa{n}][wb{n}]")
        soften = f",gblur=sigma={item['blur']}" if item.get("blur") else ""
        graph.append(f"[wb{n}]crop={item['w']}:{item['h']}:{item['sx']}:{item['sy']},{item['flip']}{soften}[wp{n}]")
        graph.append(f"[{mask_index}:v]format=gray[wm{n}]")
        graph.append(f"[wp{n}][wm{n}]alphamerge[wq{n}]")
        graph.append(f"[wa{n}][wq{n}]overlay=x={item['x']}:y={item['y']}:eof_action=pass:"
                     f"enable='between(t\\,{item['start']:.3f}\\,{item['end']:.3f})'[wv{n}]")
        current = f"wv{n}"
    if huds:
        graph.append(f"[{source_index}:v]trim=start={keep_start:.6f}:end={keep_end:.6f},setpts=PTS-STARTPTS,"
                     f"fps={out_fps},scale={size[0]}:{size[1]}:flags=bicubic,format=yuv420p,split={len(huds)}"
                     + "".join(f"[o{n}]" for n in range(len(huds))))
        for n, item in enumerate(huds):
            inputs += ["-loop", "1", "-framerate", str(out_fps), "-t", f"{total / out_fps + 1:.3f}", "-i", item["mask"]]
            mask_index = sum(1 for part in inputs if part == "-i") - 1
            start, end = item["start"] - keep_start, item["end"] - keep_start
            graph.append(f"[o{n}]crop={item['w']}:{item['h']}:{item['x']}:{item['y']}[h{n}]")
            graph.append(f"[{mask_index}:v]format=gray[m{n}]")
            graph.append(f"[h{n}][m{n}]alphamerge[ha{n}]")
            graph.append(f"[{current}][ha{n}]overlay=x={item['x']}:y={item['y']}:eof_action=pass:"
                         f"enable='between(t\\,{start:.3f}\\,{end:.3f})'[hv{n}]")
            current = f"hv{n}"
    graph.append(f"[{current}]format=yuv420p[vout]")
    audio_filter = None
    if info["audio"]:
        fade = []
        if keep_end < info["duration"] - .1:
            fade.append(f"afade=t=out:st={max(0.0, keep_end - keep_start - .35):.3f}:d=0.35")
        if keep_start > .05:
            fade.append("afade=t=in:st=0:d=0.15")
        audio_filter = (f"atrim=start={keep_start:.6f}:end={keep_end:.6f},asetpts=PTS-STARTPTS"
                        + "".join("," + f for f in fade))
    audio_codec = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    encode = ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-profile:v", "high", "-pix_fmt", "yuv420p",
              "-r", str(out_fps), "-frames:v", str(total), "-movflags", "+faststart"]
    delivery = Path(state["delivery_dir"])
    names = [r["name"] for r in state["sources"]]
    name = source["name"] + ("" if names.count(source["name"]) == 1 else f"_{source_id}")
    output = delivery / f"{name}_{style.get('label', 'reskin')}.mp4"
    partial = output.with_name(output.stem + ".partial.mp4")
    burned = []
    if row.get("overlays"):
        # Screen-fixed text and UI the program burns in (from the original, or drawn): a per-frame pass
        # between the assembled picture and the encoder; the audio is cut first, on its own.
        burned = OVERLAY.prepare(row["overlays"], (keep_start, keep_end), out_fps, size, total)
        extra, audio = [], []
        if audio_filter:
            sound = job / "work" / f"{source_id}-audio.m4a"
            run([FFMPEG, "-v", "error", "-y", "-i", source["path"], "-vn", "-af", audio_filter, *audio_codec, sound])
            extra, audio = ["-i", sound], ["-map", "1:a", "-c:a", "copy"]
        OVERLAY.burn([*inputs, "-filter_complex", ";".join(graph), "-map", "[vout]"], Path(source["path"]),
                     (keep_start, keep_end), out_fps, size, total, burned,
                     ["-map", "0:v", *audio, *encode, partial], extra_inputs=extra)
    else:
        audio = []
        if audio_filter:
            graph.append(f"[{source_index}:a]{audio_filter}[aout]")
            audio = ["-map", "[aout]", *audio_codec]
        run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", *audio,
             *encode, partial])
    try:
        partial.replace(output)
    except PermissionError:
        raise ValueError(f"{output.name} is open in another program (a video player?); close it and run assemble "
                         f"again (the new version waits in {partial.name})") from None
    made = video_info(output)
    expected = total / out_fps
    problems = []
    if abs(made["duration"] - expected) > 2 / out_fps + .05:
        problems.append(f"duration {made['duration']:.3f} s, expected {expected:.3f} s")
    if (made["width"], made["height"]) != size:
        problems.append(f"size {made['width']}x{made['height']}, expected {size[0]}x{size[1]}")
    seam_times = []
    for index, line in enumerate(lines[1:], 1):
        entry = {"segment": line["id"], "seam": line["seam"],
                 "time": round((line["body"][0] - lines[0]["out"][0]) / out_fps, 3)}
        if index in joins:
            join = joins[index]
            entry.update(join, time=round((line["out"][0] - lines[0]["out"][0] + join["frame"]) / out_fps, 3),
                         jump=jump_verdict(join))
        seam_times.append(entry)
    # The joins into and out of local patches are seams too (measured, reviewed and close-up checked like them).
    for patch in patches:
        for side in ("in", "out"):
            join = patch[side]
            if join is None:            # a patch that runs to the end has no join out
                continue
            seam_times.append({"segment": f"{patch['segment']}:{patch['patch']}:{side}", "seam": "continuous",
                               "patch": True, "time": round(join["frame"] / out_fps, 3),
                               **{k: join[k] for k in ("mismatch", "motion", "local", "jump")}})
    seam_times.sort(key=lambda entry: entry["time"])
    sheet = seam_sheet(job, source_id, clips, lines, joins, trims, out_fps)
    result = {"source": source_id, "inputs": inputs_digest(job, state, source_id, row), "output": str(output),
              "duration": round(made["duration"], 3),
              "size": [made["width"], made["height"]], "fps": out_fps, "seams": seam_times, "colour": colour,
              "problems": problems, "keep": row["keep"], "seam_sheet": str(sheet) if sheet else None,
              "hud": [{k: v for k, v in item.items() if k != "mask"} for item in huds],
              "wipes": [{k: v for k, v in item.items() if k != "mask"} for item in wipes],
              "overlays": OVERLAY.summary(burned),
              "patches": [{"key": f"{source_id}:{p['segment']}:{p['patch']}", "reason": p["reason"],
                           "start": round(p["start_frame"] / out_fps, 3), "end": round(p["end_frame"] / out_fps, 3),
                           "in": p["in"]["jump"], "out": p["out"]["jump"] if p["out"] else None,
                           "kept": p["in"]["jump"] != "visible" and (p["out"] or {}).get("jump") != "visible"}
                          for p in patches],
              "segments": [{"id": line["id"], "start": round((line["body"][0] - lines[0]["out"][0]) / out_fps, 3),
                            "end": round((line["body"][1] - lines[0]["out"][0]) / out_fps, 3)} for line in lines]}
    write(job / "state/assembled" / f"{source_id}.json", result)
    log(f"{source_id}: assembled {output.name} ({made['width']}x{made['height']}, {made['duration']:.2f} s)")
    return result


def _label(text: str, width: int, output: Path) -> Path:
    """A caption strip for one column of a comparison: white text on a dark translucent band."""
    from PIL import Image, ImageDraw
    from reskin.overlay import _font
    size = max(18, width // 18)
    font = _font("bold", size)
    band = Image.new("RGBA", (width, round(size * 1.8)), (0, 0, 0, 150))
    draw = ImageDraw.Draw(band)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2 - left, (band.height - (bottom - top)) / 2 - top), text,
              fill=(255, 255, 255, 255), font=font)
    output.parent.mkdir(parents=True, exist_ok=True)
    band.save(output)
    return output


def compare_source(job: Path, state: dict, source_id: str, layout: str = "left",
                   extras: list[Path] | None = None, labels: list[str] | None = None) -> dict:
    """The delivered video beside its original, on one clock, as a deliverable: `<delivered stem>_对比.mp4` in the
    delivery folder. Original on the left (or top), restyled on the right (or bottom), each side at the original's
    size (the restyle scaled up when the job renders smaller), with the delivered audio. `extras`: earlier versions
    on the delivered clock (e.g. a previous delivery), placed between the original and this result; `labels`: one
    caption per column (original, extras..., result) drawn at its top."""
    record_path = job / "state/assembled" / f"{source_id}.json"
    if not record_path.is_file():
        raise ValueError(f"{source_id} is not assembled yet; run assemble first")
    assembled = read(record_path)
    source = next(r for r in state["sources"] if r["id"] == source_id)
    result = Path(assembled["output"])
    if not result.is_file():
        raise ValueError(f"{result.name} is missing from the delivery folder; run assemble again")
    extras = [Path(p) for p in extras or []]
    for path in extras:
        if not path.is_file():
            raise ValueError(f"comparison video not found: {path}")
    columns = 2 + len(extras)
    if labels and len(labels) != columns:
        raise ValueError(f"{len(labels)} labels for {columns} columns (original, {len(extras)} extra, result)")
    output = result.with_name(result.stem + "_对比.mp4")
    wanted = digest({"result": [result.stat().st_size, result.stat().st_mtime],
                     "extras": [[str(p), p.stat().st_size, p.stat().st_mtime] for p in extras],
                     "labels": labels or [], "layout": layout})
    marker = job / "state/compare" / f"{source_id}.json"
    if output.is_file() and marker.is_file() and read(marker).get("digest") == wanted:
        return {"output": str(output), "reused": True}
    info = video_info(Path(source["path"]))
    width, height = info["width"] // 2 * 2, info["height"] // 2 * 2
    fps = assembled["fps"]
    start, end = assembled["keep"]
    stack = "hstack" if layout == "left" else "vstack"
    inputs = [source["path"], *extras, result]
    parts = [f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,fps={fps},"
             f"scale={width}:{height}:flags=bicubic,setsar=1[c0]"]
    for i in range(1, columns):
        parts.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}:flags=lanczos,setsar=1[c{i}]")
    names = [f"c{i}" for i in range(columns)]
    if labels:
        for i, text in enumerate(labels):
            inputs.append(_label(text, width, job / "work/compare" / f"{source_id}-label{i}.png"))
            parts.append(f"[c{i}][{columns + i}:v]overlay=0:0[l{i}]")
        names = [f"l{i}" for i in range(columns)]
    parts.append("".join(f"[{n}]" for n in names) + f"{stack}=inputs={columns}:shortest=1,format=yuv420p[v]")
    partial = output.with_name(output.stem + ".partial.mp4")
    audio = ["-map", f"{columns - 1}:a?", "-c:a", "copy"]
    command = [FFMPEG, "-v", "error", "-y"]
    for path in inputs:
        command += ["-i", path]
    run([*command, "-filter_complex", ";".join(parts), "-map", "[v]",
         *audio, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-shortest", partial])
    try:
        partial.replace(output)
    except PermissionError:
        raise ValueError(f"{output.name} is open in another program (a video player?); close it and run compare "
                         f"again (the new version waits in {partial.name})") from None
    write(marker, {"digest": wanted, "output": str(output)})
    made = video_info(output)
    log(f"{source_id}: compared {output.name} ({made['width']}x{made['height']}, {made['duration']:.2f} s)")
    return {"output": str(output), "reused": False, "size": [made["width"], made["height"]],
            "duration": made["duration"]}
