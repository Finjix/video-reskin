"""Small HTTP client on urllib: JSON calls, multipart forms, streamed downloads and retries for flaky networks."""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

USER_AGENT = "video-reskin/0.0.1"
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {body[:600]}")
        self.status, self.body, self.url = status, body, url


def multipart(fields: list[tuple[str, str | tuple[str, bytes, str]]]) -> tuple[bytes, str]:
    """Encode (name, value) pairs; a file value is (filename, bytes, content_type)."""
    boundary = "----videoreskin" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        if isinstance(value, tuple):
            filename, data, content_type = value
            chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                          f"Content-Type: {content_type}\r\n\r\n".encode())
            chunks.append(data)
        else:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def request(method: str, url: str, *, body: bytes | None = None, headers: dict | None = None,
            timeout: float = 60, attempts: int = 4) -> tuple[int, bytes, dict]:
    """One HTTP exchange with retries on network errors and retryable statuses; other statuses raise HttpError."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    delay = 2.0
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as error:
            payload = error.read()
            if error.code in RETRY_STATUS and attempt + 1 < attempts:
                time.sleep(delay); delay *= 2
                continue
            raise HttpError(error.code, payload.decode("utf-8", errors="replace"), url) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            if attempt + 1 < attempts:
                time.sleep(delay); delay *= 2
                continue
            raise RuntimeError(f"network error contacting {url.split('?')[0]}: {error}") from None
    raise AssertionError("unreachable")


def decode_json(payload: bytes):
    """JSON body; some gateways wrap an error object in a JSON string, which is unwrapped too."""
    value = json.loads(payload.decode("utf-8"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return value


def json_call(method: str, url: str, value=None, *, headers: dict | None = None, timeout: float = 60, attempts: int = 4):
    body = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
    merged = {"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
    _, payload, _ = request(method, url, body=body, headers=merged, timeout=timeout, attempts=attempts)
    return decode_json(payload)


def download(url: str, destination: Path, *, timeout: float = 120, attempts: int = 4) -> Path:
    """Stream a URL to a file; the file appears only when complete."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + f".{os.getpid()}.partial")
    delay = 2.0
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as response, partial.open("wb") as output:
                expected = int(response.headers.get("Content-Length") or 0)
                received = 0
                while block := response.read(1024 * 1024):
                    output.write(block)
                    received += len(block)
            if expected and received != expected:
                raise ConnectionError(f"incomplete download ({received}/{expected} bytes)")
            os.replace(partial, destination)
            return destination
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            partial.unlink(missing_ok=True)
            if isinstance(error, urllib.error.HTTPError) and error.code not in RETRY_STATUS:
                raise HttpError(error.code, error.read().decode("utf-8", errors="replace"), url) from None
            if attempt + 1 < attempts:
                time.sleep(delay); delay *= 2
                continue
            raise RuntimeError(f"download failed from {url.split('?')[0]}: {error}") from None
    raise AssertionError("unreachable")
