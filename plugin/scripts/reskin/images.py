"""Style keyframes with GPT Image through the Lumos image-to-image API (the user's Lumos login, no Winky key)."""
from __future__ import annotations

from pathlib import Path

from reskin import ledger, lumos, net
from reskin.common import log as _log

IMAGE_MODEL = "gpt-image-2.5-sunburst"
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2,
           "4:5": 4 / 5, "5:4": 5 / 4}


ASPECT_TOLERANCE = .01      # relative; a source further than this from every preset gets "adaptive"


def aspect_for(width: int, height: int, aspects: dict[str, float] = ASPECTS) -> str:
    """The preset matching the source ratio, or "adaptive" when none is within ASPECT_TOLERANCE."""
    ratio = width / height
    name = min(aspects, key=lambda name: abs(aspects[name] - ratio))
    return name if abs(aspects[name] / ratio - 1) <= ASPECT_TOLERANCE else "adaptive"


QUALITY = "high"            # GPT Image quality ("max" doubled the price for keyframes a 720p video cannot show)


def edit(images: list[Path], prompt: str, output: Path, *, width: int, height: int, quality: str = QUALITY,
         model: str = IMAGE_MODEL, job: Path | None = None, key: str = "", purpose: str = "keyframe") -> dict:
    """Edit the first image (later images are references); writes `output` and returns the generation. With `job`
    the call goes into the spend ledger."""
    for attempt in range(2):
        # Uploads go again too: one failure seen was Lumos timing out fetching an uploaded picture.
        references = [lumos.upload(path, "image-to-image") for path in images]
        started = lumos.submit_image_to_image(references, prompt, model=model, aspect_ratio=aspect_for(width, height),
                                              quality=quality, log=lambda text: _log(f"{key}: {text}"))
        if job is not None:
            ledger.add(job, kind="image", event="submit", purpose=purpose, key=key, model=model,
                       generation_id=started["generation_id"], cause="new" if attempt == 0 else "auto-retry")
        try:
            finished = lumos.wait(started["generation_id"], timeout=600, poll=5)
        except lumos.GenerationFailed as error:
            if attempt == 0:
                _log(f"{key}: image generation failed ({str(error)[:120]}); trying once more")
                continue
            raise
        net.download(lumos.result_urls(finished)[0], Path(output))
        return {"backend": "lumos", "model": model, "generation_id": started["generation_id"]}
    raise AssertionError("unreachable")
