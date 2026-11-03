"""The Winky API key and the Lumos login, stored per Windows user (DPAPI-encrypted) outside the plugin folder."""
from __future__ import annotations

import base64
import os
import time

from reskin.common import home, read, write

ENV_KEY = "VIDEO_RESKIN_WINKY_KEY"


class MissingCredential(RuntimeError):
    """A key or login the user has to provide; the message says which command fixes it."""


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


def winky_key() -> str:
    configured = os.environ.get(ENV_KEY)
    if configured:
        return configured.strip()
    value, _ = _get("winky_key")
    if not value:
        raise MissingCredential("no Winky API key yet: ask the user for their key (Lumos -> Winky -> API Keys), "
                                "then run auth --key <key>")
    return value


def save_winky_key(key: str):
    _store("winky_key", key.strip())


def lumos_refresh_token() -> str:
    value, _ = _get("lumos_refresh_token")
    if not value:
        raise MissingCredential("not logged in to Lumos yet: run login (the user scans a Feishu QR code)")
    return value


def save_lumos_login(token: str, user: dict):
    _store("lumos_refresh_token", token, user={k: user.get(k) for k in ("user_id", "name")})


def forget_lumos_login():
    _store("lumos_refresh_token", None)


def summary() -> dict:
    data = _load()
    key = os.environ.get(ENV_KEY) or _get("winky_key")[0]
    login = data.get("lumos_refresh_token") or {}
    return {"file": str(path()),
            "winky_key": (key[:10] + "..." + key[-4:]) if key else None,
            "winky_key_source": "environment" if os.environ.get(ENV_KEY) else ("stored" if key else None),
            "lumos_user": login.get("user"), "lumos_login_saved_at": login.get("saved_at")}
