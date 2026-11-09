"""End-to-end run on the local `.素材` footage through the real launcher (needs `auth --key` and `login` first).

Uses the default edits (kept range up to the end-card candidate, logo and warning line removed) and writes the
results to dev/smoke/latest.json; the job itself lives in a temporary folder outside the repository.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skill/video-reskin"
FOOTAGE = ROOT / ".素材"


def launcher(args, text=None, timeout=7200):
    cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    line = f'{cmd} /d /s /c "{subprocess.list2cmdline([str(SKILL / "scripts/video_reskin.cmd"), *args])}"'
    started = time.monotonic()
    result = subprocess.run(line, input=text, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    print(f"$ {' '.join(args[:1])} ({time.monotonic() - started:.0f} s, exit {result.returncode})", flush=True)
    print(result.stdout.strip(), flush=True)
    if result.returncode:
        print(result.stderr[-3000:], flush=True)
        raise SystemExit(f"{args[0]} failed")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1, help="number of .素材 videos to run")
    parser.add_argument("--match", default="1080x1920", help="only footage whose name contains this")
    args = parser.parse_args()
    sources = sorted(p for p in FOOTAGE.glob("*.mp4") if args.match in p.name)[: args.count]
    if not sources:
        raise SystemExit(f"no footage matching {args.match} in {FOOTAGE}")
    folder = Path(tempfile.mkdtemp(prefix="vr-e2e-"))
    order = {"sources": [str(p) for p in sources], "output_root": str(folder), "request": "smoke test"}
    job = Path(launcher(["init", "--work-order", "-"], json.dumps(order, ensure_ascii=False)).splitlines()[0].split(": ", 1)[1])
    launcher(["analyze", "--job-dir", str(job)])
    edits = {"defaults": {"remove": ["游戏logo「WHITEOUT SURVIVAL」"]}, "sources": {}}
    edits_path = job / "config" / "edits-smoke.json"
    edits_path.parent.mkdir(parents=True, exist_ok=True)
    edits_path.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
    launcher(["plan", "--job-dir", str(job), "--edits", str(edits_path)])
    launcher(["keyframes", "--job-dir", str(job)])
    launcher(["render", "--job-dir", str(job)])
    launcher(["assemble", "--job-dir", str(job)])
    review = launcher(["review", "--job-dir", str(job)])
    delivered = sorted(str(p) for p in job.parent.glob("*.mp4"))
    report = {"job": str(job), "sources": [p.name for p in sources], "delivered": delivered,
              "review": [line for line in review.splitlines() if " round " in line]}
    (ROOT / "dev/smoke/latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
