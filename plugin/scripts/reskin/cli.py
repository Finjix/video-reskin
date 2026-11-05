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
        live = [s for s in row["segments"] if s.get("live")]
        lines.append(f"{source_id}: keep {row['keep'][0]:.2f}-{row['keep'][1]:.2f} s, {len(row['segments'])} segments "
                     f"({cuts} seams at cuts, {inner} inside shots"
                     + (f"; live-action from the original: {', '.join(f'{s['start']:.2f}-{s['end']:.2f} s' for s in live)}"
                        if live else "")
                     + f"), anchor {row['anchor']:.2f} s, "
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


def render_lines(job: Path, results: list[dict]) -> tuple[list[str], bool]:
    """The render summary: ready / reused / held / waiting / failed segments, with the gate stills."""
    segments = [r for r in results if r.get("key", "").count(":") == 1]
    failed = [r for r in results if r.get("error") and not r.get("waiting")]
    held = [r for r in segments if r.get("held")]
    waiting = [r for r in segments if r.get("waiting")]
    ready = [r for r in segments if not r.get("error") and not r.get("waiting")]
    lines = [f"segments: {len(ready)} ready ({sum(1 for r in ready if r.get('reused'))} reused), {len(held)} held by "
             f"the segment check, {len(waiting)} waiting behind them, {len(failed)} failed"]
    for r in held:
        gate = r["gate"]
        lines.append(f"  {r['key']} held: {'; '.join(gate.get('reasons') or [])}")
        if gate.get("realism"):
            from reskin import review as REVIEW
            lines.append(f"    realism (5 = real footage): {REVIEW.realism_line(gate['realism'])}")
        for wipe in gate.get("wipes") or []:
            if wipe.get("verdict") in ("smeared", "soft"):
                lines.append(f"    wiped corner {wipe['what']}: {wipe['verdict']} (detail {wipe['share']} of the scene "
                             f"around it)")
        for issue in gate.get("issues") or []:
            lines.append(f"    #{issue['n']} [{issue['severity']}] {issue['category']} {issue['start']:.2f}-{issue['end']:.2f} s: "
                         f"{issue['description']}")
        lines += [f"    stills: {sheet}" for sheet in gate.get("sheets") or []]
    lines += [f"  {r['key']}: {r.get('error') or r.get('note')}" for r in failed + waiting]
    steps = []
    if failed:
        steps.append("fix the failures and render again (finished segments are kept)")
    if held:
        steps.append("look at each held segment's stills: a confirmed flaw -> redo it (with --prompt); a false alarm "
                     "-> accept --segment <key> --reason <why>; then render again")
    if not steps:
        steps.append("assemble")
    lines.append("next: " + "; ".join(steps))
    return lines, bool(failed)


def cmd_render(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import render as RENDER
    from reskin.common import job_lock
    waits = RENDER.twin_waits(job, state, plan, selection(args.only))
    with job_lock(job, "render"):
        results = RENDER.run_all(job, state, plan, JOB.style(state), selection(args.only), workers=args.workers,
                                 gate=not args.no_gate, hold=set(waits))
    lines, failed = render_lines(job, results)
    show(wait_lines(waits) + lines)
    return RULE_FAILURE if failed else 0


def wait_lines(waits: dict[str, str]) -> list[str]:
    return [f"{sid}: not rendered, {why} (like); --only {sid} renders it now, without fixes found later"
            for sid, why in waits.items()]


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
        lines.append(f"{source_id}: {Path(result['output']).name} {result['size'][0]}x{result['size'][1]} "
                     f"{result['duration']:.2f} s"
                     + (f", HUD from the original: {len(result['hud'])}" if result["hud"] else "")
                     + (f", problems: {'; '.join(result['problems'])}" if result["problems"] else ""))
        lines += seam_lines(result)
        if result.get("seam_sheet"):
            lines.append(f"  seam sheet: {result['seam_sheet']}")
    show(lines + ["next: " + ("render the missing segments, then assemble again" if failed else
                              "look at the seam sheet (a visible jump: redo the segment after it; a smeared logo "
                              "corner or a kept element unlike the original: see the skill's logo rows), then review")])
    return RULE_FAILURE if failed else 0


def cmd_compare(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import assemble as ASSEMBLE
    only = selection(args.only)
    if args.with_videos and len([s for s in plan["sources"] if not only or s in only]) != 1:
        raise ValueError("--with compares one source with earlier versions of it: pick it with --only S01")
    lines, failed = [], False
    for source_id in plan["sources"]:
        if only and source_id not in only:
            continue
        try:
            made = ASSEMBLE.compare_source(job, state, source_id, layout=args.layout, extras=args.with_videos,
                                           labels=args.labels)
        except ValueError as error:
            failed = True
            lines.append(f"{source_id}: no comparison: {error}")
            continue
        lines.append(f"{source_id}: {Path(made['output']).name}" + (" (unchanged)" if made["reused"] else
                     f" {made['size'][0]}x{made['size'][1]} {made['duration']:.2f} s"))
    show(lines)
    return RULE_FAILURE if failed else 0


def seam_lines(assembled: dict) -> list[str]:
    """Every seam (the switch, and where a chained segment's own frames start) and every wiped corner, measured."""
    lines = []
    for seam in assembled["seams"]:
        inside = seam.get("inside") or {}
        lines.append(f"  seam {seam['segment']} at {seam['time']:.2f} s: " + (
            f"{seam['jump']} (switch: jump {seam['mismatch']} vs motion {seam['motion']}, local {seam.get('local', 0)}"
            + (f"; own frames from {inside['time']:.2f} s: {inside['jump']}, step {inside['mismatch']} vs "
               f"{inside['motion']}" if inside else "")
            + (f"; look step: saturation z {seam['look']['saturation']['z']}, colour z {seam['look']['colour']['z']} "
               f"-> {seam['look']['jump']}" if seam.get("look") else "") + ")" if seam.get("jump") else "hard cut"))
    for wipe in assembled.get("wipes") or []:
        lines.append(f"  wiped corner {wipe['what']} {wipe['start']:.1f}-{wipe['end']:.1f} s: {wipe['verdict']}"
                     + (f" (detail {wipe['share']} of the scene around it)" if wipe.get("share") is not None else ""))
    for kept in assembled.get("fidelity") or []:
        lines.append(f"  kept {kept['what']} {kept['start']:.1f}-{kept['end']:.1f} s: {kept['verdict']}"
                     + (f" (grey difference {kept['diff']} to the original, matte {kept.get('method')})"
                        if kept.get("diff") is not None else ""))
    return lines


def review_lines(result: dict, limit: int) -> list[str]:
    from reskin import review as REVIEW
    high = sum(1 for i in result["issues"] if i["severity"] == "high")
    lines = [f"{result['source']} round {result['round']} of {limit}: {result['verdict']}, {len(result['issues'])} issues "
             f"({high} high): {result['summary']}"]
    if result.get("realism"):
        lines.append(f"  realism (5 = real footage): {REVIEW.realism_line(result['realism'])}")
    lines += [f"  {REVIEW.describe_issue(issue)}" for issue in result["issues"]]
    lines += [f"  stills: {sheet}" for sheet in result.get("sheets") or []]
    if result.get("identity_sheets"):
        lines.append(f"  identity close-ups (whole video beside the styled keyframes): "
                     f"{', '.join(result['identity_sheets'])}")
    if result.get("wipe_sheets"):
        lines.append(f"  wiped corners over time (original above, delivered below): "
                     f"{', '.join(result['wipe_sheets'])}")
    return lines


REVIEW_NEXT = ("next: confirm each high issue on its stills (upper row original, lower row restyled; the triage hint "
               "is Gemini's, not a verdict); redo only confirmed flaws a viewer sees at once (seams, kept logos or warning lines unlike the original, smeared or "
               "leftover removed elements, garbled text, morphing or vanishing objects, wrong colours of prominent objects), "
               "then run again; medium and low issues are reported, not redone")


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
        try:
            result = REVIEW.review_source(job, state, source_id, row, fps=args.fps)
        except ValueError as error:
            lines.append(f"{source_id}: not reviewed: {error}")
            failed = True
            continue
        lines += review_lines(result, int(state["options"].get("review_rounds", 3)))
    show(lines + [f"reports: {job / 'reports/review'}", REVIEW_NEXT])
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
        seg = next(s for s in row["segments"] if s["id"] == segment_id)
        if seg.get("live"):
            raise ValueError(f"{key} is live-action, delivered from the original (nothing is rendered there); if it "
                             f"is game footage, change `live` in the edits and plan again")
        if args.keyframe and seg["seam"] == "continuous":
            head = next(s["id"] for s in reversed(row["segments"][:row["segments"].index(seg) + 1])
                        if s["seam"] != "continuous")
            raise ValueError(f"{key} has no keyframe (it starts from the previous segment's result); if the shot's "
                             f"keyframe is wrong, redo {source_id}:{head} with --keyframe, else leave --keyframe out")
        entry = RENDER.bump(job, key, args.reason, args.prompt, replace=args.replace_prompt)
        lines.append(f"{key}: attempt {entry['attempt']} queued ({args.reason})"
                     + (f"; corrective sentences now: {entry['prompt']}" if entry.get("prompt") else
                        "; no corrective sentences" if args.replace_prompt else ""))
    steps = f"keyframes --redo {' '.join(keys)}, render" if args.keyframe else "render"
    show(lines + [f"next: {steps}, assemble, review"])
    return 0


def cmd_lint(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    catalog_path = job / "catalog/catalog.json"
    if not catalog_path.is_file():
        raise FileNotFoundError("no catalog yet: run analyze first")
    runtime.ensure("media")
    from reskin import lint as LINT
    from reskin import plan as PLAN
    catalog = read(catalog_path)
    edits = PLAN.check_edits(read(args.edits), state, catalog) if args.edits else None
    results = LINT.run_all(job, state, catalog, edits, selection(args.only))
    lines = []
    for source_id, facts in results:
        if "error" in facts:
            lines.append(f"{source_id}: not checked: {facts['error']}")
            continue
        problems = [c for c in facts.get("edits_check") or [] if c.get("verdict") != "ok"]
        lines.append(f"{source_id}: {len(facts.get('objects') or [])} objects, {len(facts.get('logos') or [])} logos/warnings, "
                     f"{len(facts.get('screen_text') or [])} screen texts, {len(facts.get('hud') or [])} HUD, end card from "
                     f"{facts.get('end_card_start')} s; sheet {facts.get('sheet')}")
        if edits is not None:
            lines.append("  edits: " + ("no problems found" if not problems else f"{len(problems)} to look at"))
            lines += [f"  - [{c['verdict']}] {c['field']}: {c['what']} -> {c['suggestion']}" for c in problems]
    show(lines + [f"lint: {job / 'catalog/lint.md'}; draft edits: {job / 'config/edits.draft.json'}",
                  "next: check the draft against the catalog and lint sheets (Gemini boxes and times are approximate), "
                  "write the edits file, lint --edits <file> if you changed much, then plan --edits <file>"])
    return 0


def cmd_preview(args) -> int:
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import preview as PREVIEW
    results = PREVIEW.run_all(job, state, plan, JOB.style(state), args.only, args.at, workers=args.workers,
                              redo=args.redo)
    failed = [r for r in results if r.get("error")]
    made = [r for r in results if not r.get("error") and not r.get("reused")]
    lines = [f"previews: {len(made)} made, {len(results) - len(made) - len(failed)} reused, {len(failed)} failed"]
    lines += [f"  {r['key']}: {r['error']}" for r in failed]
    lines.append(f"sheet: {job / 'sheets' / (args.only + '-preview.jpg')} (original | preview with the video prompt's "
                 "texts and the segment's corrective sentences)")
    lines.append("next: wrong objects, colours or text on a preview mean the edits are wrong there: fix them, plan, "
                 "then run")
    show(lines)
    return RULE_FAILURE if failed else 0


def cmd_accept(args) -> int:
    from reskin import gate as GATE
    job = args.job_dir.resolve()
    JOB.load(job)
    lines = []
    for key in sorted(selection(args.segment) or []):
        GATE.accept(job, key, args.reason)
        lines.append(f"{key}: accepted ({args.reason}); the rest of its shot renders on the next render")
    show(lines + ["next: render"])
    return 0


def cmd_cost(args) -> int:
    from reskin import ledger
    job = args.job_dir.resolve()
    JOB.load(job)
    value = ledger.summary(job)
    if args.json:
        print(json.dumps(value, ensure_ascii=False))
    else:
        show(ledger.markdown(value))
    return 0


def cmd_run(args) -> int:
    """keyframes (+ previews) -> render with segment checks -> assemble and review each source as soon as its
    segments are done; stops wherever the agent has to look or decide."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    job = args.job_dir.resolve()
    state = JOB.load(job)
    plan = load_plan(job)
    runtime.ensure("media")
    from reskin import assemble as ASSEMBLE, keyframes as KEYFRAMES, render as RENDER, review as REVIEW
    from reskin.common import job_lock, log
    style = JOB.style(state)
    only = selection(args.only)
    results = KEYFRAMES.run_all(job, state, plan, style, only, set())
    failed = [r for r in results if r.get("error")]
    made = [r["key"] for r in results if not r.get("error") and not r.get("reused")]
    if failed:
        show([f"  {r['key']}: {r['error']}" for r in failed] + ["next: fix the failures and run again"])
        return RULE_FAILURE
    if made:
        show([f"new keyframes: {', '.join(made)}", f"sheets: {job / 'sheets'} (<id>-keyframes.jpg)",
              "next: look at every new keyframe sheet; fix the edits or keyframes --redo <key> where needed (a "
              "preview --only <id> --at <t> only for a moment the keyframes do not show); then run again (it "
              "renders once nothing new needs looking at)"])
        return 0
    limit = int(state["options"].get("review_rounds", 3))
    reports: dict[str, list[str]] = {}
    lock = threading.Lock()

    def follow_up(source_id: str):
        row = plan["sources"][source_id]
        lines = []
        try:
            assembled = ASSEMBLE.current(job, state, source_id, row)
            if assembled is None:
                assembled = ASSEMBLE.assemble_source(job, state, source_id, row, style)
            visible = [s for s in assembled["seams"] if s.get("jump") == "visible"]
            smeared = [w for w in assembled.get("wipes") or [] if w.get("verdict") == "smeared"]
            unfaithful = [k for k in assembled.get("fidelity") or [] if k.get("verdict") == "changed"]
            lines.append(f"{source_id}: {Path(assembled['output']).name}"
                         + (f"; seam sheet {assembled['seam_sheet']}" if assembled.get("seam_sheet") else ""))
            lines += seam_lines(assembled)
            last = REVIEW.last_round(job, source_id)
            if last and last.get("assembled") == assembled.get("inputs"):
                lines.append(f"  unchanged since review round {last['round']}:")
                lines += ["  " + line for line in review_lines(last, limit)]
            else:
                lines += ["  " + line for line in review_lines(REVIEW.review_source(job, state, source_id, row), limit)]
            if visible:
                lines.append(f"  visible seams: {', '.join(s['segment'] for s in visible)} (not done until fixed)")
            if smeared:
                lines.append(f"  smeared logo corners: {'; '.join(w['what'] for w in smeared)} (not done until fixed)")
            if unfaithful:
                lines.append(f"  kept elements unlike the original: {'; '.join(k['what'] for k in unfaithful)} "
                             "(not done until fixed: check the hud box and times, plan, assemble)")
        except Exception as error:
            lines.append(f"{source_id}: follow-up failed: {type(error).__name__}: {error}")
        with lock:
            reports[source_id] = lines
    pool = ThreadPoolExecutor(2)
    futures = []

    def done(source_id, chain_results):
        if any(r.get("error") or r.get("held") or r.get("waiting") for r in chain_results):
            log(f"{source_id}: not complete (held or failed segments); no assembly yet")
            return
        log(f"{source_id}: all segments ready; assembling and reviewing while the rest renders")
        futures.append(pool.submit(follow_up, source_id))
    waits = RENDER.twin_waits(job, state, plan, only)
    with job_lock(job, "render"):
        results = RENDER.run_all(job, state, plan, style, only, workers=args.workers, gate=not args.no_gate,
                                 on_source_done=done, hold=set(waits))
    pool.shutdown(wait=True)
    lines, failed = render_lines(job, results)
    lines = wait_lines(waits) + lines[:-1]
    for source_id in plan["sources"]:
        lines += reports.get(source_id, [])
    lines.append(REVIEW_NEXT + "; held segments: look at their stills first (accept or redo); run again afterwards "
                 "(only what changed renders, unchanged videos are not reviewed again)")
    show(lines)
    return RULE_FAILURE if failed else 0


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
        keys = [s["id"] for s in row["segments"] if not s.get("live")]
        needed = ["anchor", *(s["id"] for s in row["segments"] if s["seam"] != "continuous" and not s.get("live"))]
        frames_ready = sum(1 for k in needed if (job / "keyframes" / f"{source_id}-{k}.png").is_file())
        rendered, held = 0, []
        for k in keys:
            record = job / "state/render" / f"{source_id}-{k}.json"
            value = read(record) if record.is_file() else {}
            rendered += value.get("status") == "done"
            gate = value.get("gate") or {}
            if gate.get("held") and not gate.get("accepted") and gate.get("generation_id") == value.get("generation_id"):
                held.append(k)
        assembled = (job / "state/assembled" / f"{source_id}.json").is_file()
        rounds_path = job / "state/review" / f"{source_id}.json"
        rounds = read(rounds_path)["rounds"] if rounds_path.is_file() else []
        last = f"round {rounds[-1]['round']} {rounds[-1]['verdict']} ({rounds[-1]['issues']} issues)" if rounds else "no review"
        lines.append(f"{source_id}: keyframes {frames_ready}/{len(needed)}, segments {rendered}/{len(keys)}"
                     + (f" (held: {', '.join(held)})" if held else "")
                     + f", assembled: {'yes' if assembled else 'no'}, {last}")
        if step is None:
            if frames_ready < len(needed):
                step = "keyframes"
            elif rendered < len(keys):
                step = "render"
            elif not assembled:
                step = "assemble"
            elif not rounds:
                step = "review"
    from reskin import ledger
    lines += ledger.markdown(ledger.summary(job))
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
    render.add_argument("--no-gate", action="store_true",
                        help="do not check segments before the next segment of their shot starts")
    assemble = command("assemble", cmd_assemble, help="retime, colour-match and join segments with the original audio")
    assemble.add_argument("--only", nargs="*", help="source IDs")
    compare = command("compare", cmd_compare, help="the delivered video beside its original (<name>_对比.mp4)")
    compare.add_argument("--only", nargs="*", help="source IDs")
    compare.add_argument("--with", dest="with_videos", nargs="+", metavar="VIDEO",
                         help="earlier versions on the delivered clock (e.g. a previous delivery), placed between the "
                              "original and the result; use with --only for one source")
    compare.add_argument("--labels", nargs="+", metavar="TEXT",
                         help="one caption per column: original, each --with video, result (e.g. 原版 旧写实版 新写实版)")
    compare.add_argument("--layout", choices=("left", "top"), default="left",
                         help="original on the left (default) or on top")
    review = command("review", cmd_review, help="Gemini review of each delivered video against its original")
    review.add_argument("--only", nargs="*", help="source IDs")
    review.add_argument("--fps", type=float, default=None,
                        help="frames per second Gemini samples (default 4 up to 75 s, else 2)")
    frames = command("frames", cmd_frames, help="stills of original and restyled around given times")
    frames.add_argument("--source", required=True)
    frames.add_argument("--at", type=float, nargs="+", required=True, help="seconds in the delivered video")
    frames.add_argument("--span", type=float, default=.3)
    redo = command("redo", cmd_redo, help="send confirmed broken segments back for another render")
    redo.add_argument("--segment", nargs="+", required=True, help="keys like S01:K02")
    redo.add_argument("--reason", required=True, help="what is wrong, as confirmed on the stills")
    redo.add_argument("--prompt", help="extra instruction added to the video prompt of these segments")
    redo.add_argument("--keyframe", action="store_true", help="the keyframe itself is wrong: make it again too")
    redo.add_argument("--replace-prompt", action="store_true",
                      help="drop this segment's earlier corrective sentences first, those taken from a like twin "
                           "included (one of them made things worse)")
    lint = command("lint", cmd_lint, help="Gemini lists what each source shows, checks the edits, drafts edits")
    lint.add_argument("--edits", type=Path, help="edits JSON to check")
    lint.add_argument("--only", nargs="*", help="source IDs")
    preview = command("preview", cmd_preview, help="GPT Image previews of a few moments of one source (looked at only)")
    preview.add_argument("--only", required=True, help="the source ID, e.g. S01")
    preview.add_argument("--at", nargs="+", type=float, required=True, metavar="T",
                         help="source times to preview (at most 4): moments the keyframes do not show")
    preview.add_argument("--redo", action="store_true", help="make these previews again even when current")
    preview.add_argument("--workers", type=int, default=4)
    run_cmd = command("run", cmd_run, help="keyframes, render with checks, assemble and review per source")
    run_cmd.add_argument("--only", nargs="*", help="source IDs or keys like S01:K02")
    run_cmd.add_argument("--workers", type=int, default=4, help="generations in flight at once")
    run_cmd.add_argument("--no-gate", action="store_true", help="no segment checks")
    accept = command("accept", cmd_accept, help="let a segment held by the segment check through")
    accept.add_argument("--segment", nargs="+", required=True, help="keys like S01:K02")
    accept.add_argument("--reason", required=True, help="why the reported flaw is not one (as seen on the stills)")
    cost = command("cost", cmd_cost, help="what was sent to Seedance, GPT Image and Gemini, and why")
    cost.add_argument("--json", action="store_true")
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
    except (ValueError, FileExistsError, FileNotFoundError) as error:      # includes common.Busy
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
