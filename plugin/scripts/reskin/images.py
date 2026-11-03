"""Style keyframes with GPT Image through the Lumos image-to-image API (the user's Lumos login, no Winky key)."""
from __future__ import annotations

from pathlib import Path

from reskin import lumos, net

IMAGE_MODEL = "gpt-image-2.5-sunburst"
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}


def aspect_for(width: int, height: int) -> str:
    ratio = width / height
    return min(ASPECTS, key=lambda name: abs(ASPECTS[name] - ratio))


def edit(images: list[Path], prompt: str, output: Path, *, width: int, height: int, quality: str = "high",
         model: str = IMAGE_MODEL) -> dict:
    """Edit the first image (later images are references); writes `output` and returns the generation."""
    references = [lumos.upload(path, "image-to-image") for path in images]
    started = lumos.submit_image_to_image(references, prompt, model=model, aspect_ratio=aspect_for(width, height),
                                          quality=quality)
    finished = lumos.wait(started["generation_id"], timeout=1200, poll=5)
    net.download(lumos.result_urls(finished)[0], Path(output))
    return {"backend": "lumos", "model": model, "generation_id": started["generation_id"]}
