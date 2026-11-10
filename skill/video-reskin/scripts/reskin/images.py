"""Reference pictures with GPT Image through the Lumos image-to-image API (the user's Lumos login, no Winky key)."""
from __future__ import annotations

from pathlib import Path

from reskin import lumos, net

IMAGE_MODEL = "gpt-image-2.6-sunburst"     # on Lumos
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}
# The user's settings: the scene map at 4K and quality high, three-view sheets at 1K and quality medium.
SCENE = {"resolution": "4K", "quality": "high"}
SUBJECT = {"resolution": "1K", "quality": "medium"}


def aspect_for(width: float, height: float) -> str:
    """The nearest aspect preset (4K needs one: Lumos offers no 4K for "adaptive")."""
    ratio = width / height
    return min(ASPECTS, key=lambda name: abs(ASPECTS[name] / ratio - 1))


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
