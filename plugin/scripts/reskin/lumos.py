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


def call(method: str, path: str, value=None, *, form: tuple[bytes, str] | None = None, timeout: float = 60):
    """A Legil server call; returns `data` of a `{"code": 0, "data": ...}` reply."""
    url = path if path.startswith("http") else SERVER + path
    for attempt in range(2):
        headers = {"Authorization": "LumosBearer " + access_token(force=attempt > 0)}
        try:
            if form is not None:
                body, content_type = form
                _, payload, _ = net.request(method, url, body=body, headers={**headers, "Content-Type": content_type,
                                                                              "Accept": "application/json"}, timeout=timeout)
                reply = net.decode_json(payload)
            else:
                reply = net.json_call(method, url, value, headers=headers, timeout=timeout)
        except net.HttpError as error:
            if error.status == 401 and attempt == 0:
                continue
            raise
        if isinstance(reply, dict) and reply.get("code") not in (0, None):
            raise RuntimeError(f"Lumos {path}: {reply.get('message') or reply.get('msg') or reply}")
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


def submit_video_edit(video: dict, images: list[dict], prompt: str, *, resolution: str = "1080p",
                      model: str = VIDEO_MODEL) -> dict:
    body = {"reference_videos": [video], "reference_images": images, "generation_mode": "video_edit",
            "prompt": prompt, "generate_audio": False, "resolution": resolution, "aspect_ratio": "adaptive",
            "output_format": "mp4", "sample_count": 1, "model": model}
    return call("POST", "/generations/video-to-video", body, timeout=120)


def submit_image_to_image(images: list[dict], prompt: str, *, model: str, aspect_ratio: str,
                          resolution: str = "2K", quality: str = "high") -> dict:
    body = {"reference_images": images, "prompt": prompt, "aspect_ratio": aspect_ratio, "resolution": resolution,
            "quality": quality, "output_format": "png", "background": "auto", "output_compression": 100,
            "sample_count": 1, "model": model}
    return call("POST", "/generations/image-to-image", body, timeout=120)


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
