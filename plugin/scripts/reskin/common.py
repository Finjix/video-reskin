"""Suite paths and the small file, hash, process and logging helpers shared by every module."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from reskin import runtime
FFMPEG = runtime.Executable("ffmpeg")
FFPROBE = runtime.Executable("ffprobe")
PROMPTS = ROOT / "assets/prompts.json"
_DIGESTS: dict[tuple, str] = {}


def desktop() -> Path:
    """The user's Desktop folder, honouring OneDrive/folder redirection."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    except (AttributeError, OSError):
        pass
    return Path.home() / "Desktop"


def home() -> Path:
    """Per-user settings (credentials, login browser profile); survives plugin updates."""
    configured = os.environ.get("VIDEO_RESKIN_HOME")
    if configured:
        return Path(configured).resolve()
    base = os.environ.get("APPDATA")
    return (Path(base) if base else Path.home() / ".config") / "video-reskin"


def read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path: Path, value) -> Path:
    """Atomic UTF-8 JSON write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def sha(path: Path) -> str:
    """SHA-256 of a file, cached per process while path, size and mtime are unchanged."""
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key not in _DIGESTS:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        _DIGESTS[key] = digest.hexdigest()
    return _DIGESTS[key]


def digest(value) -> str:
    """SHA-256 of canonical JSON."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def run(command: list, *, binary: bool = False, cwd: Path | None = None, input: bytes | None = None):
    """Run a command and return its stdout; raise with the stderr tail on failure."""
    result = subprocess.run([str(part) for part in command], capture_output=True, cwd=cwd, input=input,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    if result.returncode:
        detail = result.stderr[-3000:].decode("utf-8", errors="replace")
        raise RuntimeError(f"command failed ({result.returncode}): {detail.strip()}")
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def probe(path: Path) -> dict:
    return json.loads(run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))


def video_info(path: Path) -> dict:
    """Width, height, frame rate, duration and audio presence of a media file."""
    data = probe(path)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"no video stream: {path}")
    num, den = (int(x) for x in (video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1").split("/"))
    if not num or not den:
        num, den = (int(x) for x in video["r_frame_rate"].split("/"))
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(side["rotation"])
    width, height = int(video["width"]), int(video["height"])
    if rotation % 180:
        width, height = height, width
    duration = float(video.get("duration") or data["format"].get("duration") or 0)
    return {"width": width, "height": height, "fps": num / den, "fps_fraction": f"{num}/{den}",
            "duration": duration, "frames": int(video.get("nb_frames") or round(duration * num / den)),
            "audio": any(s.get("codec_type") == "audio" for s in data["streams"]),
            "codec": video.get("codec_name"), "size": int(data["format"].get("size") or 0)}


_STARTED = time.monotonic()


def log(message: str) -> None:
    """One progress line for the agent on stderr; results go to stdout."""
    print(f"[{time.monotonic() - _STARTED:6.1f}s] {message}", file=sys.stderr, flush=True)


def timestamp(seconds: float) -> str:
    minutes, rest = divmod(max(0.0, seconds), 60)
    return f"{int(minutes)}:{rest:05.2f}"


class Busy(ValueError):
    """Another process holds the job lock (a second `render` would submit the same segments again)."""


class job_lock:
    """Non-blocking per-job lock on a file in `state/`; the OS releases it when the process ends, even on a crash."""

    def __init__(self, job: Path, name: str):
        self.path = Path(job) / "state" / f"{name}.lock"
        self.name = name
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise Busy(f"another {self.name} of this job is still running; wait for it to finish (its finished "
                       "segments are kept), never start a second one") from None
        return self

    def __exit__(self, *exc):
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
        finally:
            self.stream.close()
