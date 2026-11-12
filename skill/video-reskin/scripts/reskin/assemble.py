"""Put the restyled segments back on the source's clock and hide the seams.

1. Retime: a result need not keep the source's length or pace exactly (Seedance 2.5 returns clips slightly short),
   so every clip plays at one constant speed whose ends are pinned to the source's moments at its seams (found by
   matching edge maps); the few holds or skips this needs go where the picture stands still.
2. Colours: gain/offset per clip so colours meet across every seam inside a shot (least squares per run).
3. Seams: at a camera cut the clips are simply joined. Inside a shot (where the camera moves fast, plan) both
   results cover an overlap; the switch goes to the frame there where the jump from the one to the other is
   smallest. When that jump is no larger than the picture's ordinary change from frame to frame there, and the two
   drawings are close, it is a plain cut; otherwise a very light dissolve over DISSOLVE_SECONDS, nothing more (the
   user's rule: no effects at seams unless needed, then only a slight dissolve).
4. Join with the source's own audio of the kept range. Passthrough footage (live action, non-game: plan) goes back in
   as the original picture at its place, joined to the restyled clips with plain cuts.
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from reskin.common import FFMPEG, log, read, run, video_info, write

GAIN_LIMITS = (.8, 1.25)
OFFSET_LIMIT = 40.0
GRID = (10, 6)              # rows, columns of the regions a seam is measured in
DISSOLVE_SECONDS = .25      # the light dissolve when a plain cut would show (6 frames at 24 fps)
# A plain cut passes when the jump at the cut frame is at most CUT_RATIO times the ordinary frame-to-frame change
# (CUT_LOCAL for the worst region) and the two drawings differ by at most CUT_SAME levels at that moment. Measured:
# a cut at 1.00 / 0.99 / 24 showed only a block popping in; one at 1.14 / 1.05 / 49 jumped in brightness and drawing.
CUT_RATIO = 1.15
CUT_LOCAL = 1.2
CUT_SAME = 30.0
SEAM_WIDTH = 192            # width of the frames seams are judged on
SEAM_REVIEW = 45.0          # drawings this far apart at a dissolved seam go to the review as an issue
COLOUR_REVIEW = 6.0         # a colour residual above this after matching goes to the review as an issue
SHORT_SIDE = 1080           # deliveries: 1080 on the short side (Seedance 2.5's largest render size)
FEATURE = (48, 84)          # width, height of the edge maps clips are aligned on (portrait; swapped for landscape)
WARP_BAND = 1.5             # seconds a result may run ahead of or behind the source
WARP_STEP = .08             # extra cost (share of the median frame difference) of holding or skipping a frame


def target_size(width: int, height: int) -> tuple[int, int]:
    """The delivery size: SHORT_SIDE on the short side, the source's aspect, even dimensions."""
    if width >= height:
        return round(SHORT_SIDE * width / height / 2) * 2, SHORT_SIDE
    return SHORT_SIDE, round(SHORT_SIDE * height / width / 2) * 2


def timeline(row: dict, out_fps: float) -> list[dict]:
    """Output frame ranges of every segment on one global clock (0 = the kept range's start), so rounding never
    drifts: `out` is what the segment rendered, `body` what it delivers."""
    fps = row["fps"]
    first = round(row["keep"][0] * fps)
    position = lambda frame: round((frame - first) * out_fps / fps)
    rows = []
    for seg in row["segments"]:
        a, b = seg["render_frames"]
        rows.append({"id": seg["id"], "seam": seg["seam"], "out": [position(a), position(b)],
                     "body": [position(seg["frames"][0]), position(seg["frames"][1])],
                     "camera": seg.get("seam_camera") or [0.0, 0.0]})
    for original in row.get("originals") or []:
        span = [position(original["frames"][0]), position(original["frames"][1])]
        rows.append({"id": original["id"], "seam": "cut", "out": span, "body": list(span), "camera": [0.0, 0.0],
                     "original": True})
    return sorted(rows, key=lambda line: line["body"][0])


def original_clip(source: Path, start: float, frames: int, out_fps: float, size: tuple[int, int], output: Path) -> Path:
    """`frames` frames of the source from `start` at the delivery size and rate (passthrough footage)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial.mp4")
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, start):.6f}", "-i", source, "-vf",
         f"fps={out_fps},scale={size[0]}:{size[1]}:flags=lanczos,setsar=1,tpad=stop_mode=clone:stop_duration=1",
         "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
         partial])
    partial.replace(output)
    return output


# --- 1. retime -------------------------------------------------------------------------------------------------

def edge_maps(path: Path, size: tuple[int, int], *, start: float | None = None, seconds: float | None = None,
              fps: float | None = None) -> np.ndarray:
    """Normalised edge maps (frames, h, w) of a clip at `size`: independent of the materials' colours, so a cartoon
    frame and its realistic version of the same moment match."""
    command = [FFMPEG, "-v", "error"]
    if start is not None:
        command += ["-ss", f"{max(0.0, start):.6f}"]
    command += ["-i", path]
    if seconds is not None:
        command += ["-t", f"{seconds:.6f}"]
    filters = ([f"fps={fps}"] if fps else []) + [f"scale={size[0]}:{size[1]}:flags=area", "format=gray"]
    raw = run(command + ["-vf", ",".join(filters), "-f", "rawvideo", "-"], binary=True)
    data = np.frombuffer(raw, np.uint8)
    count = data.size // (size[0] * size[1])
    grey = data[: count * size[0] * size[1]].reshape(count, size[1], size[0]).astype(np.float32)
    gy, gx = np.gradient(grey, axis=(1, 2))
    edges = np.hypot(gx, gy)
    edges /= edges.mean(axis=(1, 2), keepdims=True) + 1e-3
    return edges.reshape(count, -1)


def structure_maps(path: Path, size: tuple[int, int] = (16, 28), fps: float | None = None) -> np.ndarray:
    """Coarse grey layouts (frames, pixels), each normalised: where things are, not what they are made of."""
    filters = ([f"fps={fps}"] if fps else []) + [f"scale={size[0]}:{size[1]}:flags=area", "format=gray"]
    raw = run([FFMPEG, "-v", "error", "-i", path, "-vf", ",".join(filters), "-f", "rawvideo", "-"], binary=True)
    data = np.frombuffer(raw, np.uint8)
    count = data.size // (size[0] * size[1])
    grey = data[: count * size[0] * size[1]].reshape(count, -1).astype(np.float32)
    return (grey - grey.mean(axis=1, keepdims=True)) / (grey.std(axis=1, keepdims=True) + 1e-3)


FOLLOW_EDGES = .93          # matched / typical difference of edge maps above which a result may have left the source
FOLLOW_STRUCTURE = .80      # ... and of coarse layouts. Measured: results that followed 0.71-0.92 / 0.50-0.61; one
                            # that cut to a close-up of its own 0.976 / 0.89


def follows(clip: Path, ai: Path, out_fps: float = 24.0) -> dict:
    """Whether a result still shows the source clip's camera and layout (it may set its own pace): both the
    edge maps and the coarse layouts, matched after time warping, must stay clearly below random pairs."""
    info = video_info(clip)
    portrait = info["height"] >= info["width"]
    ratios = {}
    for name, read_maps in (("edges", lambda p, fps=None: edge_maps(p, FEATURE if portrait else FEATURE[::-1], fps=fps)),
                            ("structure", lambda p, fps=None: structure_maps(p, (16, 28) if portrait else (28, 16), fps))):
        src, res = read_maps(clip, out_fps), read_maps(ai)
        cost = np.stack([np.abs(res - s).mean(axis=1) for s in src])
        _, matched = warp(cost, max(4, round(WARP_BAND * out_fps)))
        ratios[name] = round(matched / max(float(np.median(cost)), 1e-6), 3)
    ratios["follows"] = not (ratios["edges"] > FOLLOW_EDGES and ratios["structure"] > FOLLOW_STRUCTURE)
    return ratios


def warp(cost: np.ndarray, band: int, jump: int = 18) -> tuple[list[int], float]:
    """Monotonic match of every output frame (rows) to one result frame (columns), by dynamic programming inside a
    band around the even pace: from one output frame to the next the result advances one frame for free, holds
    (0) or skips ahead (up to `jump` frames) at a small cost per frame of difference from one; the result may start a
    few frames late and end early (its last frame is then held)."""
    rows, cols = cost.shape
    # The cost of a hold or skip scales with how much a right match beats a typical one: under a strong restyle
    # the edges differ everywhere (best 0.8 vs typical 0.93), and a penalty scaled to the typical difference made the
    # warp trail and hold frames rather than follow a result that catches up.
    spread = float(np.median(cost)) - float(np.median(cost.min(axis=1)))
    step = WARP_STEP * max(spread, 1e-3)
    inf = np.inf
    ratio = cols / rows
    columns = np.arange(cols)
    penalty = np.array([step] + [step * (k - 1) for k in range(1, jump + 1)])     # advance 0, 1, 2, ... frames
    total = np.full((rows, cols), inf)
    came = np.zeros((rows, cols), np.int16)          # how many frames the result advanced into this cell
    inside = lambda i: np.abs(columns - i * ratio) <= band
    total[0] = np.where((columns <= max(2, band // 3)) & inside(0), cost[0], inf)
    for i in range(1, rows):
        best = np.full(cols, inf)
        moved = np.zeros(cols, np.int16)
        for k in range(jump + 1):
            shifted = np.full(cols, inf)
            shifted[k:] = total[i - 1, : cols - k] if k else total[i - 1]
            better = shifted + penalty[k] < best
            best[better], moved[better] = shifted[better] + penalty[k], k
        total[i] = np.where(inside(i), best + cost[i], inf)
        came[i] = moved
    j = int(np.argmin(total[rows - 1]))
    if not np.isfinite(total[rows - 1, j]):
        raise RuntimeError("the result cannot be matched to the source inside the time band")
    mapping = [0] * rows
    for i in range(rows - 1, -1, -1):
        mapping[i] = j
        j -= int(came[i, j]) if i else 0
    return mapping, float(np.mean([cost[i, m] for i, m in enumerate(mapping)]))


RETIME_WORKERS = 6          # segments retimed side by side


def write_frames(ai: Path, mapping: list[int], out_fps: float, size: tuple[int, int], output: Path) -> Path:
    """The clip whose frame k is result frame mapping[k] (monotonic: streamed, one frame in memory)."""
    width, height = size
    frame_bytes = width * height * 3
    filters = [f"scale={width}:{height}:flags=lanczos", "format=rgb24"]
    decoder = subprocess.Popen([str(FFMPEG), "-v", "error", "-i", str(ai), "-vf", ",".join(filters),
                                "-fps_mode", "passthrough", "-f", "rawvideo", "-"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=0x08000000)   # stopped early on purpose
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial.mp4")
    encoder = subprocess.Popen([str(FFMPEG), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-s", f"{width}x{height}", "-r", str(out_fps), "-i", "-", "-an", "-c:v", "libx264",
                                "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p", str(partial)],
                               stdin=subprocess.PIPE, creationflags=0x08000000)
    try:
        index, current = -1, None
        for wanted in mapping:
            while index < wanted:
                block = decoder.stdout.read(frame_bytes)
                if len(block) < frame_bytes:
                    break
                current, index = block, index + 1
            if current is None:
                raise RuntimeError(f"{ai.name}: no frames decoded")
            encoder.stdin.write(current)
    finally:
        encoder.stdin.close()
        decoder.stdout.close()
        decoder.wait()
    if encoder.wait():
        raise RuntimeError(f"encoding {output.name} failed")
    partial.replace(output)
    return output


CAMERA_WIDTH = 270          # width the camera's path is measured at (phase correlation, pixels)


def camera_path(path: Path, *, start: float | None = None, seconds: float | None = None,
                fps: float | None = None) -> np.ndarray:
    """The camera's position on screen per frame (pixels at CAMERA_WIDTH, cumulative phase-correlation shifts)."""
    from reskin.analyze import _shift, decode
    grey = decode(path, CAMERA_WIDTH, fps=fps, start=start, duration=seconds, gray=True).astype(np.float32)
    steps = [(0.0, 0.0)] + [_shift(grey[i - 1], grey[i]) for i in range(1, len(grey))]
    return np.cumsum(np.array(steps), axis=0)


PIN_SECONDS = .5            # half-width of the window around a pin whose matches give the result's moment there
PIN_SPEEDS = (.75, 1.33)    # the constant speed a clip may play at


def pin(mapping: list[int], anchors: list[int], out_fps: float) -> tuple[float, float]:
    """One constant speed per clip (result frame = offset + speed * output frame) through the result's moments at
    the two pins: where the clip meets its neighbours (the seams inside a shot) or its ends. The moment at a pin is
    the median offset of the frame-by-frame match (edge maps) around it, so the seams show the source's moment;
    in between the result keeps its own pace, without freezes, skips, slow motion or speed-ups (following the match
    frame by frame stuttered; a speed fitted on camera velocity misplaced seams: a still camera fits any timing)."""
    n = len(mapping)
    half = max(2, round(PIN_SECONDS * out_fps))
    def moment(at: int) -> float:
        lo, hi = max(0, at - half), min(n, at + half + 1)
        return at + float(np.median([mapping[i] - i for i in range(lo, hi)]))
    a, b = min(anchors), max(anchors)
    if b - a < out_fps:                                 # pins too close for a speed: keep the result's pace
        return moment(a) - a, 1.0
    speed = (moment(b) - moment(a)) / (b - a)
    speed = min(max(speed, PIN_SPEEDS[0]), PIN_SPEEDS[1])
    return moment(a) - speed * a, speed


def retime(ai: Path, source: Path, render_frames: list[int], fps: float, frames: int, out_fps: float,
           size: tuple[int, int], output: Path, anchors: list[int] | None = None) -> dict:
    """The AI clip on the source clock: `frames` output frames from the render's start, played at the constant speed
    `pin` finds (`anchors`: output frames where the clip must show the source's moment, default its ends)."""
    feature = FEATURE if size[1] >= size[0] else FEATURE[::-1]
    start = render_frames[0] / fps
    seconds = frames / out_fps + 1 / out_fps
    src = edge_maps(source, feature, start=start, seconds=seconds, fps=out_fps)[:frames]
    if len(src) < frames:
        src = np.concatenate([src, np.repeat(src[-1:], frames - len(src), axis=0)])
    res = edge_maps(ai, feature)
    cost = np.stack([np.abs(res - src[i]).mean(axis=1) for i in range(frames)])
    src_path = camera_path(source, start=start, seconds=seconds, fps=out_fps)[:frames]
    res_path = camera_path(ai)[: len(res)]
    mapping, _ = warp(cost, max(4, round(WARP_BAND * out_fps)))
    offset, speed = pin(mapping, anchors or [0, frames - 1], out_fps)
    line = offset + speed * np.arange(frames)
    motion = np.r_[0.0, np.abs(np.diff(res, axis=0)).mean(axis=1)]     # how much the result changes per frame
    picks = place(line, sorted(set([0, frames - 1] + list(anchors or []))), motion, len(res))
    write_frames(ai, picks, out_fps, size, output)
    steps = [b - a for a, b in zip(picks, picks[1:])]
    seen = res_path[np.array(picks)]
    return {"frames": frames, "result_frames": int(len(res)), "typical": round(float(np.median(cost)), 3),
            "speed": round(speed, 4), "offset": round(offset / out_fps, 3),
            "held": sum(1 for d in steps if d == 0), "skipped": sum(1 for d in steps if d > 1),
            "camera_error": round(float(np.median(np.hypot(*(seen - src_path[: len(seen)]).T))), 1)}


CORRECTION_GAP = 4          # output frames at least between two holds or skips


def place(line: np.ndarray, knots: list[int], motion: np.ndarray, cols: int) -> list[int]:
    """Whole result frames for every output frame: at each knot (the clip's ends and its seams) the frame the line
    gives; between knots the result plays frame by frame, and the holds or skips needed to meet the next knot go
    where the result changes least (the camera and the scene stand still: invisible there), CORRECTION_GAP apart
    where possible. No interpolated frames: ffmpeg's motion interpolation repeated one frame for seconds on these
    clips."""
    targets = [int(min(max(round(line[k]), 0), cols - 1)) for k in knots]
    for i in range(1, len(targets)):                       # monotonic, and never faster than 2 frames per frame
        targets[i] = min(max(targets[i], targets[i - 1]), targets[i - 1] + 2 * (knots[i] - knots[i - 1]))
    picks = [targets[0]]
    for (k0, t0), (k1, t1) in zip(zip(knots, targets), zip(knots[1:], targets[1:])):
        n = k1 - k0                                        # output frames after k0 up to k1
        frames = list(range(t0 + 1, t1 + 1))               # result frames after t0 up to t1
        extra = len(frames) - n                            # > 0: result frames to leave out, < 0: to show twice
        inner = frames[:-1] if len(frames) > 1 else frames # the knot's own frame stays
        order = sorted(inner, key=lambda f: (motion[min(f, len(motion) - 1)], f))
        chosen: list[int] = []
        for gap in (CORRECTION_GAP, 2, 1):
            for f in order:
                if len(chosen) >= abs(extra):
                    break
                if f not in chosen and all(abs(f - c) >= gap for c in chosen):
                    chosen.append(f)
            if len(chosen) >= abs(extra):
                break
        if extra > 0:
            shown = [f for f in frames if f not in set(chosen)]
            while len(shown) > n:                          # faster than 2 frames per frame: rare
                shown.pop(int(np.argmin([motion[min(f, len(motion) - 1)] for f in shown[:-1]])))
        else:
            twice = set(chosen)
            shown = []
            for f in frames:
                shown += [f, f] if f in twice else [f]
            while len(shown) < n:                          # slower than half speed: hold the knot frame
                shown.insert(0, shown[0] if shown else t0)
            shown = shown[len(shown) - n:]
        picks += shown
    return picks


# --- 2. colours ------------------------------------------------------------------------------------------------

def frames_rgb(path: Path, start: int, count: int, width: int = 96) -> np.ndarray:
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


def overlap(before: dict, after: dict) -> tuple[int, int]:
    """The global output frames both neighbours rendered."""
    return max(before["out"][0], after["out"][0]), min(before["out"][1], after["out"][1])


def runs_of(lines: list[dict]) -> list[list[int]]:
    """Indices of the clips joined by seams inside a shot (a cut or the start opens a new run)."""
    runs: list[list[int]] = []
    for index, line in enumerate(lines):
        if index and line["seam"] == "continuous":
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def match_colours(clips: list[Path], lines: list[dict]) -> tuple[list[tuple], list[dict]]:
    """Gain/offset per clip so the colours meet across every seam inside a shot (hard cuts may change the look)."""
    corrections: list[tuple] = [(np.ones(3), np.zeros(3)) for _ in clips]
    seams = []
    for members in runs_of(lines):
        if len(members) < 2:
            continue
        boundaries = []
        for position, index in enumerate(members[1:], 1):
            a, b = overlap(lines[index - 1], lines[index])
            count = max(2, b - a)
            mean_b, std_b = stats(frames_rgb(clips[index - 1], a - lines[index - 1]["out"][0], count))
            mean_a, std_a = stats(frames_rgb(clips[index], a - lines[index]["out"][0], count))
            boundaries.append({"index": position, "id": lines[index]["id"], "mean_before": mean_b, "std_before": std_b,
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


def corrected(pixels: np.ndarray, correction: tuple) -> np.ndarray:
    gain, offset = correction
    return np.clip(pixels * gain + offset, 0, 255)


# --- 3. seams --------------------------------------------------------------------------------------------------

def regional(a: np.ndarray, b: np.ndarray) -> float:
    """The largest mean difference of one region of the frame."""
    rows, cols = GRID
    h, w = a.shape[0] // rows, a.shape[1] // cols
    diff = np.abs(a - b).mean(axis=2)
    return float(max(diff[r * h:(r + 1) * h, c * w:(c + 1) * w].mean() for r in range(rows) for c in range(cols)))


def cut_options(before: np.ndarray, after: np.ndarray) -> list[dict]:
    """Every frame i (1..n-1) of an overlap where the switch could go (the earlier clip up to i-1, the later from i):
    the jump there against the ordinary change from frame to frame in both clips, overall and in the worst region,
    and how different both drawings are at that moment."""
    options = []
    for i in range(1, min(len(before), len(after))):
        jump = float(np.abs(before[i - 1] - after[i]).mean())
        normal = (float(np.abs(before[i - 1] - before[i]).mean()) + float(np.abs(after[i - 1] - after[i]).mean())) / 2
        local = regional(before[i - 1], after[i])
        local_normal = (regional(before[i - 1], before[i]) + regional(after[i - 1], after[i])) / 2
        options.append({"frame": i, "ratio": round(jump / max(normal, .5), 3),
                        "local_ratio": round(local / max(local_normal, .5), 3),
                        "same": round(float(np.abs(before[i] - after[i]).mean()), 2)})
    return options


def choose_cut(options: list[dict], planned: int) -> dict:
    """The least visible switch frame (nearest the planned seam on a tie) and whether a plain cut passes there."""
    best = min(options, key=lambda o: (round(max(o["ratio"], o["local_ratio"]), 2), abs(o["frame"] - planned)))
    plain = best["ratio"] <= CUT_RATIO and best["local_ratio"] <= CUT_LOCAL and best["same"] <= CUT_SAME
    return {**best, "mode": "cut" if plain else "dissolve"}


def dissolve_frame(a: np.ndarray, b: np.ndarray, u: float) -> np.ndarray:
    """One frame of the light dissolve at progress u (0-1): a smoothstep blend, nothing else."""
    alpha = u * u * (3 - 2 * u)
    return (1 - alpha) * a + alpha * b


def read_full(path: Path, start: int, count: int, size: tuple[int, int]) -> np.ndarray:
    raw = run([FFMPEG, "-v", "error", "-i", path, "-vf", f"select='between(n\\,{start}\\,{start + count - 1})',"
               f"scale={size[0]}:{size[1]},format=rgb24", "-fps_mode", "passthrough", "-f", "rawvideo", "-"], binary=True)
    data = np.frombuffer(raw, np.uint8)
    n = data.size // (size[0] * size[1] * 3)
    return data[: n * size[0] * size[1] * 3].reshape(n, size[1], size[0], 3)


def write_transition(frames: list[np.ndarray], size: tuple[int, int], out_fps: float, output: Path) -> Path:
    partial = output.with_name(output.stem + ".partial.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(np.clip(f, 0, 255).astype(np.uint8).tobytes() for f in frames)
    run([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
         "-r", str(out_fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
         partial], input=data)
    partial.replace(output)
    return output


def make_seam(job: Path, source_id: str, clips: list[Path], lines: list[dict], index: int,
              corrections: list[tuple], size: tuple[int, int], out_fps: float, seams: str = "auto") -> dict:
    """The switch between clips index-1 and index inside their overlap: at the least visible frame, a plain cut when
    it does not show (or always with `seams` "cut"), else a light dissolve centred there (always with "dissolve")."""
    before, after = lines[index - 1], lines[index]
    a, b = overlap(before, after)
    first = corrected(frames_rgb(clips[index - 1], a - before["out"][0], b - a, SEAM_WIDTH), corrections[index - 1])
    second = corrected(frames_rgb(clips[index], a - after["out"][0], b - a, SEAM_WIDTH), corrections[index])
    choice = choose_cut(cut_options(first, second), after["body"][0] - a)
    mode = choice["mode"] if seams == "auto" else seams
    judged = {k: choice[k] for k in ("ratio", "local_ratio", "same")}
    if mode == "cut":
        return {"start": a + choice["frame"], "length": 0, "clip": None, "mode": "cut", **judged}
    length = max(2, round(DISSOLVE_SECONDS * out_fps))
    start = max(a, min(a + choice["frame"] - length // 2, b - length))
    length = min(length, b - start)
    left = read_full(clips[index - 1], start - before["out"][0], length, size)
    right = read_full(clips[index], start - after["out"][0], length, size)
    count = min(len(left), len(right), length)
    frames = [dissolve_frame(corrected(left[i].astype(np.float32), corrections[index - 1]),
                             corrected(right[i].astype(np.float32), corrections[index]), (i + .5) / count)
              for i in range(count)]
    clip = write_transition(frames, size, out_fps, job / "work" / f"{source_id}-{after['id']}-seam.mp4")
    return {"start": start, "length": count, "clip": clip, "mode": "dissolve", **judged}


# --- 4. join ---------------------------------------------------------------------------------------------------

def assemble_source(job: Path, state: dict, source_id: str, row: dict, style: dict, seams: str = "auto",
                    upto: str | None = None) -> dict:
    """Join the source's segments into its delivery. `upto` (a segment id) joins only the segments up to that one,
    without audio, into `work/preview/` (the segment check judges the new seam on it); every clip is retimed against
    the whole plan either way, so a preview and the delivery share their retimed clips."""
    source = next(r for r in state["sources"] if r["id"] == source_id)
    info = video_info(Path(source["path"]))
    out_fps = min(float(state["options"]["fps"]), round(row["fps"], 3))
    size = target_size(row["width"], row["height"])
    full = timeline(row, out_fps)
    count = len(full) if upto is None else next(i for i, line in enumerate(full) if line["id"] == upto) + 1
    lines = full[:count]
    segments = {seg["id"]: seg for seg in row["segments"]}

    def one(index: int) -> tuple[Path, dict]:
        line = lines[index]
        if line.get("original"):
            frames = line["out"][1] - line["out"][0]
            work = job / "work" / f"{source_id}-{line['id']}.mp4"
            start = row["keep"][0] + line["out"][0] / out_fps
            stamp = {"original": round(start, 4), "frames": frames, "fps": out_fps, "size": list(size)}
            stamp_path = work.with_suffix(".json")
            if not (work.is_file() and stamp_path.is_file() and read(stamp_path).get("stamp") == stamp):
                log(f"{source_id}:{line['id']}: original footage (passthrough)")
                original_clip(Path(source["path"]), start, frames, out_fps, size, work)
                write(stamp_path, {"stamp": stamp, "timing": {"frames": frames, "original": True}})
            return work, read(stamp_path)["timing"]
        seg = segments[line["id"]]
        ai = job / "ai" / f"{source_id}-{line['id']}.mp4"
        record_path = job / "state/render" / f"{source_id}-{line['id']}.json"
        record = read(record_path) if record_path.is_file() else {}
        if not ai.is_file() or record.get("status") != "done":
            raise ValueError(f"{source_id}:{line['id']} is not rendered yet; run render first")
        frames = line["out"][1] - line["out"][0]
        work = job / "work" / f"{source_id}-{line['id']}.mp4"
        stamp = {"ai": record.get("generation_id"), "frames": frames, "fps": out_fps, "size": list(size), "retime": 10}
        stamp_path = work.with_suffix(".json")
        if work.is_file() and stamp_path.is_file() and read(stamp_path).get("stamp") == stamp:
            return work, read(stamp_path)["timing"]
        log(f"{source_id}:{line['id']}: retiming")
        # Where the clip's timing must meet the source: its start and every seam inside a shot (the switch lies there).
        joined = lambda near: 0 < near < len(full) and full[near]["seam"] == "continuous"
        anchors = [line["body"][0] - line["out"][0] if joined(index) else 0,
                   line["body"][1] - line["out"][0] if joined(index + 1) else frames - 1]
        measured = retime(ai, Path(source["path"]), seg["render_frames"], row["fps"], frames, out_fps, size, work,
                          anchors)
        write(stamp_path, {"stamp": stamp, "timing": measured})
        return work, measured

    # Each retime is mostly ffmpeg decoding and encoding: several side by side.
    with ThreadPoolExecutor(max_workers=RETIME_WORKERS) as pool:
        done = list(pool.map(one, range(len(lines))))
    clips = [work for work, _ in done]
    timing = [{"segment": line["id"], **measured} for line, (_, measured) in zip(lines, done)]
    corrections, colour = match_colours(clips, lines)
    switches = {index: make_seam(job, source_id, clips, lines, index, corrections, size, out_fps, seams)
                for index, line in enumerate(lines) if index and line["seam"] == "continuous"}
    # Pieces on the global clock: each clip from its body start (or the end of the switch before it) to its body
    # end (or the start of the switch after it), the switch clips in between.
    pieces = []
    for index, line in enumerate(lines):
        head = switches[index]["start"] + switches[index]["length"] if index in switches else line["body"][0]
        tail = switches[index + 1]["start"] if index + 1 in switches else line["body"][1]
        if tail > head:
            pieces.append({"path": clips[index], "a": head - line["out"][0], "b": tail - line["out"][0],
                           "correction": corrections[index]})
        if index + 1 in switches and switches[index + 1]["length"]:
            s = switches[index + 1]
            pieces.append({"path": s["clip"], "a": 0, "b": s["length"], "correction": None})
    inputs, graph = [], []
    for number, piece in enumerate(pieces):
        inputs += ["-i", piece["path"]]
        chain = f"[{number}:v]trim=start_frame={piece['a']}:end_frame={piece['b']},setpts=PTS-STARTPTS"
        if piece["correction"] is not None and not (np.allclose(piece["correction"][0], 1)
                                                    and np.allclose(piece["correction"][1], 0)):
            chain += f",format=rgb24,{lut(*piece['correction'])},format=yuv420p"
        graph.append(chain + f",setsar=1[p{number}]")
    total = lines[-1]["body"][1] - lines[0]["body"][0]
    graph.append("".join(f"[p{n}]" for n in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=0[joined]")
    graph.append(f"[joined]trim=end_frame={total},setpts=PTS-STARTPTS[vout]")
    keep_start, keep_end = row["keep"]
    audio = []
    if info["audio"] and upto is None:
        index = len(pieces)
        inputs += ["-i", source["path"]]
        fade = []
        if keep_end < info["duration"] - .1:
            fade.append(f"afade=t=out:st={max(0.0, keep_end - keep_start - .35):.3f}:d=0.35")
        if keep_start > .05:
            fade.append("afade=t=in:st=0:d=0.15")
        graph.append(f"[{index}:a]atrim=start={keep_start:.6f}:end={keep_end:.6f},asetpts=PTS-STARTPTS"
                     + "".join("," + f for f in fade) + "[aout]")
        audio = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    if upto is None:
        delivery = Path(state["delivery_dir"])
        names = [r["name"] for r in state["sources"]]
        name = source["name"] + ("" if names.count(source["name"]) == 1 else f"_{source_id}")
        output = delivery / f"{name}_{style.get('label', 'reskin')}.mp4"
    else:
        # one file per result of the last segment: an earlier preview may still be open in a player
        record_path = job / "state/render" / f"{source_id}-{upto}.json"
        generation = (read(record_path).get("generation_id") or "") if record_path.is_file() else ""
        output = job / "work/preview" / f"{source_id}-upto-{upto}-{generation[:8] or 'current'}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
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
    for entry in timing:
        # the camera on screen far from the source's at the ends of a clip: the seams there may not line up
        if entry.get("camera_error", 0) > 25:
            problems.append(f"{entry['segment']}: the result's camera runs far from the source's (median "
                            f"{entry['camera_error']} px at {CAMERA_WIDTH} wide)")
    origin = lines[0]["body"][0]
    seam_times = []
    for index, line in enumerate(lines[1:], 1):
        entry = {"segment": line["id"], "seam": line["seam"], "time": round((line["body"][0] - origin) / out_fps, 3)}
        if index in switches:
            s = switches[index]
            entry = {**entry, "time": round((s["start"] + s["length"] / 2 - origin) / out_fps, 3),
                     "frames": s["length"], **{k: s[k] for k in ("mode", "ratio", "local_ratio", "same")}}
            if s["mode"] == "dissolve" and s["same"] > SEAM_REVIEW:
                problems.append(f"seam before {line['id']} at {entry['time']:.2f} s: the two results differ a lot "
                                f"(drawings {s['same']} apart, jump {s['ratio']} of the frame change): joined with a "
                                f"light dissolve, check the seam sheet")
        seam_times.append(entry)
    result = {"source": source_id, "output": str(output), "duration": round(made["duration"], 3),
              "size": [made["width"], made["height"]], "fps": out_fps, "seams": seam_times, "colour": colour,
              "timing": timing, "problems": problems, "keep": row["keep"],
              "segments": [{"id": line["id"], "start": round((line["body"][0] - origin) / out_fps, 3),
                            "end": round((line["body"][1] - origin) / out_fps, 3),
                            **({"original": True} if line.get("original") else {})} for line in lines]}
    if upto is None:
        write(job / "state/assembled" / f"{source_id}.json", result)
    log(f"{source_id}: {'assembled' if upto is None else 'preview up to ' + upto}: {output.name} "
        f"({made['width']}x{made['height']}, {made['duration']:.2f} s)")
    return result
