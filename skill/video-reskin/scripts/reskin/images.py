"""Style keyframes with GPT Image through the Lumos image-to-image API (the user's Lumos login, no Winky key)."""
from __future__ import annotations

from pathlib import Path

from reskin import lumos, net

IMAGE_MODEL = "gpt-image-2.6-sunburst"     # Lumos, from 2026-11-06 (2.5 before)
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}


ASPECT_TOLERANCE = .01      # relative; a source further than this from every preset gets "adaptive"
QUALITY = "high"            # GPT Image quality ("max" doubles the price); resolution 2K (lumos.submit_image_to_image)


def aspect_for(width: int, height: int, aspects: dict[str, float] = ASPECTS) -> str:
    """The preset matching the source ratio, or "adaptive" when none is within ASPECT_TOLERANCE."""
    ratio = width / height
    name = min(aspects, key=lambda name: abs(aspects[name] - ratio))
    return name if abs(aspects[name] / ratio - 1) <= ASPECT_TOLERANCE else "adaptive"


def edit(images: list[Path], prompt: str, output: Path, *, width: int, height: int, quality: str = QUALITY,
         model: str = IMAGE_MODEL) -> dict:
    """Edit the first image (later images are references); writes `output` and returns the generation."""
    references = [lumos.upload(path, "image-to-image") for path in images]
    started = lumos.submit_image_to_image(references, prompt, model=model, aspect_ratio=aspect_for(width, height),
                                          quality=quality)
    finished = lumos.wait(started["generation_id"], timeout=1200, poll=5)
    net.download(lumos.result_urls(finished)[0], Path(output))
    return {"backend": "lumos", "model": model, "generation_id": started["generation_id"]}
