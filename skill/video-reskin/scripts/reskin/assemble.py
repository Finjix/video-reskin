"""Put the restyled segments back on the source's clock and hide the seams.

1. Retime: MiniMax H3 follows the source's motion at an uneven pace (2026-11-09, GOF3D10232: a 6 s clip came back
   6.58 s long, on time for 2.5 s, then half a second slow), so a plain stretch drifts. Every output frame is
   matched to the result frame showing the same moment (dynamic time warping on edge maps), and the clip is
   rewritten frame by frame at the output rate and size.
2. Colours: gain/offset per clip so colours meet across every seam inside a shot (least squares per run).
3. Seams: at a camera cut the clips are simply joined. Inside a shot (where the camera moves fast, plan) both
   results cover an overlap; the switch goes to the frame there where the jump from the one to the other is
   smallest. When that jump is no larger than the picture's ordinary change from frame to frame there, and the two
   drawings are close, it is a plain cut; otherwise a very light dissolve over DISSOLVE_SECONDS, nothing more (the
   user's rule, 2026-11-09: no effects at seams unless needed, then only a slight dissolve).
4. Join with the source's own audio of the kept range.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np

from reskin.common import FFMPEG, log, read, run, video_info, write

GAIN_LIMITS = (.8, 1.25)
OFFSET_LIMIT = 40.0
GRID = (10, 6)              # rows, columns of the regions a seam is measured in
SEAM_EXCESS = 6.0           # switch difference minus motion, 0-255
SEAM_RATIO = 2.5            # switch difference / motion
SEAM_LOCAL = 30.0           # largest region difference, 0-255 (and SEAM_RATIO times that region's own motion)
DISSOLVE_SECONDS = .25      # the light dissolve when a plain cut would show (6 frames at 24 fps)
# A plain cut passes when the jump at the cut frame is at most CUT_RATIO times the ordinary frame-to-frame change
# (CUT_LOCAL for the worst region) and the two drawings differ by at most CUT_SAME levels at that moment.
# GOF3D10232 (2026-11-09): a cut at 1.00 / 0.99 / 24 showed only a block popping in; one at 1.14 / 1.05 / 49 jumped
# in brightness and drawing.
CUT_RATIO = 1.15
CUT_LOCAL = 1.2
CUT_SAME = 30.0
SEAM_WIDTH = 192            # width of the frames seams are judged on
SEAM_REVIEW = 45.0          # drawings this far apart at a dissolved seam go to the review as an issue
FEATURE = (48, 84)          # width, height of the edge maps clips are aligned on (portrait; swapped for landscape)
WARP_BAND = 1.5             # seconds a result may run ahead of or behind the source
WARP_STEP = .08             # extra cost (share of the median frame difference) of holding or skipping a frame


def target_size(width: int, height: int, resolution: str) -> tuple[int, int]:
    short = 1080 if resolution == "1080p" else 720
    if width >= height:
        return round(short * width / height / 2) * 2, short
    return short, round(short * height / width / 2) * 2


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
    return rows


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
FOLLOW_STRUCTURE = .80      # ... and of coarse layouts. GOF3D10232 (2026-11-09): results that followed 0.71-0.92 /
                            # 0.50-0.61; one that cut to a close-up of its own 0.976 / 0.89


def follows(clip: Path, ai: Path, out_fps: float = 24.0) -> dict:
    """Whether an H3 result still shows the source clip's camera and layout (it may set its own pace): both the
    edge maps and the coarse layouts, matched after time warping, must stay clearly below random pairs."""
    portrait = video_info(clip)["height"] >= video_info(clip)["width"]
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
    (0) or skips ahead (up to `jump` frames: H3 stalls for half a second at times) at a small cost per frame of
    difference from one; the result may start a few frames late and end early (its last frame is then held)."""
    rows, cols = cost.shape
    # The cost of a hold or skip scales with how much a right match beats a typical one: under a strong restyle
    # the edges differ everywhere (best 0.8 vs typical 0.93), and a penalty scaled to the typical difference made the
    # warp trail 1.25 s and hold 32 frames rather than follow H3's catch-up at the end (GOF3D10232 K01, 2026-11-09).
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


def smooth(mapping: list[int], spread: int = 6) -> list[int]:
    """A running median over up to 5 frames (no single-frame back-and-forth from noisy matches), then a running mean
    over up to 2 * `spread` + 1 frames, so the holds and skips a result's own pace needs spread out evenly instead of
    freezing or jumping in one place; both narrower at the ends, so the ends stay put, and kept monotonic."""
    n = len(mapping)
    median = []
    for i in range(n):
        k = min(2, i, n - 1 - i)
        window = sorted(mapping[i - k: i + k + 1])
        median.append(window[len(window) // 2])
    out, last = [], 0
    for i in range(n):
        k = min(spread, i, n - 1 - i)
        last = max(last, int(round(sum(median[i - k: i + k + 1]) / (2 * k + 1))))
        out.append(last)
    return out


def write_frames(ai: Path, mapping: list[int], out_fps: float, size: tuple[int, int], output: Path) -> Path:
    """The clip whose frame k is result frame mapping[k] (monotonic: streamed, one frame in memory)."""
    width, height = size
    frame_bytes = width * height * 3
    decoder = subprocess.Popen([str(FFMPEG), "-v", "error", "-i", str(ai), "-vf",
                                f"scale={width}:{height}:flags=lanczos,format=rgb24", "-fps_mode", "passthrough",
                                "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=0x08000000)     # stopped early on purpose: no broken-pipe noise
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


def retime(ai: Path, source: Path, render_frames: list[int], fps: float, frames: int, out_fps: float,
           size: tuple[int, int], output: Path) -> dict:
    """The AI clip on the source clock: `frames` output frames from the render's start, each the result frame
    showing that moment of the source."""
    feature = FEATURE if size[1] >= size[0] else FEATURE[::-1]
    start = render_frames[0] / fps
    src = edge_maps(source, feature, start=start, seconds=frames / out_fps + 1 / out_fps, fps=out_fps)[:frames]
    if len(src) < frames:
        src = np.concatenate([src, np.repeat(src[-1:], frames - len(src), axis=0)])
    res = edge_maps(ai, feature)
    cost = np.stack([np.abs(res - src[i]).mean(axis=1) for i in range(frames)])
    mapping, matched = warp(cost, max(4, round(WARP_BAND * out_fps)))
    mapping = smooth(mapping)
    write_frames(ai, mapping, out_fps, size, output)
    even = [round(i * (len(res) - 1) / max(1, frames - 1)) for i in range(frames)]
    return {"frames": frames, "result_frames": int(len(res)), "matched": round(matched, 3),
            "typical": round(float(np.median(cost)), 3),
            "even": round(float(np.mean([cost[i, j] for i, j in enumerate(even)])), 3),
            "held": sum(1 for a, b in zip(mapping, mapping[1:]) if a == b),
            "skipped": sum(max(0, b - a - 1) for a, b in zip(mapping, mapping[1:]))}


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


def measure(before: np.ndarray, after: np.ndarray) -> dict:
    """How different the two results are across the switch (colours corrected) against how much the picture moves
    from frame to frame there: `visible` when they differ clearly more than it moves, or one region a lot."""
    count = min(len(before), len(after))
    difference = float(np.mean([np.abs(before[i] - after[i]).mean() for i in range(count)]))
    steps = [float(np.abs(clip[i + 1] - clip[i]).mean()) for clip in (before, after) for i in range(count - 1)]
    motion = float(np.median(steps)) if steps else 0.0
    middle = count // 2
    local = regional(before[middle], after[middle])
    local_steps = [regional(clip[i], clip[i + 1]) for clip in (before, after) for i in range(count - 1)]
    local_motion = float(np.median(local_steps)) if local_steps else 0.0
    visible = ((difference - motion > SEAM_EXCESS and difference > SEAM_RATIO * max(motion, .5))
               or (local > SEAM_LOCAL and local > SEAM_RATIO * max(local_motion, .5)))
    return {"difference": round(difference, 2), "motion": round(motion, 2), "local": round(local, 2),
            "local_motion": round(local_motion, 2), "verdict": "visible" if visible else "clean"}


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


SEAM_MODES = ("auto", "cut", "dissolve")   # auto: a plain cut where it does not show, else a light dissolve


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
    window = slice(max(0, choice["frame"] - 3), choice["frame"] + 3)
    found = measure(first[window], second[window])
    found["verdict"] = "clean" if choice["mode"] == "cut" else "visible"      # as a plain cut
    judged = {k: choice[k] for k in ("ratio", "local_ratio", "same")}
    if mode == "cut":
        return {"start": a + choice["frame"], "length": 0, "clip": None, "mode": "cut", **judged, **found}
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
    return {"start": start, "length": count, "clip": clip, "mode": "dissolve", **judged, **found}


# --- 4. join ---------------------------------------------------------------------------------------------------

def assemble_source(job: Path, state: dict, source_id: str, row: dict, style: dict, seams: str = "auto") -> dict:
    source = next(r for r in state["sources"] if r["id"] == source_id)
    info = video_info(Path(source["path"]))
    out_fps = min(float(state["options"]["fps"]), round(row["fps"], 3))
    size = target_size(row["width"], row["height"], state["options"]["resolution"])
    lines = timeline(row, out_fps)
    clips, timing = [], []
    for line, seg in zip(lines, row["segments"]):
        ai = job / "ai" / f"{source_id}-{line['id']}.mp4"
        record_path = job / "state/render" / f"{source_id}-{line['id']}.json"
        record = read(record_path) if record_path.is_file() else {}
        if not ai.is_file() or record.get("status") != "done":
            raise ValueError(f"{source_id}:{line['id']} is not rendered yet; run render first")
        frames = line["out"][1] - line["out"][0]
        work = job / "work" / f"{source_id}-{line['id']}.mp4"
        stamp = {"ai": record.get("generation_id"), "frames": frames, "fps": out_fps, "size": list(size), "retime": 3}
        stamp_path = work.with_suffix(".json")
        if work.is_file() and stamp_path.is_file() and read(stamp_path).get("stamp") == stamp:
            measured = read(stamp_path)["timing"]
        else:
            measured = retime(ai, Path(source["path"]), seg["render_frames"], row["fps"], frames, out_fps, size, work)
            write(stamp_path, {"stamp": stamp, "timing": measured})
        timing.append({"segment": line["id"], **measured})
        clips.append(work)
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
    if info["audio"]:
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
    for entry in timing:
        # a strong restyle changes the edges too: matches of 0.7-0.92 of the typical difference lined up on the stills
        # (GOF3D10232, 2026-11-09); near 1 the result no longer follows the source
        if entry["matched"] > .95 * entry["typical"]:
            problems.append(f"{entry['segment']}: the result does not follow the source's motion (match "
                            f"{entry['matched']} vs typical {entry['typical']})")
    origin = lines[0]["body"][0]
    seam_times = []
    for index, line in enumerate(lines[1:], 1):
        entry = {"segment": line["id"], "seam": line["seam"], "time": round((line["body"][0] - origin) / out_fps, 3)}
        if index in switches:
            s = switches[index]
            entry = {**entry, "time": round((s["start"] + s["length"] / 2 - origin) / out_fps, 3),
                     "frames": s["length"], **{k: s[k] for k in ("mode", "ratio", "local_ratio", "same", "difference",
                                                                 "motion", "local", "local_motion", "verdict")}}
            if s["mode"] == "dissolve" and s["same"] > SEAM_REVIEW:
                problems.append(f"seam before {line['id']} at {entry['time']:.2f} s: the two results differ a lot "
                                f"(drawings {s['same']} apart, jump {s['ratio']} of the frame change): joined with a "
                                f"light dissolve, check the seam sheet")
        seam_times.append(entry)
    result = {"source": source_id, "output": str(output), "duration": round(made["duration"], 3),
              "size": [made["width"], made["height"]], "fps": out_fps, "seams": seam_times, "colour": colour,
              "timing": timing, "problems": problems, "keep": row["keep"],
              "segments": [{"id": line["id"], "start": round((line["body"][0] - origin) / out_fps, 3),
                            "end": round((line["body"][1] - origin) / out_fps, 3)} for line in lines]}
    write(job / "state/assembled" / f"{source_id}.json", result)
    log(f"{source_id}: assembled {output.name} ({made['width']}x{made['height']}, {made['duration']:.2f} s)")
    return result
