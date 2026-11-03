"""Inspect the plugin and selected runtime capability, without downloading anything."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from reskin import runtime

ROOT = Path(__file__).resolve().parent.parent


def verify(feature="base") -> dict:
    failures = []
    for filename in ("plugin.json", "skills/video-reskin/SKILL.md", "skills/video-reskin/agents/openai.yaml",
                     "skills/video-reskin/references/workflow.md", "skills/video-reskin/references/review.md",
                     "assets/runtime.json", "assets/icon.svg", "assets/prompts.json", "scripts/video_reskin.ps1",
                     "scripts/video_reskin.cmd", "requirements/media.txt", "requirements/login.txt"):
        if not (ROOT / filename).is_file():
            failures.append(f"missing package file: {filename}")
    if sys.version_info[:3] != (3, 13, 15) or sys.maxsize <= 2**32:
        failures.append("64-bit Python 3.13.15 required; use video_reskin.ps1")
    try:
        metadata = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        if not metadata["extensions"]["com.openai"]["requires_local_executor"]:
            failures.append("requires_local_executor must be true")
        info = runtime.status(feature)
        failures.extend(f"not cached: {name}" for name in info["missing"])
    except (OSError, ValueError, KeyError, runtime.RuntimeFailure) as exc:
        info = {}
        failures.append(str(exc))
    return {"decision": "reject" if failures else "pass", "failures": failures, "runtime": info}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature", choices=("base", "media", "login"), default="base")
    args = parser.parse_args()
    report = verify(args.feature)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["decision"] == "pass" else 3)
