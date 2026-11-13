"""End-to-end run on the local `.素材` footage through the real launcher (needs `auth --key` and `login` first).

Walks every segment through refs -> render -> check, approves passed checks unseen, redoes failed ones up to the
retry limit and then takes the latest result with select-best; writes the results to dev/smoke/latest.json. The job
itself lives in a temporary folder outside the repository.
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
MAX_RETRIES = 2


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


def results(output):
    """The `result` of every JSON line the launcher printed."""
    return [json.loads(line)["result"] for line in output.splitlines() if line.startswith("{")]


def run_source(job, sid):
    """Segment by segment until every one is approved; returns the check verdicts."""
    base = ["--job-dir", str(job), "--only", sid]
    checks = []
    while True:
        if results(launcher(["refs", *base])) == ["all segments approved"]:
            return checks
        for attempt in range(MAX_RETRIES + 1):
            launcher(["render", *base])
            check = results(launcher(["check", *base]))[0]
            checks.append({"source": sid, "attempt": attempt, "verdict": check["verdict"]})
            if check["verdict"] == "pass":
                launcher(["approve", *base, "--segment", segment_of(job, sid)])
                break
            if attempt < MAX_RETRIES:
                launcher(["redo", *base, "--segment", segment_of(job, sid), "--reason", "Smoke test: check failed"])
        else:
            key = segment_of(job, sid)
            state = next(row for row in results(launcher(["status", *base]))[0] if f"{sid}:{row['segment']}" == key)
            launcher(["select-best", *base, "--segment", key, "--generation-id", state["generation_id"],
                      "--reason", "Smoke test: latest result taken unseen after the retries"])


def segment_of(job, sid):
    """The first unapproved segment of a source, as S01:K02."""
    rows = results(launcher(["status", "--job-dir", str(job), "--only", sid]))[0]
    return f"{sid}:{next(row['segment'] for row in rows if not row['approved'])}"


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
    for command in ("analyze", "survey", "plan"):
        launcher([command, "--job-dir", str(job)])
    checks = []
    for number in range(1, len(sources) + 1):
        checks += run_source(job, f"S{number:02d}")
    launcher(["assemble", "--job-dir", str(job)])
    delivered = sorted(str(p) for p in job.parent.glob("*.mp4"))
    report = {"job": str(job), "sources": [p.name for p in sources], "delivered": delivered, "checks": checks}
    (ROOT / "dev/smoke/latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
