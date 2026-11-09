"""video-reskin command line: auth/login -> init -> analyze -> survey -> plan -> refs -> render -> assemble -> review."""
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
        lines.append(f"winky: key {checked['key']} (Gemini review)")
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
                  "next: survey"])
    return 0


def cmd_survey(args) -> int:
    runtime.ensure("media")
    from reskin import survey as SURVEY
    job = args.job_dir.resolve()
    state = JOB.load(job)
    catalog_path = job / "catalog/catalog.json"
    if not catalog_path.is_file():
        raise FileNotFoundError("no catalog yet: run analyze first")
    results = SURVEY.run_all(job, state, read(catalog_path), selection(args.only), redo=args.redo)
    lines, failed = [], False
    for r in results:
        if r.get("error"):
            failed = True
            lines.append(f"{r['id']}: survey failed: {r['error']}")
            continue
        scenes = "; ".join(f"{s['name']} {s['start']:g}-{s['end']:g} s ({len(s['views'])} views)" for s in r["scenes"])
        subjects = "; ".join(f"P{k:02d} {x['what']} ({x['appears']}, {x['start']:g}-{x['end']:g} s, at {x['at']:g})"
                             for k, x in enumerate(r["subjects"], 1))
        lines.append(f"{r['id']}{' (reused)' if r.get('reused') else ''}: scenes: {scenes or 'none'}; "
                     f"subjects: {subjects or 'none'}")
    show(lines + [f"survey: {job / 'catalog/survey'} (<id>.json); sheets: {job / 'catalog'} (<id>-survey.jpg)",
                  "next: look at every survey sheet with the catalog sheets, write the edits file (correct scenes or "
                  "subjects there when Gemini picked a bad view, crop or moment), then plan --edits <edits.json>"])
    return RULE_FAILURE if failed else 0


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
                     f"({cuts} seams at cuts, {inner} inside shots), {len(row['scenes'])} scene map(s), "
                     f"{len(row['subjects'])} subject sheet(s), remove: {describe(row['remove']) or 'nothing'}"
                     + ("" if row["surveyed"] or row.get("scenes") else " (not surveyed)"))
    if missing:
        lines.append(f"note: no edits for {', '.join(missing)} (defaults used: keep up to the end-card candidate)")
    show(lines + [f"plan: {job / 'reports/plan.md'}", "next: refs"])
    return 0


def cmd_refs(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import refs as REFS
    redo = selection(args.redo) or set()
    results = REFS.run_all(job, state, plan, JOB.style(state), selection(args.only) or (redo or None), redo,
                           workers=args.workers)
    failed = [r for r in results if r.get("error")]
    made = [r for r in results if not r.get("error") and not r.get("reused")]
    lines = [f"references: {len(made)} made, {len(results) - len(made) - len(failed)} reused, {len(failed)} failed"]
    lines += [f"  {r['key']}: {r['error']}" for r in failed]
    lines.append(f"sheets: {job / 'sheets'} (<id>-refs.png: views | scene map, crop | three views)")
    lines.append("next: " + ("fix the failures and run refs again" if failed else
                             "look at every refs sheet (make a bad one again with refs --redo <id>:scene1 or <id>:P02 "
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
        checked = "; ".join(f"{s['segment']} at {s['time']:.2f} s {s['mode']} {s['verdict']} ({s['difference']} vs "
                            f"motion {s['motion']}, region {s['local']})" for s in result["seams"] if "verdict" in s)
        warped = ", ".join(f"{t['segment']} {t['held']} held/{t['skipped']} skipped" for t in result["timing"]
                           if t["held"] or t["skipped"])
        if warped:
            colour = (colour + "; " if colour else "") + "retimed: " + warped
        if checked:
            colour = (colour + "; " if colour else "") + "seams: " + checked
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
        if result.get("stills"):
            lines.append(f"  stills (every {REVIEW.STILL_STEP:g} s, lossless): {Path(result['stills'][0]).parent} "
                         f"({len(result['stills'])} pairs)")
        for seam in result.get("seams") or []:
            lines.append(f"  seam {seam['segment']} at {seam['time']:.2f} s ({seam['seam']}): {seam['sheet']}")
    show(lines + [f"reports: {job / 'reports/review'}",
                  "next: look at every still and every seam sheet yourself; confirm each issue with frames "
                  "--source <id> --at <seconds>; send confirmed flaws back with redo (at most twice per segment), "
                  "then (refs,) render, assemble and review again"])
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
        entry = RENDER.bump(job, key, args.reason, args.prompt, replace=args.replace)
        lines.append(f"{key}: attempt {entry['attempt']} queued ({args.reason})")
    steps = "refs --redo <the broken scene map or sheet>, render" if args.refs else "render"
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
    elif not (job / "catalog/survey").is_dir():
        step = "survey"
    elif not plan["sources"]:
        step = "plan --edits <edits.json>"
    for source_id, row in plan["sources"].items():
        keys = [s["id"] for s in row["segments"]]
        own = [f"scene{k}" for k in range(1, len(row["scenes"]) + 1)] + [f"P{k:02d}" for k in range(1, len(row["subjects"]) + 1)]
        frames_ready = sum(1 for k in own if (job / "refs" / f"{source_id}-{k}.png").is_file())
        rendered = 0
        for k in keys:
            record = job / "state/render" / f"{source_id}-{k}.json"
            rendered += record.is_file() and read(record).get("status") == "done"
        assembled = (job / "state/assembled" / f"{source_id}.json").is_file()
        rounds_path = job / "state/review" / f"{source_id}.json"
        rounds = read(rounds_path)["rounds"] if rounds_path.is_file() else []
        last = f"round {rounds[-1]['round']} {rounds[-1]['verdict']} ({rounds[-1]['issues']} issues)" if rounds else "no review"
        lines.append(f"{source_id}: references {frames_ready}/{len(own)}, segments {rendered}/{len(keys)}, "
                     f"assembled: {'yes' if assembled else 'no'}, {last}")
        if step is None:
            if frames_ready < len(own):
                step = "refs"
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
    survey = command("survey", cmd_survey, help="Gemini names the scene views and the subjects of every source")
    survey.add_argument("--only", nargs="*", help="source IDs")
    survey.add_argument("--redo", action="store_true", help="ask Gemini again even if a survey exists")
    plan = command("plan", cmd_plan, help="apply the edits file and cut every kept range into segments")
    plan.add_argument("--edits", type=Path, help="edits JSON (kept range, materials, removals, scenes and subjects per source)")
    refs = command("refs", cmd_refs, help="scene maps (4K, max) and three-view subject sheets (1K, high) with GPT Image")
    refs.add_argument("--only", nargs="*", help="source IDs or keys like S01:scene1 S01:P02")
    refs.add_argument("--redo", nargs="*", help="keys or source IDs to make again even if unchanged")
    refs.add_argument("--workers", type=int, default=3)
    render = command("render", cmd_render, help="MiniMax H3 video edit of every segment on Lumos")
    render.add_argument("--only", nargs="*", help="source IDs or keys like S01:K02")
    render.add_argument("--workers", type=int, default=4, help="generations in flight at once")
    assemble = command("assemble", cmd_assemble, help="retime, colour-match and join segments with the original audio")
    assemble.add_argument("--only", nargs="*", help="source IDs")
    review = command("review", cmd_review, help="Gemini review of each delivered video against its original")
    review.add_argument("--only", nargs="*", help="source IDs")
    review.add_argument("--fps", type=float, default=None, help="frames per second Gemini samples (default: 4 up to 75 s, else 2)")
    frames = command("frames", cmd_frames, help="stills of original and restyled around given times")
    frames.add_argument("--source", required=True)
    frames.add_argument("--at", type=float, nargs="+", required=True, help="seconds in the delivered video")
    frames.add_argument("--span", type=float, default=.3)
    redo = command("redo", cmd_redo, help="send confirmed broken segments back for another render (twice at most)")
    redo.add_argument("--segment", nargs="+", required=True, help="keys like S01:K02")
    redo.add_argument("--reason", required=True, help="what is wrong, as confirmed on the stills")
    redo.add_argument("--prompt", help="extra instruction added to the video prompt of these segments")
    redo.add_argument("--replace", action="store_true", help="drop the earlier corrective sentences of these segments")
    redo.add_argument("--refs", action="store_true", help="a scene map or subject sheet is wrong: make it again too")
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
