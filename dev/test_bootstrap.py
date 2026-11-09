"""Cold start with no Python on PATH, Unicode paths, JSON stdin and offline reuse."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def main():
    folder = Path(tempfile.mkdtemp(prefix="vr-bootstrap-")) / "中文 路径"
    folder.mkdir()
    plugin = folder / "video-reskin"
    shutil.copytree(ROOT / "skill/video-reskin", plugin, ignore=shutil.ignore_patterns("runtime-cache", "__pycache__", "dependencies", "*.npz"))
    before = snapshot(plugin)
    environment = {**os.environ, "VIDEO_RESKIN_CACHE_DIR": str(folder / "cache"), "VIDEO_RESKIN_HOME": str(folder / "home"),
                   "VIDEO_RESKIN_OFFLINE": "0", "PATH": str(Path(os.environ["SystemRoot"]) / "System32")}
    environment.pop("VIDEO_RESKIN_WINKY_KEY", None)
    cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"

    def invoke(args, text=None, expected=0):
        command = f'{cmd} /d /s /c "{subprocess.list2cmdline([str(plugin / "scripts/video_reskin.cmd"), *args])}"'
        result = subprocess.run(command, input=text, capture_output=True, text=True, encoding="utf-8", env=environment, timeout=600)
        assert result.returncode == expected, (args, result.stdout, result.stderr)
        return result.stdout, result.stderr

    assert "keyframes" in invoke(["--help"])[0]
    report = json.loads(invoke(["runtime"])[0])
    assert report["decision"] == "pass", report
    source = folder / "素材 1.mp4"
    source.write_bytes(b"not really a video")
    order = {"sources": [str(source)], "output_root": str(folder / "output"), "request": "中文 stdin 测试"}
    out, _ = invoke(["init", "--work-order", "-"], json.dumps(order, ensure_ascii=False))
    job = Path(out.splitlines()[0].split(": ", 1)[1])
    saved = json.loads((job / "job.json").read_text(encoding="utf-8"))
    assert saved["request"] == order["request"]
    _, err = invoke(["auth"])
    assert "missing" in invoke(["auth"])[0]
    _, err = invoke(["render", "--job-dir", str(job)], expected=2)
    assert "no plan yet" in err, err
    environment["VIDEO_RESKIN_OFFLINE"] = "1"
    invoke(["runtime"])
    invoke(["status", "--job-dir", str(job)])
    python = folder / "cache/environment/Scripts/python.exe"
    result = subprocess.run([str(python), "-B", "-c", "import importlib.util; assert importlib.util.find_spec('numpy') is None"], env=environment)
    assert result.returncode == 0
    assert before == snapshot(plugin), "Skill folder changed during execution"
    print(json.dumps({"cold_start": "pass", "unicode_json_stdin": "pass", "offline_reuse": "pass", "skill_immutable": True,
                      "test_directory": str(folder)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
