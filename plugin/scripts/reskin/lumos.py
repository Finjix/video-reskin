"""Lumos / Legil web API (the same calls the Legil pages make): login refresh, uploads, generations and results.

Authentication: the Feishu QR login leaves a long-lived `lumos_refresh_token` cookie; every process trades it for a
short-lived access token (`LumosBearer`, about 30 minutes) and trades again when a call is refused.
"""
from __future__ import annotations

import mimetypes
import threading
import time
from pathlib import Path

from reskin import credentials, net

BASE = "https://lumos.diandian.info"
SERVER = BASE + "/legil/server"
VIDEO_MODEL = "doubao-seedance-2-5-260628"
DONE = {"Succeeded", "Failed", "PartiallySucceeded", "Cancelled"}
_TOKEN = {"value": None, "expires": 0.0}
_LOCK = threading.Lock()


class LoginRequired(credentials.MissingCredential):
    pass


class Refused(RuntimeError):
    """Lumos answered with an error code (busy queue, rate limit, bad parameters): nothing was created."""


class GenerationFailed(RuntimeError):
    def __init__(self, generation: dict):
        message = generation.get("error_message") or generation.get("raw_error_message") or generation.get("status")
        super().__init__(f"generation {generation.get('generation_id')} {generation.get('status')}: {message}")
        self.generation = generation


def refresh(token: str | None = None) -> str:
    """Trade the stored refresh token for an access token."""
    token = token or credentials.lumos_refresh_token()
    try:
        value = net.json_call("POST", BASE + "/marauders-map/lumos-auth/refresh",
                              headers={"Cookie": f"lumos_refresh_token={token}"}, attempts=3)
    except net.HttpError as error:
        if error.status in (400, 401, 403):
            raise LoginRequired("the Lumos login expired or was revoked: run login again (Feishu QR code)") from None
        raise
    access = (value.get("data") or {}).get("lumos_access_token") if isinstance(value, dict) else None
    if not access:
        raise LoginRequired(f"Lumos refused the stored login ({value}); run login again")
    return access


def access_token(force: bool = False) -> str:
    with _LOCK:
        if force or not _TOKEN["value"] or time.time() > _TOKEN["expires"]:
            _TOKEN["value"] = refresh()
            _TOKEN["expires"] = time.time() + 20 * 60
        return _TOKEN["value"]


def call(method: str, path: str, value=None, *, form: tuple[bytes, str] | None = None, timeout: float = 60,
         attempts: int = 4):
    """A Legil server call; returns `data` of a `{"code": 0, "data": ...}` reply. `attempts` > 1 retries network errors
    and 5xx replies, so only idempotent calls may use it (a lost reply to a submission may still have created one)."""
    url = path if path.startswith("http") else SERVER + path
    for attempt in range(2):
        headers = {"Authorization": "LumosBearer " + access_token(force=attempt > 0)}
        try:
            if form is not None:
                body, content_type = form
                _, payload, _ = net.request(method, url, body=body, headers={**headers, "Content-Type": content_type,
                                                                              "Accept": "application/json"},
                                            timeout=timeout, attempts=attempts)
                reply = net.decode_json(payload)
            else:
                reply = net.json_call(method, url, value, headers=headers, timeout=timeout, attempts=attempts)
        except net.HttpError as error:
            if error.status == 401 and attempt == 0:
                continue
            raise
        if isinstance(reply, dict) and reply.get("code") not in (0, None):
            raise Refused(f"Lumos {path}: {reply.get('message') or reply.get('msg') or reply}")
        return reply.get("data") if isinstance(reply, dict) else reply
    raise LoginRequired("Lumos keeps refusing the login: run login again")


def user() -> dict:
    url = BASE + "/marauders-map/user"
    reply = net.json_call("GET", url, headers={"Authorization": "LumosBearer " + access_token()})
    return reply.get("data") or {}


def upload(path: Path, generation_type: str) -> dict:
    """Upload a local file the way the pages do: signed OSS PUT, then the upload callback."""
    path = Path(path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    ticket = call("POST", "/input-media-url", {"file_name": path.name, "generation_type": generation_type})
    data = path.read_bytes()
    net.request("PUT", ticket["media_url"], body=data,
                headers={"Content-Type": ticket.get("content_type") or content_type}, timeout=600, attempts=3)
    call("POST", "/upload-callback", form=net.multipart([("file_id", ticket["media_id"])]))
    signed = call("POST", "/output-media-url", {"media_id": ticket["media_id"]})
    return {"media_path": ticket["media_path"], "media_id": ticket["media_id"], "media_url": signed["media_url"],
            "reference_type": "BASE"}


SUBMIT_PATIENCE = 20 * 60      # how long to keep retrying a refused submission (busy queue, rate limits)
LOOKUPS = (10, 20, 40)         # seconds to wait before each history lookup after a reply was lost


def recent(generation_type: str, pages: int = 2, page_size: int = 20) -> list[dict]:
    """The user's newest generations of one type, as the Legil history page lists them (newest first)."""
    output = "IMAGE" if generation_type.endswith("image") else "VIDEO"
    rows = []
    for page in range(1, pages + 1):
        data = call("POST", "/generations", {"page": page, "page_size": page_size, "generation_type": generation_type,
                                             "only_succeeded": False, "only_collected": False, "output_type": output})
        batch = (data or {}).get("data") if isinstance(data, dict) else data
        rows += batch or []
        if not batch or len(batch) < page_size:
            break
    return rows


def find_generation(media_id: str, generation_type: str) -> dict | None:
    """The generation that uses an uploaded file (every submission uploads its own, so the media id is unique)."""
    for row in recent(generation_type):
        config = row.get("generation_config") or {}
        refs = [*(config.get("reference_videos") or []), *(config.get("reference_images") or [])]
        if any(ref.get("media_id") == media_id for ref in refs):
            return row
    return None


def submit(path: str, body: dict, *, media_id: str, generation_type: str, patience: float = SUBMIT_PATIENCE,
           log=None) -> dict:
    """POST a generation exactly once. A refusal (error code, 429) created nothing and is retried for `patience`
    seconds; other 4xx are final. When the reply is lost (network error, 5xx), the history is searched for a generation
    using `media_id` before anything is sent again, so a lost reply never pays for a second generation."""
    started = time.monotonic()
    while True:
        try:
            return call("POST", path, body, timeout=120, attempts=1)
        except Refused as error:
            unclear, reason = False, error
        except net.HttpError as error:
            if error.status != 429 and error.status < 500:
                raise
            unclear, reason = error.status >= 500, error
        except net.NetworkError as error:
            unclear, reason = True, error
        if unclear:
            for wait_seconds in LOOKUPS:
                time.sleep(wait_seconds)
                try:
                    found = find_generation(media_id, generation_type)
                except credentials.MissingCredential:
                    raise
                except (RuntimeError, net.HttpError):
                    continue
                if found:
                    if log:
                        log(f"the reply was lost but Lumos created {found['generation_id']}; using it")
                    return {"generation_id": found["generation_id"], "recovered": True}
        if time.monotonic() - started > patience:
            raise reason
        if log:
            log(f"submission refused or lost ({reason}); retrying in 60 s")
        time.sleep(60)


def submit_video_edit(video: dict, images: list[dict], prompt: str, *, resolution: str = "1080p",
                      model: str = VIDEO_MODEL, log=None) -> dict:
    # In video_edit mode Seedance takes the output ratio and length from the input clip, so the ratio stays adaptive.
    body = {"reference_videos": [video], "reference_images": images, "generation_mode": "video_edit",
            "prompt": prompt, "generate_audio": False, "resolution": resolution, "aspect_ratio": "adaptive",
            "output_format": "mp4", "sample_count": 1, "model": model}
    return submit("/generations/video-to-video", body, media_id=video["media_id"], generation_type="video-to-video",
                  log=log)


def submit_image_to_image(images: list[dict], prompt: str, *, model: str, aspect_ratio: str,
                          resolution: str = "2K", quality: str = "max", log=None) -> dict:
    body = {"reference_images": images, "prompt": prompt, "aspect_ratio": aspect_ratio, "resolution": resolution,
            "quality": quality, "output_format": "png", "background": "opaque", "output_compression": 100,
            "sample_count": 1, "model": model}
    return submit("/generations/image-to-image", body, media_id=images[0]["media_id"],
                  generation_type="image-to-image", patience=5 * 60, log=log)


def generation(generation_id: str) -> dict:
    return call("GET", f"/generations/{generation_id}")


def cancel(generation_id: str) -> bool:
    try:
        call("POST", f"/generations/{generation_id}:cancel")
        return True
    except (RuntimeError, net.HttpError):
        return False


def wait(generation_id: str, *, timeout: float = 3600, poll: float = 10, on_poll=None) -> dict:
    """Poll until the generation finishes; raises GenerationFailed unless it succeeded."""
    deadline = time.monotonic() + timeout
    while True:
        value = generation(generation_id)
        if on_poll:
            on_poll(value)
        if value.get("status") in DONE:
            if value["status"] != "Succeeded" or not result_urls(value):
                raise GenerationFailed(value)
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"generation {generation_id} still {value.get('status')} after {timeout / 60:.0f} min")
        time.sleep(poll)


def result_urls(value: dict) -> list[str]:
    results = ((value.get("generations") or {}).get("media_result")) or []
    return [row["media_url"] for row in results if row.get("media_url")]
