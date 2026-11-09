"""On-demand, verified runtime downloads. Importing this module never installs anything."""
from __future__ import annotations

import hashlib
import http.client
import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "assets/runtime.json"
FEATURES = {"media": {"numpy": "2.5.3", "Pillow": "12.3.0"},
            "login": {"playwright": "1.64.1"}}


class RuntimeFailure(RuntimeError):
    """Unavailable runtime; retry the original command after fixing the reported cause."""


def cache() -> Path:
    return Path(os.environ.get("VIDEO_RESKIN_CACHE_DIR") or ROOT / "runtime-cache").resolve()


def offline() -> bool:
    return os.environ.get("VIDEO_RESKIN_OFFLINE") == "1"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def note(message: str):
    print(f"runtime: {message}", file=sys.stderr, flush=True)


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid(path: Path, spec: dict, *, remember=True) -> bool:
    if not path.is_file() or path.stat().st_size != spec["size"]:
        return False
    receipt = path.with_name(path.name + ".verified.json")
    stat = path.stat()
    signature = {"sha256": spec["sha256"], "size": stat.st_size, "mtime": stat.st_mtime_ns}
    try:
        if json.loads(receipt.read_text(encoding="utf-8")) == signature:
            return True
    except (OSError, ValueError):
        pass
    if checksum(path) != spec["sha256"]:
        return False
    if remember:
        save(receipt, signature)
    return True


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + f".{os.getpid()}.partial")
    partial.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(partial, path)


@contextmanager
def locked(name: str):
    """OS locks are released on crashes, so abandoned setup cannot block future runs."""
    folder = cache() / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(name.encode()).hexdigest() + ".lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0"); stream.flush()
        deadline = time.monotonic() + 1800
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeFailure(f"Timed out waiting for {name}; retry the command")
                time.sleep(.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def fetch(path: Path, spec: dict) -> Path:
    """Resume a verified file from approved sources, without publishing partial downloads."""
    with locked(str(path)):
        if valid(path, spec):
            return path
        if offline():
            raise RuntimeFailure(f"Offline cache missing or damaged: {path}; retry online")
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        failures = []
        for url in spec["urls"]:
            for attempt in range(2):
                try:
                    offset = partial.stat().st_size if partial.exists() else 0
                    if offset >= spec["size"]:
                        if offset == spec["size"] and checksum(partial) == spec["sha256"]:
                            os.replace(partial, path)
                            valid(path, spec)
                            return path
                        partial.unlink(); offset = 0
                    headers = {"User-Agent": "video-reskin/0.0.1"}
                    if offset:
                        headers["Range"] = f"bytes={offset}-"
                    note(f"downloading {path.name} ({offset}/{spec['size']} bytes)")
                    request = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(request, timeout=45) as response:
                        resumed = response.status == 206 and response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
                        with partial.open("ab" if resumed else "wb") as output:
                            received = offset if resumed else 0
                            last_progress = time.monotonic()
                            while block := response.read(1024 * 1024):
                                received += len(block)
                                if received > spec["size"]:
                                    raise ValueError("download larger than declared size")
                                output.write(block)
                                if time.monotonic() - last_progress >= 10:
                                    note(f"{path.name}: {received}/{spec['size']} bytes")
                                    last_progress = time.monotonic()
                    if partial.stat().st_size != spec["size"] or checksum(partial) != spec["sha256"]:
                        partial.unlink(missing_ok=True)
                        raise ValueError("size or SHA-256 mismatch")
                    os.replace(partial, path)
                    valid(path, spec)
                    return path
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    failures.append(f"{url}: {exc}")
                    if attempt == 0:
                        time.sleep(.25)
        raise RuntimeFailure(f"Cannot download {path.name}. Retry the original command. " + "; ".join(failures))


def uv() -> Path:
    path = cache() / "bootstrap/uv-0.13.4/uv.exe"
    if not path.is_file():
        raise RuntimeFailure("Start with scripts/video_reskin.ps1 to prepare uv and Python")
    return path


def ensure(feature: str):
    if feature == "base":
        return
    if feature not in FEATURES:
        raise RuntimeFailure(f"Unknown runtime feature: {feature}")
    lock = ROOT / "requirements" / f"{feature}.txt"
    stamp = {"lock": checksum(lock), "python": sys.executable}
    receipt = cache() / "installed" / (hashlib.sha256(sys.executable.encode()).hexdigest() + f"-{feature}.json")

    def installed() -> bool:
        try:
            return (json.loads(receipt.read_text(encoding="utf-8")) == stamp
                    and all(importlib.metadata.version(package) == version for package, version in FEATURES[feature].items()))
        except (OSError, ValueError, importlib.metadata.PackageNotFoundError):
            return False
    if installed():
        return
    with locked("packages-" + sys.prefix):
        if installed():
            return
        if cache() not in Path(sys.executable).resolve().parents:
            raise RuntimeFailure("Use scripts/video_reskin.ps1; dependencies are only installed into the private cache")
        note(f"preparing {feature} dependencies")
        command = [str(uv()), "pip", "install", "--python", sys.executable, "--require-hashes", "-r", str(lock),
                   "--only-binary", ":all:"]
        if offline():
            command += ["--offline"]
        env = {**os.environ, "UV_CACHE_DIR": str(cache() / "uv-cache")}
        # uv shows no progress without a terminal: a heartbeat says the setup is still alive.
        done = threading.Event()

        def heartbeat(started=time.monotonic()):
            while not done.wait(60):
                note(f"still preparing {feature} dependencies ({(time.monotonic() - started) / 60:.0f} min)")
        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            result = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr, env=env)
        finally:
            done.set()
        if result.returncode:
            raise RuntimeFailure(f"Dependency setup failed ({feature}); retry the original command")
        importlib.invalidate_caches()
        save(receipt, stamp)


_BINARIES: dict[tuple[str, str], Path] = {}


def binary(name: str) -> Path:
    """Verified FFmpeg tool; checked once per process, not on every command."""
    known = _BINARIES.get((str(cache()), name))
    if known is not None and known.is_file():
        return known
    spec = manifest()["ffmpeg"]
    folder = cache() / "tools" / f"ffmpeg-{spec['version']}"
    target = folder / f"{name}.exe"
    with locked("ffmpeg"):
        files = spec["files"]
        if all(valid(folder / filename, item) for filename, item in files.items()):
            _BINARIES[(str(cache()), name)] = target
            return target
        archive = fetch(cache() / "downloads" / "ffmpeg.zip", spec["archive"])
        with zipfile.ZipFile(archive) as zipped:
            for filename, item in files.items():
                if valid(folder / filename, item):
                    continue
                member = next((v for v in zipped.infolist() if v.filename.endswith("/bin/" + filename)), None)
                if member is None:
                    raise RuntimeFailure(f"FFmpeg archive missing {filename}")
                folder.mkdir(parents=True, exist_ok=True)
                partial = folder / (filename + ".partial")
                with zipped.open(member) as source, partial.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                os.replace(partial, folder / filename)
                if not valid(folder / filename, item):
                    raise RuntimeFailure(f"FFmpeg checksum mismatch: {filename}")
        _BINARIES[(str(cache()), name)] = target
        return target


class Executable(os.PathLike):
    """Resolve only when a command is actually constructed, never during module import."""
    def __init__(self, name):
        self.name = name

    def __fspath__(self):
        return str(binary(self.name))

    def __str__(self):
        return self.__fspath__()


def status(feature="base") -> dict:
    """Inspect only: no network, no import of optional libraries, no cache writes."""
    value = manifest()
    packages = {}
    for package, expected in FEATURES.get(feature, {}).items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        packages[package] = {"version": actual, "expected": expected}
    missing = [name for name, row in packages.items() if row["version"] != row["expected"]]
    if feature == "media":
        folder = cache() / "tools" / f"ffmpeg-{value['ffmpeg']['version']}"
        missing += [name for name, spec in value["ffmpeg"]["files"].items()
                    if not valid(folder / name, spec, remember=False)]
    return {"feature": feature, "cache_dir": str(cache()), "python": sys.executable,
            "packages": packages, "missing": missing}
