"""Winky (Lumos model gateway) with the user's API key: Gemini generateContent for the review."""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from reskin import credentials, net

BASE = "https://lumos.diandian.info/winky"
REVIEW_MODEL = "gemini-3.8-flash"
MEDIA_RESOLUTION = "MEDIA_RESOLUTION_HIGH"     # video frames were 66 tokens each by default: too coarse to judge


class PermissionDenied(RuntimeError):
    """The key is valid but may not use this model."""


class InvalidKey(credentials.MissingCredential):
    pass


def _error(error: net.HttpError) -> Exception:
    try:
        value = net.decode_json(error.body.encode("utf-8"))
    except ValueError:
        value = {}
    detail = value.get("error") if isinstance(value, dict) else None
    kind = detail.get("type") if isinstance(detail, dict) else None
    text = (detail or {}).get("message") if isinstance(detail, dict) else (value.get("msg") if isinstance(value, dict) else None)
    if kind == "winky_permission_error":
        return PermissionDenied(text or "not authorized for this model")
    if isinstance(value, dict) and value.get("code") == -1 and "api_key" in str(value.get("msg")):
        return InvalidKey("the Winky API key was refused: ask the user for a valid key, then run auth --key <key>")
    return RuntimeError(f"Winky error {error.status}: {text or error.body[:500]}")


def _post(url: str, *, body: bytes, content_type: str, timeout: float) -> dict:
    headers = {"Authorization": "Bearer " + credentials.winky_key(), "Content-Type": content_type,
               "Accept": "application/json"}
    try:
        _, payload, _ = net.request("POST", url, body=body, headers=headers, timeout=timeout, attempts=3)
    except net.HttpError as error:
        raise _error(error) from None
    value = net.decode_json(payload)
    if isinstance(value, dict) and (value.get("type") == "error" or (value.get("code") not in (None, 0) and "msg" in value)):
        raise _error(net.HttpError(200, json.dumps(value), url))
    return value


def check_key() -> dict:
    """Free check that the key is accepted."""
    headers = {"Authorization": "Bearer " + credentials.winky_key()}
    try:
        net.json_call("GET", BASE + "/openai/v1/models", headers=headers, attempts=2)
    except net.HttpError as error:
        raise _error(error) from None
    return {"key": "accepted"}


def gemini(parts: list[dict], *, schema: dict | None = None, model: str = REVIEW_MODEL, system: str | None = None,
           temperature: float = 0.2, timeout: float = 900, media_resolution: str | None = MEDIA_RESOLUTION) -> dict | str:
    """generateContent; with a schema the JSON answer is parsed. `media_resolution` (default HIGH) sets how finely
    Gemini reads images and video frames."""
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature}}
    if media_resolution:
        body["generationConfig"]["mediaResolution"] = media_resolution
    if schema:
        body["generationConfig"].update(responseMimeType="application/json", responseSchema=schema)
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    value = _post(f"{BASE}/gemini/v1/models/{model}:generateContent", body=json.dumps(body).encode("utf-8"),
                  content_type="application/json", timeout=timeout)
    candidates = value.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no answer: {str(value)[:500]}")
    text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
                   if not part.get("thought"))
    if schema:
        try:
            return json.loads(text)
        except ValueError:
            raise RuntimeError(f"Gemini answer is not valid JSON: {text[:500]}") from None
    return text


def inline(path: Path, mime: str | None = None) -> dict:
    data = Path(path).read_bytes()
    return {"inlineData": {"mimeType": mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                           "data": base64.b64encode(data).decode()}}
