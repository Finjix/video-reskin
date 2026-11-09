"""Feishu QR login to Lumos in a hidden Edge/Chrome window; keeps only the resulting refresh token.

The QR image is written to the settings folder and opened on screen; when Feishu lets it expire a fresh one replaces
it (and opens again) until the user scans or the time runs out.
"""
from __future__ import annotations

import base64
import io
import os
import time

from reskin import credentials, lumos, runtime
from reskin.common import home, log

LOGIN_URL = lumos.BASE + "/login?redirect_uri=" + lumos.BASE + "/"
QR_SCRIPT = """() => {
  const canvas = document.querySelector('canvas');
  if (canvas && canvas.width > 50) return canvas.toDataURL('image/png');
  const img = [...document.querySelectorAll('img')].find(i => i.naturalWidth >= 100);
  return img ? img.src : null;
}"""


class LoginTimeout(credentials.MissingCredential):
    pass


def _cookie(context) -> str | None:
    for row in context.cookies([lumos.BASE]):
        if row["name"] == "lumos_refresh_token" and row["value"]:
            return row["value"]
    return None


def _save_qr(data_url: str, number: int) -> str:
    from PIL import Image
    raw = base64.b64decode(data_url.split(",", 1)[1]) if data_url.startswith("data:") else None
    if raw is None:
        raise RuntimeError("the QR code is not an inline image")
    with Image.open(io.BytesIO(raw)) as image:
        rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, "white")
    white.alpha_composite(rgba)
    border = Image.new("RGB", (rgba.width + 80, rgba.height + 80), "white")
    border.paste(white.convert("RGB"), (40, 40))
    big = border.resize((border.width * 2, border.height * 2), Image.NEAREST)
    folder = home()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("lumos-login-qr-*.png"):
        old.unlink(missing_ok=True)
    path = folder / f"lumos-login-qr-{number}.png"
    big.save(path)
    return str(path)


def _launch(playwright, profile):
    errors = []
    for channel in ("msedge", "chrome"):
        try:
            return playwright.chromium.launch_persistent_context(str(profile), channel=channel, headless=True,
                                                                 viewport={"width": 1280, "height": 900})
        except Exception as error:      # a missing browser channel raises a generic playwright Error
            errors.append(f"{channel}: {str(error).splitlines()[0]}")
    raise runtime.RuntimeFailure("login needs Microsoft Edge or Google Chrome: " + "; ".join(errors))


def run(timeout: float = 300, open_image: bool = True) -> dict:
    from playwright.sync_api import sync_playwright
    profile = home() / "browser-profile"
    with sync_playwright() as playwright:
        context = _launch(playwright, profile)
        try:
            token = _cookie(context)
            if token:
                try:
                    lumos.refresh(token)
                    return _finish(token)
                except credentials.MissingCredential:
                    context.clear_cookies()
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            deadline = time.monotonic() + timeout
            shown, number, candidate, steady = None, 0, None, 0
            while time.monotonic() < deadline:
                token = _cookie(context)
                if token:
                    try:
                        lumos.refresh(token)
                        return _finish(token)
                    except credentials.MissingCredential:
                        pass
                frame = next((f for f in page.frames if "passport.feishu" in f.url), None)
                if frame is not None:
                    try:
                        expired = frame.locator("[class*=refresh]")
                        if expired.count() and expired.first.is_visible():
                            expired.first.click(timeout=3000)
                            time.sleep(1.5)
                        data = frame.evaluate(QR_SCRIPT)
                    except Exception:       # the frame reloads while the code refreshes
                        data = None
                    # The page draws a first code and replaces it within a second (2026-11-09: the user got a dead
                    # code): pass a code on only once it has stayed the same for two polls.
                    steady = steady + 1 if data and data == candidate else 0
                    candidate = data
                    if data and data != shown and steady >= 2:
                        number += 1
                        path = _save_qr(data, number)
                        shown = data
                        print(f"qr: {path} (scan it with the Feishu app; it refreshes on its own)", flush=True)
                        if open_image and hasattr(os, "startfile"):
                            os.startfile(path)
                time.sleep(1)
            raise LoginTimeout(f"nobody scanned the QR code within {timeout:.0f} s; run login again when the user is ready")
        finally:
            context.close()
            for old in home().glob("lumos-login-qr-*.png"):
                old.unlink(missing_ok=True)


def _finish(token: str) -> dict:
    lumos._TOKEN.update(value=lumos.refresh(token), expires=time.time() + 20 * 60)
    credentials.save_lumos_login(token, {})
    try:
        user = lumos.user()
    except Exception as error:       # the login works; the name is only for the summary
        log(f"could not read the Lumos user name: {error}")
        user = {}
    credentials.save_lumos_login(token, user)
    return {"user_id": user.get("user_id"), "name": user.get("name")}
