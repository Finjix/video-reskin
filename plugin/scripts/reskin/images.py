"""Style keyframes with GPT Image: Winky image edits first, the Lumos image-to-image page API when the key may not."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from reskin import lumos, net, winky
from reskin.common import log

ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}
_STATE = {"backend": None}
_LOCK = threading.Lock()


def size_for(width: int, height: int) -> str:
    """The OpenAI size for a frame: same aspect ratio, multiples of 16, about 2.4 megapixels (9:16 -> 1152x2048)."""
    ratio = width / height
    pixels = 2_359_296
    w = (pixels * ratio) ** .5
    h = w / ratio
    w, h = max(16, round(w / 16) * 16), max(16, round(h / 16) * 16)
    return f"{w}x{h}"


def aspect_for(width: int, height: int) -> str:
    ratio = width / height
    return min(ASPECTS, key=lambda name: abs(ASPECTS[name] - ratio))


def backend() -> str:
    """auto (default): Winky until it refuses the model, then Lumos for the rest of the process."""
    forced = os.environ.get("VIDEO_RESKIN_IMAGE_BACKEND", "auto")
    if forced in ("winky", "lumos"):
        return forced
    return _STATE["backend"] or "winky"


def edit(images: list[Path], prompt: str, output: Path, *, width: int, height: int, quality: str = "high",
         model: str = winky.IMAGE_MODEL) -> dict:
    """Edit the first image (later images are references); writes `output` (PNG) and says which backend made it."""
    output = Path(output)
    if backend() == "winky":
        try:
            data = winky.edit_image(images, prompt, size=size_for(width, height), quality=quality, model=model)
            output.parent.mkdir(parents=True, exist_ok=True)
            partial = output.with_name(output.name + ".partial")
            partial.write_bytes(data)
            os.replace(partial, output)
            return {"backend": "winky", "model": model}
        except winky.PermissionDenied as error:
            with _LOCK:
                if _STATE["backend"] != "lumos":
                    log(f"Winky key may not use {model} ({error}); using the Lumos image-to-image API instead")
                _STATE["backend"] = "lumos"
    references = [lumos.upload(path, "image-to-image") for path in images]
    started = lumos.submit_image_to_image(references, prompt, model=model, aspect_ratio=aspect_for(width, height),
                                          quality=quality)
    finished = lumos.wait(started["generation_id"], timeout=1200, poll=5)
    net.download(lumos.result_urls(finished)[0], output)
    return {"backend": "lumos", "model": model, "generation_id": started["generation_id"]}
