"""Source-native comparison sheets and synchronized motion clips for element replacement."""
from pathlib import Path
from tempfile import TemporaryDirectory

from reskin.common import FFMPEG, digest, run


def motion_clip(source, result, frames, size, last_hold, dest):
    """Show consecutive frames at their original presentation times, source on the left."""
    from PIL import Image, ImageDraw, ImageFont

    start, end = frames
    ratio = min(1, 512 / size[0], 640 / size[1])
    w, h = (max(2, round(dim * ratio / 2) * 2) for dim in size)
    bar = Image.new("RGB", (2 * w, 24), "#15191e")
    draw = ImageDraw.Draw(bar)
    for side, label in enumerate(("Source", "Result")):
        draw.text((side * w + 3, 2), label, font=ImageFont.load_default(size=13), fill="white")
    header = dest.with_suffix(".png")
    bar.save(header)
    panel = f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,scale={w}:{h}:flags=lanczos,setsar=1"
    graph = (f"[0:v:0]{panel},pad={2*w}:{h+24}:0:24[left];"
             f"[1:v:0]{panel}[right];[left][right]overlay={w}:24:ts_sync_mode=nearest[pair];"
             f"[pair][2:v]overlay,trim=end_frame={end-start}[out]")
    # A VFR source may hold the last selected picture until well after the next nominal frame.
    duration = f"setts=duration='if(eq(N,{end-start-1}),{max(1, round(last_hold * 1_000_000))},DURATION)'"
    run([FFMPEG, "-v", "error", "-filter_complex_threads", "2", "-i", source, "-i", result, "-i", header,
         "-filter_complex", graph, "-map", "[out]", "-an", "-fps_mode:v", "passthrough", "-enc_time_base:v",
         "1:1000000", "-video_track_timescale", "1000000", "-c:v", "libx264", "-bf", "0", "-bsf:v", duration,
         "-threads:v", "3", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", dest])


def element_sheets(source, result, frames, times, folder, *, full_size=False):
    """A sparse source-native survey or paired review identified by exact presentation frame numbers."""
    from PIL import Image, ImageDraw, ImageFont

    folder = folder / digest([frames, full_size])[:12]
    folder.mkdir(parents=True, exist_ok=True)
    paths = [source, result] if result else [source]
    # FFmpeg extracts in time order. The caller can put decisive review frames at the front of the sheet.
    image_index = {frame: index for index, frame in enumerate(sorted(set(frames)), 1)}
    selected = "+".join(f"eq(n\\,{frame})" for frame in image_index)
    count = 1 if full_size else 6
    pages = [folder / f"page-{index // count + 1:03}.jpg" for index in range(0, len(frames), count)]
    if not all(p.is_file() for p in pages):
        with TemporaryDirectory(dir=folder) as td:
            work = Path(td)
            for side, video in enumerate(paths):
                filters = f"select={selected}"
                if not full_size:
                    filters += ",scale=320:360:force_original_aspect_ratio=decrease:force_divisible_by=2"
                run([FFMPEG, "-v", "error", "-i", video, "-map", "0:v:0", "-vf", filters, "-an",
                     "-fps_mode", "passthrough", "-q:v", "2", "-threads", "1", work / f"{side}-%06d.jpg"])
            with Image.open(work / "0-000001.jpg") as image:
                width, height = image.size
            columns = 1 if full_size else 2
            gap, label = 8, 23
            tile_width = (width + gap) * len(paths)
            font = ImageFont.load_default(size=15)
            for first, page in zip(range(0, len(frames), count), pages):
                number = min(count, len(frames) - first)
                rows = (number + columns - 1) // columns
                canvas = Image.new("RGB", (gap + min(columns, number) * tile_width,
                                           gap + rows * (label + height + gap)), "#15191e")
                draw = ImageDraw.Draw(canvas)
                for slot in range(number):
                    frame = frames[first + slot]
                    row, col = divmod(slot, columns)
                    y = gap + row * (label + height + gap)
                    for side in range(len(paths)):
                        x = gap + col * tile_width + side * (width + gap)
                        heading = "Result" if side else "Source"
                        draw.text((x, y), f"{heading}  #{frame}  {times[frame]:.3f}s", font=font, fill="white")
                        with Image.open(work / f"{side}-{image_index[frame]:06}.jpg") as image:
                            canvas.paste(image, (x, y + label))
                temp = work / page.name
                canvas.save(temp, quality=94, subsampling=0)
                temp.replace(page)
    return {"frames": frames, "sheets": [str(p) for p in pages]}
