"""Winky (Lumos model gateway) with the user's API key: OpenAI image edits and Gemini generateContent."""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from reskin import credentials, net

BASE = "https://lumos.diandian.info/winky"
IMAGE_MODEL = "gpt-image-2.5-sunburst"
REVIEW_MODEL = "gemini-3.8-flash"


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
    """Free checks: the key is accepted, and whether it may use the image model (an invalid size is never billed)."""
    headers = {"Authorization": "Bearer " + credentials.winky_key()}
    try:
        net.json_call("GET", BASE + "/openai/v1/models", headers=headers, attempts=2)
    except net.HttpError as error:
        raise _error(error) from None
    try:
        _post(BASE + "/openai/v1/images/generations", body=json.dumps({"model": IMAGE_MODEL, "prompt": "x", "size": "7x7"}).encode(),
              content_type="application/json", timeout=60)
        image = "allowed"
    except PermissionDenied:
        image = "not authorized (image keyframes fall back to the Lumos image-to-image page API)"
    except RuntimeError:
        image = "allowed"
    return {"key": "accepted", "image_model": image}


def edit_image(images: list[Path], prompt: str, *, size: str, quality: str = "high", model: str = IMAGE_MODEL,
               timeout: float = 600) -> bytes:
    """/v1/images/edits with one or more input images; returns PNG bytes."""
    fields: list = [("model", model), ("prompt", prompt), ("size", size), ("quality", quality),
                    ("output_format", "png"), ("n", "1")]
    for path in images:
        path = Path(path)
        fields.append(("image[]", (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png")))
    body, content_type = net.multipart(fields)
    value = _post(BASE + "/openai/v1/images/edits", body=body, content_type=content_type, timeout=timeout)
    rows = value.get("data") or []
    if not rows or not rows[0].get("b64_json"):
        raise RuntimeError(f"image edit returned no image: {str(value)[:400]}")
    return base64.b64decode(rows[0]["b64_json"])


def gemini(parts: list[dict], *, schema: dict | None = None, model: str = REVIEW_MODEL, system: str | None = None,
           temperature: float = 0.2, timeout: float = 600) -> dict | str:
    """generateContent; with a schema the JSON answer is parsed."""
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature}}
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
