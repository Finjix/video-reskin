"""Before anything is paid for: Gemini watches each source once and lists what is on screen (prominent objects with
their colours and times, logos and warning lines, screen-fixed text and UI, HUD counters, where the end card starts),
checks the edits against it, and drafts edits from it.

Most redos so far came from the edits, not from chance: a material the video does not show (`三文鱼` turned tuna into
salmon steaks), an object listed without its time (a truck drawn into the opening), a warning line named where none is
shown (Seedance drew garbled text there), a colour left unnamed (a yellow cable drawn red). Such a mistake repeats in
every segment and every attempt, so it is cheapest to catch here.
"""
from __future__ import annotations

import json
from pathlib import Path

from reskin import ledger, winky
from reskin.analyze import sheet
from reskin.common import FFMPEG, log, read, run, video_info, write, write_text

BOX = {"type": "ARRAY", "items": {"type": "NUMBER"}}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "objects": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "what": {"type": "STRING"}, "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
            "always": {"type": "BOOLEAN"}}, "required": ["what", "start", "end", "always"]}},
        "logos": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "what": {"type": "STRING"}, "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "box": BOX},
            "required": ["what", "start", "end", "box"]}},
        "screen_text": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "what": {"type": "STRING"}, "text": {"type": "STRING"}, "start": {"type": "NUMBER"},
            "end": {"type": "NUMBER"}, "box": BOX, "fixed": {"type": "BOOLEAN"}},
            "required": ["what", "text", "start", "end", "box", "fixed"]}},
        "hud": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "what": {"type": "STRING"}, "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "box": BOX},
            "required": ["what", "start", "end", "box"]}},
        "end_card_start": {"type": "NUMBER"},
        "edits_check": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "field": {"type": "STRING"}, "what": {"type": "STRING"},
            "verdict": {"type": "STRING", "enum": ["ok", "not_on_screen", "wrong_time", "wrong_colour", "ambiguous",
                                                    "missing"]},
            "suggestion": {"type": "STRING"}}, "required": ["field", "what", "verdict", "suggestion"]}}},
    "required": ["objects", "logos", "screen_text", "hud", "end_card_start", "edits_check"]}


def proxy(source: Path, output: Path) -> Path:
    info = video_info(source)
    portrait = info["height"] >= info["width"]
    scale = "480:-2" if portrait else "854:-2"
    output.parent.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-i", source, "-vf", f"fps=10,scale={scale}:flags=area", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", output])
    return output


def prompt(catalog_row: dict, edits_row: dict | None) -> str:
    shots = ", ".join(f"{s['start']:.2f}-{s['end']:.2f}" for s in catalog_row["shots"])
    overlays = "; ".join(f"{o['id']} {o['box']} {o['start']:.1f}-{o['end']:.1f} s" for o in catalog_row.get("overlays") or []) or "none"
    lines = [
        "You prepare an AI restyle of a game advertising video: every cartoon material becomes a realistic real-world "
        "material while composition, objects, counts, colours and timing stay. List what the video shows, precisely, "
        "with times in seconds from the start. Write every `what` in Chinese.",
        f"Duration {catalog_row['duration']:.2f} s; camera shots: {shots}. Still regions found by the program "
        f"(boxes [x0, y0, x1, y1] as fractions of the frame): {overlays}.",
        "- objects: every prominent object, animal, character group and ground/water surface, each named as itself "
        "with its colour (e.g. 黄色的黄鳍金枪鱼, 绿色的纸币堆, 黄色的缆绳, 积雪地面). Use names that cannot be confused with "
        "another object (whole fish vs fish steaks, cash vs food). start/end = when it is on screen; always = on "
        "screen during the whole video. If an object changes colour, list each colour as its own item with its times.",
        "- logos: game logos, watermarks and warning or disclaimer lines, each `what` naming the element and its text "
        "(e.g. 右上角的游戏logo「WHITEOUT SURVIVAL」, 画面底部的白色小字警示语), with times and a box [x0, y0, x1, y1] "
        "(fractions). A logo that moves to another corner is two items.",
        "- screen_text: other text and flat UI: captions, labels, banners, buttons. fixed = true when it stays at one "
        "place on the screen while the scene moves (it does not move with the 3D scene); price tags standing on the "
        "floor, bubbles over characters or signs in perspective are fixed = false. Give the exact text and a box.",
        "- hud: screen-fixed counters with changing numbers (coins, timers, scores) with a box slightly larger than "
        "the element.",
        "- end_card_start: the second where the closing card begins (the transition into it included: wipe, flash, "
        "blur, logo zoom); the duration if there is none.",
    ]
    if edits_row:
        lines += [
            "- edits_check: an agent wrote these edits for this video: "
            + json.dumps({k: edits_row.get(k) for k in ("keep", "materials", "remove", "hud", "overlays") if k in edits_row},
                         ensure_ascii=False),
            "Check every `materials` and `remove` item: ok; not_on_screen (the video never shows it: naming it makes the "
            "model draw it); wrong_time (shown only part of the time but untimed, or the times are wrong: give the "
            "right ones); wrong_colour; ambiguous (another object could be turned into it). Add `missing` items for "
            "prominent objects whose colour should be named, and logos or warning lines that are not in `remove`. "
            "`suggestion` (Chinese) says what to write instead.",
        ]
    else:
        lines.append("- edits_check: an empty list.")
    return "\n".join(lines)


def _box(value) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    if max(x0, y0, x1, y1) > 1.5:       # answered in 0-1000
        x0, y0, x1, y1 = (v / 1000 for v in (x0, y0, x1, y1))
    x0, x1 = sorted((max(0.0, x0), min(1.0, x1)))
    y0, y1 = sorted((max(0.0, y0), min(1.0, y1)))
    return [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)] if x1 > x0 and y1 > y0 else None


def draft(facts: dict, catalog_row: dict) -> dict:
    """Edits drafted from the facts: the agent checks every item on the sheets before using it."""
    duration = catalog_row["duration"]
    end = float(facts.get("end_card_start") or duration)
    card = catalog_row.get("end_card")
    if not 4 < end <= duration:
        end = card["start"] if card else duration
    keep = [0.0, round(min(end, duration), 2)]

    def timed(item):
        start, stop = round(float(item.get("start", 0)), 2), round(float(item.get("end", keep[1])), 2)
        if item.get("always") or (start <= keep[0] + .3 and stop >= keep[1] - .3):
            return item["what"]
        return {"what": item["what"], "start": start, "end": stop}
    row = {"keep": keep, "materials": [timed(o) for o in facts.get("objects") or [] if o.get("what")],
           "remove": [timed(o) for o in facts.get("logos") or [] if o.get("what")]}
    hud = [{"box": box, "what": h["what"], "start": round(float(h["start"]), 2), "end": round(float(h["end"]), 2)}
           for h in facts.get("hud") or [] if (box := _box(h.get("box")))
           and (box[2] - box[0]) * (box[3] - box[1]) <= .15]
    if hud:
        row["hud"] = hud
    overlays = [{"what": t["what"], "box": box, "start": round(float(t["start"]), 2), "end": round(float(t["end"]), 2)}
                for t in facts.get("screen_text") or [] if t.get("fixed") and (box := _box(t.get("box")))]
    if overlays:
        row["overlays"] = overlays
    return row


def lint_source(job: Path, state: dict, catalog_row: dict, edits_row: dict | None) -> dict:
    source = next(r for r in state["sources"] if r["id"] == catalog_row["id"])
    clip = proxy(Path(source["path"]), job / "catalog/lint" / f"{catalog_row['id']}.mp4")
    log(f"{catalog_row['id']}: asking Gemini what is on screen" + (" and checking the edits" if edits_row else ""))
    part = winky.inline(clip, "video/mp4")
    part["videoMetadata"] = {"fps": 2}
    facts = winky.gemini([part, {"text": prompt(catalog_row, edits_row)}], schema=SCHEMA)
    ledger.add(job, kind="gemini", purpose="lint", key=catalog_row["id"], seconds=catalog_row["duration"])
    facts["draft"] = draft(facts, catalog_row)
    write(job / "catalog/lint" / f"{catalog_row['id']}.json", facts)
    boxes = ([{"id": f"L{n}", "box": b, "start": float(item["start"]), "end": float(item["end"])}
              for n, item in enumerate(facts.get("logos") or [], 1) if (b := _box(item.get("box")))]
             + [{"id": f"T{n}", "box": b, "start": float(item["start"]), "end": float(item["end"])}
                for n, item in enumerate(facts.get("screen_text") or [], 1) if (b := _box(item.get("box")))]
             + [{"id": f"H{n}", "box": b, "start": float(item["start"]), "end": float(item["end"])}
                for n, item in enumerate(facts.get("hud") or [], 1) if (b := _box(item.get("box")))])
    info = catalog_row["info"]
    step = 2.0 if catalog_row["duration"] > 40 else 1.0
    times = [round(t, 2) for t in [x * step + .25 for x in range(int((catalog_row["duration"] - .3) / step) + 1)]]
    portrait = info["height"] >= info["width"]
    facts["sheet"] = str(sheet(Path(source["path"]), info, times, job / "catalog" / f"{catalog_row['id']}-lint.jpg",
                               columns=10 if portrait else 6, thumb=192 if portrait else 300, boxes=boxes,
                               title=f"{catalog_row['id']} Gemini boxes: L logo/warning, T screen text, H HUD"))
    write(job / "catalog/lint" / f"{catalog_row['id']}.json", facts)
    return facts


def markdown(rows: list[tuple[str, dict]]) -> str:
    lines = ["# Lint", "", "What Gemini saw in each source, its check of the edits, and drafted edits "
             "(`config/edits.draft.json`). Gemini is a second pair of eyes: confirm items on the sheets.", ""]
    for source_id, facts in rows:
        lines.append(f"## {source_id}")
        lines.append(f"end card from {facts.get('end_card_start')} s")
        lines.append("objects: " + "; ".join(f"{o['what']} ({o['start']:g}-{o['end']:g} s)" for o in facts.get("objects") or []))
        lines.append("logos/warnings: " + ("; ".join(f"{o['what']} ({o['start']:g}-{o['end']:g} s)" for o in facts.get("logos") or []) or "none"))
        lines.append("screen text: " + ("; ".join(f"{o['what']} 「{o.get('text', '')}」 ({'fixed' if o.get('fixed') else 'in scene'}, "
                                               f"{o['start']:g}-{o['end']:g} s)" for o in facts.get("screen_text") or []) or "none"))
        lines.append("hud: " + ("; ".join(f"{o['what']} {o.get('box')}" for o in facts.get("hud") or []) or "none"))
        problems = [c for c in facts.get("edits_check") or [] if c.get("verdict") != "ok"]
        if facts.get("edits_check"):
            lines.append("edits check: " + ("all ok" if not problems else ""))
            lines += [f"- [{c['verdict']}] {c['field']}: {c['what']} -> {c['suggestion']}" for c in problems]
        lines.append(f"sheet: {Path(facts.get('sheet', '')).name}")
        lines.append("")
    return "\n".join(lines)


def run_all(job: Path, state: dict, catalog: dict, edits: dict | None, only: set[str] | None = None) -> list[tuple[str, dict]]:
    from concurrent.futures import ThreadPoolExecutor
    rows = [r for r in catalog["sources"] if not only or r["id"] in only]

    def one(row):
        edits_row = None
        if edits:
            from reskin.plan import source_edits
            from reskin import job as JOB
            if row["id"] in (edits.get("sources") or {}) or edits.get("defaults"):
                edits_row = source_edits(edits, row["id"], row, JOB.style(state))
        try:
            return row["id"], lint_source(job, state, row, edits_row)
        except Exception as error:
            return row["id"], {"error": f"{type(error).__name__}: {error}"}
    with ThreadPoolExecutor(min(4, max(1, len(rows)))) as executor:
        results = list(executor.map(one, rows))
    good = [(sid, facts) for sid, facts in results if "error" not in facts]
    draft_path = job / "config/edits.draft.json"
    drafted = read(draft_path) if draft_path.is_file() else {"defaults": {}, "sources": {}}
    for source_id, facts in good:
        drafted["sources"][source_id] = facts["draft"]
    write(draft_path, drafted)
    write_text(job / "catalog/lint.md", markdown(good))
    return results
