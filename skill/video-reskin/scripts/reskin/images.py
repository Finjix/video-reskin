"""Reference pictures with GPT Image through the Lumos image-to-image API (the user's Lumos login, no Winky key)."""
from __future__ import annotations

from pathlib import Path

from reskin import lumos, net

IMAGE_MODEL = "gpt-image-2.6-sunburst"     # Lumos, from 2026-11-06 (2.5 before)
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}
ASPECT_TOLERANCE = .01      # relative; a source further than this from every preset gets "adaptive"
# The user's settings (2026-11-09): the scene map at 4K and quality max, three-view sheets at 1K and quality high.
SCENE = {"resolution": "4K", "quality": "max"}
SUBJECT = {"resolution": "1K", "quality": "high"}


def aspect_for(width: float, height: float, aspects: dict[str, float] = ASPECTS, exact: bool = False) -> str:
    """The preset matching the ratio, or "adaptive" when none is within ASPECT_TOLERANCE; `exact` False always
    returns the nearest preset (4K needs one: Lumos hides the resolution for "adaptive")."""
    ratio = width / height
    name = min(aspects, key=lambda name: abs(aspects[name] / ratio - 1))
    if not exact:
        return name
    return name if abs(aspects[name] / ratio - 1) <= ASPECT_TOLERANCE else "adaptive"


def edit(images: list[Path], prompt: str, output: Path, *, aspect: str, resolution: str, quality: str,
         model: str = IMAGE_MODEL) -> dict:
    """Edit with the images as references (Image 1, 2, ... in order); writes `output` and returns the generation."""
    references = [lumos.upload(path, "image-to-image") for path in images]
    started = lumos.submit_image_to_image(references, prompt, model=model, aspect_ratio=aspect,
                                          resolution=resolution, quality=quality)
    finished = lumos.wait(started["generation_id"], timeout=1800, poll=5)
    net.download(lumos.result_urls(finished)[0], Path(output))
    return {"backend": "lumos", "model": model, "generation_id": started["generation_id"], "aspect": aspect,
            "resolution": resolution, "quality": quality}
