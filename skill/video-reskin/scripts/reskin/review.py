"""Timestamped frames for inspecting the source and normalized video results."""
from pathlib import Path
from tempfile import TemporaryDirectory

from reskin.common import FFMPEG, FPS, run

SAMPLE_FPS = 6
SAMPLE_STEP = FPS // SAMPLE_FPS


def extract_samples(video, start, end, size, folder):
    width, height = size
    folder.mkdir()
    filters = (f"trim=start_frame={start}:end_frame={end},select=not(mod(n\\,{SAMPLE_STEP})),"
               f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=decrease:force_divisible_by=2,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    run([FFMPEG, "-v", "error", "-i", video, "-vf", filters, "-an", "-fps_mode", "passthrough",
         "-q:v", "2", "-threads", "1", folder / "%06d.jpg"])
    return sorted(folder.glob("*.jpg"))


def sheets(source, start, end, size, folder, result=None):
    """Page through the 24 fps timeline four frames at a time, starting on the requested frame."""
    from PIL import Image, ImageDraw, ImageFont

    samples = range(start, end, SAMPLE_STEP)
    per_page = 12 if result else 24
    pages = [folder / f"page-{i:03d}.jpg" for i in range(1, (len(samples) + per_page - 1) // per_page + 1)]
    if pages and not all(page.is_file() for page in pages):
        portrait = size[0] <= size[1]
        bounds = (240, 416) if portrait else (416, 240)
        scale = min(1, bounds[0] / size[0], bounds[1] / size[1])
        width, height = (max(2, int(n * scale) // 2 * 2) for n in size)
        columns = 6 if portrait else 4
        gap, label_height = 8, 22
        panels = 2 if result else 1
        font = ImageFont.load_default(size=14)
        folder.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=folder) as temporary:
            work = Path(temporary)
            pictures = [extract_samples(source, start, end, (width, height), work / "original")]
            if result:
                pictures.append(extract_samples(result, 0, end - start, (width, height), work / "result"))
            if any(len(side) != len(samples) for side in pictures):
                raise ValueError("review media does not cover the full frame range")
            for offset, page in enumerate(pages):
                first = offset * per_page
                count = min(per_page, len(samples) - first)
                rows = (count * panels + columns - 1) // columns
                canvas = Image.new("RGB", (gap + columns * (width + gap), gap + rows * (height + label_height + gap)),
                                   "#15191e")
                draw = ImageDraw.Draw(canvas)
                for index in range(count):
                    frame = samples[first + index] - (start if result else 0)
                    for side, paths in enumerate(pictures):
                        row, column = divmod(index * panels + side, columns)
                        x = gap + column * (width + gap)
                        y = gap + row * (height + label_height + gap)
                        label = ("  Result" if side else "  Source") if result else ""
                        draw.text((x, y), f"{frame / FPS:.3f} s{label}", font=font, fill="white")
                        with Image.open(paths[first + index]) as image:
                            canvas.paste(image, (x, y + label_height))
                target = work / page.name
                canvas.save(target, quality=94, subsampling=0)
                target.replace(page)
    return {"sample_fps": SAMPLE_FPS, "sample_count": len(samples), "sheets": [str(p) for p in pages]}
