"""Lumos login storage outside the skill folder, encrypted for the Windows user."""
from __future__ import annotations

import base64
import os
import time

from reskin.common import home, read, write

class MissingCredential(RuntimeError):
    """A missing or unreadable login."""


def path():
    return home() / "credentials.json"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData/CryptUnprotectData for the current Windows user (other users and machines cannot read it)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not function(ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(result)):
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the stored credential")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _seal(text: str) -> str:
    raw = text.encode("utf-8")
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
    return "plain:" + base64.b64encode(raw).decode()


def _open(value: str) -> str:
    kind, _, payload = value.partition(":")
    raw = base64.b64decode(payload)
    return (_dpapi(raw, False) if kind == "dpapi" else raw).decode("utf-8")


def _load() -> dict:
    try:
        return read(path())
    except (OSError, ValueError):
        return {}


def _store(key: str, secret: str | None, **extra):
    data = _load()
    if secret is None:
        data.pop(key, None)
    else:
        data[key] = {"value": _seal(secret), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
    write(path(), data)


def _get(key: str) -> tuple[str | None, dict]:
    row = _load().get(key)
    if not row:
        return None, {}
    try:
        return _open(row["value"]), row
    except (OSError, ValueError, KeyError):
        return None, row


def lumos_refresh_token() -> str:
    value, _ = _get("lumos_refresh_token")
    if not value:
        raise MissingCredential("not logged in to Lumos yet: run login (the user scans a Feishu QR code)")
    return value


def save_lumos_login(token: str, user: dict):
    _store("lumos_refresh_token", token, user={k: user.get(k) for k in ("user_id", "name")})


def forget_lumos_login():
    _store("lumos_refresh_token", None)
