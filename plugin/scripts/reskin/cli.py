"""video-reskin command line: auth/login -> init -> analyze -> plan -> keyframes -> render -> assemble -> review."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reskin import credentials, job as JOB, net, runtime
from reskin.common import read, write

RULE_FAILURE = 2


def show(lines) -> None:
    print("\n".join(lines) if isinstance(lines, list) else lines, flush=True)


def selection(values: list[str] | None) -> set[str] | None:
    """--only S01 S02:K03 ... -> {"S01", "S02:K03"}; None means everything."""
    if not values:
        return None
    return {part.strip() for value in values for part in value.split(",") if part.strip()}


def load_plan(job: Path) -> dict:
    path = job / "config/plan.json"
    if not path.is_file():
        raise FileNotFoundError("no plan yet: run plan --edits <edits.json> first")
    return read(path)


def cmd_auth(args) -> int:
    from reskin import lumos, winky
    if args.key:
        credentials.save_winky_key(args.key)
    if args.forget_login:
        credentials.forget_lumos_login()
    summary = credentials.summary()
    lines = [f"credentials: {summary['file']}",
             f"winky key: {summary['winky_key'] or 'missing (auth --key <key>)'}"
             + (f" ({summary['winky_key_source']})" if summary["winky_key"] else "")]
    if summary["winky_key"]:
        checked = winky.check_key()
        lines.append(f"winky: key {checked['key']}; image model: {checked['image_model']}")
    try:
        live = lumos.user()
        lines.append(f"lumos: logged in as {live.get('name')} ({live.get('user_id')})")
    except credentials.MissingCredential as error:
        lines.append(f"lumos: {error}")
    show(lines)
    return 0


def cmd_login(args) -> int:
    if args.check:
        from reskin import lumos
        live = lumos.user()
        show(f"lumos: logged in as {live.get('name')} ({live.get('user_id')})")
        return 0
    runtime.ensure("media")
    runtime.ensure("login")
    from reskin import login
    result = login.run(timeout=args.timeout, open_image=not args.no_open)
    show([f"lumos: logged in as {result['name']} ({result['user_id']})", "next: init (or continue the job)"])
    return 0


def cmd_init(args) -> int:
    order = json.loads(sys.stdin.buffer.read().decode("utf-8-sig")) if str(args.work_order) == "-" else read(args.work_order)
    job, state = JOB.init(order, args.job_dir)
    style = state["style"] if isinstance(state["style"], str) else state["style"].get("label")
    show([f"job_dir: {job}", f"delivery: {state['delivery_dir']}",
          f"sources: {len(state['sources'])} ({', '.join(r['id'] + ' ' + r['name'] for r in state['sources'])})",
          f"style: {style}, options: {json.dumps(state['options'], ensure_ascii=False)}", "next: analyze"])
    return 0


def cmd_analyze(args) -> int:
    runtime.ensure("media")
    from reskin import analyze
    job = args.job_dir.resolve()
    state = JOB.load(job)
    lines = []
    for r in analyze.run_all(job, state, selection(args.only)):
        card = f"end card from {r['end_card']['start']:.2f} s" if r["end_card"] else "no end card found"
        lines.append(f"{r['id']}: {r['info']['width']}x{r['info']['height']} {r['duration']:.1f} s, {len(r['shots'])} shots, "
                     f"{card}, {len(r['overlays'])} still overlays")
    show(lines + [f"catalog: {job / 'catalog/catalog.md'}; sheets: {job / 'catalog'} (<id>.jpg, <id>-end.jpg)",
                  "next: look at every sheet, write the edits file, then plan --edits <edits.json>"])
    return 0


def cmd_plan(args) -> int:
    from reskin import plan as PLAN
    from reskin.prompts import describe
    job = args.job_dir.resolve()
    state = JOB.load(job)
    catalog_path = job / "catalog/catalog.json"
    if not catalog_path.is_file():
        raise FileNotFoundError("no catalog yet: run analyze first")
    catalog = read(catalog_path)
    stored = job / "config/edits.json"
    if args.edits:
        edits = PLAN.check_edits(read(args.edits), state, catalog)
        write(stored, edits)
    else:
        edits = read(stored) if stored.is_file() else {"defaults": {}, "sources": {}}
    missing = [r["id"] for r in catalog["sources"] if r["id"] not in edits["sources"]]
    plan = PLAN.build(job, state, edits, catalog, JOB.style(state), float(state["options"]["max_segment_seconds"]))
    lines = []
    for source_id, row in plan["sources"].items():
        cuts = sum(1 for s in row["segments"] if s["seam"] == "cut")
        inner = sum(1 for s in row["segments"] if s["seam"] == "continuous")
        lines.append(f"{source_id}: keep {row['keep'][0]:.2f}-{row['keep'][1]:.2f} s, {len(row['segments'])} segments "
                     f"({cuts} seams at cuts, {inner} inside shots), anchor {row['anchor']:.2f} s, "
                     f"remove: {describe(row['remove']) or 'nothing'}")
    if missing:
        lines.append(f"note: no edits for {', '.join(missing)} (defaults used: keep up to the end-card candidate)")
    show(lines + [f"plan: {job / 'reports/plan.md'}", "next: keyframes"])
    return 0


def cmd_keyframes(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import keyframes as KEYFRAMES
    redo = selection(args.redo) or set()
    results = KEYFRAMES.run_all(job, state, plan, JOB.style(state), selection(args.only) or (redo or None), redo,
                                workers=args.workers)
    failed = [r for r in results if r.get("error")]
    made = [r for r in results if not r.get("error") and not r.get("reused")]
    backends = sorted({r.get("backend", "?") for r in made})
    lines = [f"keyframes: {len(made)} made, {len(results) - len(made) - len(failed)} reused, {len(failed)} failed"
             + (f" (via {', '.join(backends)})" if made else "")]
    lines += [f"  {r['key']}: {r['error']}" for r in failed]
    lines.append(f"sheets: {job / 'sheets'} (<id>-keyframes.jpg: original | styled)")
    lines.append("next: " + ("fix the failures and run keyframes again" if failed else
                             "look at every keyframe sheet (make a bad one again with keyframes --redo <id>:<key> "
                             "after adjusting the edits), then render"))
    show(lines)
    return RULE_FAILURE if failed else 0


def cmd_render(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import render as RENDER
    results = RENDER.run_all(job, state, plan, JOB.style(state), selection(args.only), workers=args.workers)
    failed = [r for r in results if r.get("error")]
    lines = [f"segments: {sum(1 for r in results if not r.get('error'))} ready "
             f"({sum(1 for r in results if r.get('reused'))} reused), {len(failed)} failed"]
    lines += [f"  {r['key']}: {r['error']}" for r in failed]
    lines.append("next: " + ("fix the failures and run render again (finished segments are kept)" if failed else "assemble"))
    show(lines)
    return RULE_FAILURE if failed else 0


def cmd_assemble(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import assemble as ASSEMBLE
    only = selection(args.only)
    lines, failed = [], False
    for source_id, row in plan["sources"].items():
        if only and source_id not in only:
            continue
        try:
            result = ASSEMBLE.assemble_source(job, state, source_id, row, JOB.style(state))
        except ValueError as error:
            failed = True
            lines.append(f"{source_id}: not assembled: {error}")
            continue
        colour = "; ".join(f"{c['before']} diff {c['raw_difference']} -> {c['residual']}" for c in result["colour"])
        lines.append(f"{source_id}: {Path(result['output']).name} {result['size'][0]}x{result['size'][1]} "
                     f"{result['duration']:.2f} s" + (f", seam colours: {colour}" if colour else "")
                     + (f", problems: {'; '.join(result['problems'])}" if result["problems"] else ""))
    show(lines + ["next: " + ("render the missing segments, then assemble again" if failed else "review")])
    return RULE_FAILURE if failed else 0


def cmd_review(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import review as REVIEW
    only = selection(args.only)
    lines, failed = [], False
    for source_id, row in plan["sources"].items():
        if only and source_id not in only:
            continue
        if not (job / "state/assembled" / f"{source_id}.json").is_file():
            lines.append(f"{source_id}: not assembled yet")
            failed = True
            continue
        result = REVIEW.review_source(job, state, source_id, row, fps=args.fps)
        high = sum(1 for i in result["issues"] if i["severity"] == "high")
        lines.append(f"{source_id} round {result['round']}: {result['verdict']}, {len(result['issues'])} issues "
                     f"({high} high): {result['summary']}")
        for issue in result["issues"]:
            lines.append(f"  #{issue['n']} [{issue['severity']}] {issue['category']} {issue['start']:.2f}-{issue['end']:.2f} s "
                         f"-> {', '.join(issue['segments'])}: {issue['description']}")
    show(lines + [f"reports: {job / 'reports/review'}",
                  "next: confirm each issue with frames --source <id> --at <seconds>; send confirmed ones back with "
                  "redo, then (keyframes,) render, assemble and review again"])
    return RULE_FAILURE if failed else 0


def cmd_frames(args) -> int:
    runtime.ensure("media")
    from reskin import review as REVIEW
    job = args.job_dir.resolve()
    state = JOB.load(job)
    paths = REVIEW.stills(job, state, args.source, args.at, span=args.span)
    show([f"frames: {p}" for p in paths])
    return 0


def cmd_redo(args) -> int:
    from reskin import render as RENDER
    job = args.job_dir.resolve()
    JOB.load(job)
    plan = load_plan(job)
    keys = sorted(selection(args.segment) or [])
    lines = []
    for key in keys:
        source_id, _, segment_id = key.partition(":")
        row = plan["sources"].get(source_id)
        if not row or segment_id not in {s["id"] for s in row["segments"]}:
            raise ValueError(f"unknown segment {key}; segments look like S01:K02 (see reports/plan.md)")
        entry = RENDER.bump(job, key, args.reason, args.prompt)
        lines.append(f"{key}: attempt {entry['attempt']} queued ({args.reason})")
    steps = f"keyframes --redo {' '.join(keys)}, render" if args.keyframe else "render"
    show(lines + [f"next: {steps}, assemble, review"])
    return 0


def cmd_status(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan_path = job / "config/plan.json"
    plan = read(plan_path) if plan_path.is_file() else {"sources": {}}
    lines = [f"delivery: {state['delivery_dir']}", f"sources: {len(state['sources'])}, planned: {len(plan['sources'])}"]
    step = None
    if not (job / "catalog/catalog.json").is_file():
        step = "analyze"
    elif not plan["sources"]:
        step = "plan --edits <edits.json>"
    for source_id, row in plan["sources"].items():
        keys = [s["id"] for s in row["segments"]]
        frames_ready = sum(1 for k in ["anchor", *keys] if (job / "keyframes" / f"{source_id}-{k}.png").is_file())
        rendered = 0
        for k in keys:
            record = job / "state/render" / f"{source_id}-{k}.json"
            rendered += record.is_file() and read(record).get("status") == "done"
        assembled = (job / "state/assembled" / f"{source_id}.json").is_file()
        rounds_path = job / "state/review" / f"{source_id}.json"
        rounds = read(rounds_path)["rounds"] if rounds_path.is_file() else []
        last = f"round {rounds[-1]['round']} {rounds[-1]['verdict']} ({rounds[-1]['issues']} issues)" if rounds else "no review"
        lines.append(f"{source_id}: keyframes {frames_ready}/{len(keys) + 1}, segments {rendered}/{len(keys)}, "
                     f"assembled: {'yes' if assembled else 'no'}, {last}")
        if step is None:
            if frames_ready < len(keys) + 1:
                step = "keyframes"
            elif rendered < len(keys):
                step = "render"
            elif not assembled:
                step = "assemble"
            elif not rounds:
                step = "review"
    show(lines + [f"next: {step or 'confirm review issues, redo what is broken, or deliver'}"])
    return 0


def cmd_runtime(args) -> int:
    if args.install:
        runtime.ensure(args.feature)
        if args.feature == "media":
            runtime.binary("ffmpeg"); runtime.binary("ffprobe")
    from verify_runtime import verify
    report = verify(args.feature)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["decision"] == "pass" else 3


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, job=True, help=None):
        action = sub.add_parser(name, help=help)
        action.set_defaults(handler=handler)
        if job:
            action.add_argument("--job-dir", type=Path, required=True)
        return action

    auth = command("auth", cmd_auth, job=False, help="store or check the Winky API key and the Lumos login")
    auth.add_argument("--key", help="the user's Winky API key (stored encrypted for this Windows user)")
    auth.add_argument("--forget-login", action="store_true", help="drop the stored Lumos login")
    login = command("login", cmd_login, job=False, help="Feishu QR login to Lumos (opens the QR image on screen)")
    login.add_argument("--timeout", type=float, default=300, help="seconds to wait for the scan (QR codes refresh on their own)")
    login.add_argument("--no-open", action="store_true", help="only write the QR image, do not open it")
    login.add_argument("--check", action="store_true", help="only check the stored login")
    init = command("init", cmd_init, job=False, help="create the job from a work order")
    init.add_argument("--work-order", type=Path, required=True, help="JSON file, or - for stdin")
    init.add_argument("--job-dir", type=Path)
    analyze = command("analyze", cmd_analyze, help="media facts, cuts, end card, overlays and sheets")
    analyze.add_argument("--only", nargs="*", help="source IDs")
    plan = command("plan", cmd_plan, help="apply the edits file and cut every kept range into segments")
    plan.add_argument("--edits", type=Path, help="edits JSON (kept range, materials, removals, anchor per source)")
    keyframes = command("keyframes", cmd_keyframes, help="style anchor and segment keyframes with GPT Image")
    keyframes.add_argument("--only", nargs="*", help="source IDs or keys like S01:anchor S01:K02")
    keyframes.add_argument("--redo", nargs="*", help="keys or source IDs to make again even if unchanged")
    keyframes.add_argument("--workers", type=int, default=3)
    render = command("render", cmd_render, help="Seedance 2.5 video edit of every segment on Lumos")
    render.add_argument("--only", nargs="*", help="source IDs or keys like S01:K02")
    render.add_argument("--workers", type=int, default=4, help="generations in flight at once")
    assemble = command("assemble", cmd_assemble, help="retime, colour-match and join segments with the original audio")
    assemble.add_argument("--only", nargs="*", help="source IDs")
    review = command("review", cmd_review, help="Gemini review of each delivered video against its original")
    review.add_argument("--only", nargs="*", help="source IDs")
    review.add_argument("--fps", type=float, default=2.0, help="frames per second Gemini samples")
    frames = command("frames", cmd_frames, help="stills of original and restyled around given times")
    frames.add_argument("--source", required=True)
    frames.add_argument("--at", type=float, nargs="+", required=True, help="seconds in the delivered video")
    frames.add_argument("--span", type=float, default=.3)
    redo = command("redo", cmd_redo, help="send confirmed broken segments back for another render")
    redo.add_argument("--segment", nargs="+", required=True, help="keys like S01:K02")
    redo.add_argument("--reason", required=True, help="what is wrong, as confirmed on the stills")
    redo.add_argument("--prompt", help="extra instruction added to the video prompt of these segments")
    redo.add_argument("--keyframe", action="store_true", help="the keyframe itself is wrong: make it again too")
    command("status", cmd_status, help="progress per source and the next step")
    inspection = command("runtime", cmd_runtime, job=False, help="inspect or prefetch the private runtime")
    inspection.add_argument("--feature", choices=("base", "media", "login"), default="base")
    inspection.add_argument("--install", action="store_true")
    args = parser.parse_args()
    try:
        code = args.handler(args)
    except credentials.MissingCredential as error:
        print(f"credential error: {error}", file=sys.stderr, flush=True)
        code = RULE_FAILURE
    except runtime.RuntimeFailure as error:
        print(f"runtime error: {error}", file=sys.stderr, flush=True)
        code = 3
    except (ValueError, FileExistsError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        code = RULE_FAILURE
    except (OSError, ImportError) as error:
        print(f"runtime error: {error}; retry the command after fixing the runtime", file=sys.stderr, flush=True)
        code = 3
    except (RuntimeError, net.HttpError) as error:
        print(f"service error: {error}", file=sys.stderr, flush=True)
        code = 3
    raise SystemExit(code)


if __name__ == "__main__":
    main()
