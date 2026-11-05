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
        "live_action": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "what": {"type": "STRING"}, "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}},
            "required": ["what", "start", "end"]}},
        "end_card_start": {"type": "NUMBER"},
        "edits_check": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "field": {"type": "STRING"}, "what": {"type": "STRING"},
            "verdict": {"type": "STRING", "enum": ["ok", "not_on_screen", "wrong_time", "wrong_colour", "ambiguous",
                                                    "missing"]},
            "suggestion": {"type": "STRING"}}, "required": ["field", "what", "verdict", "suggestion"]}}},
    "required": ["objects", "logos", "screen_text", "hud", "live_action", "end_card_start", "edits_check"]}


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
        "with times in seconds from the start. Write every `what` in English (it goes into the prompts).",
        f"Duration {catalog_row['duration']:.2f} s; camera shots: {shots}. Still regions found by the program "
        f"(boxes [x0, y0, x1, y1] as fractions of the frame): {overlays}.",
        "- objects: every prominent object, animal, character group and ground/water surface, each named as itself "
        "with its colour (e.g. yellow yellowfin tuna, green stacks of banknotes, yellow ropes, snow-covered ground). "
        "Use names that cannot be confused with "
        "another object (whole fish vs fish steaks, cash vs food). start/end = when it is on screen; always = on "
        "screen during the whole video. If an object changes colour, list each colour as its own item with its times.",
        "Logos, screen-fixed text and HUD are drawn by the image and video models from these descriptions, so each "
        "`what` of logos, screen_text and hud names its place on the screen, its exact text or digits in quotes "
        "(the original's language and characters, copied exactly), its colours, outline and plate, and its size "
        "(e.g. the game logo \"WHITEOUT SURVIVAL\" in white and gold letters in the top-right corner; one line of "
        "small grey text \"健康游戏忠告：抵制不良游戏\" along the bottom edge).",
        "- logos: game logos, watermarks and warning or disclaimer lines, with times and a box [x0, y0, x1, y1] "
        "(fractions). A logo that moves to another corner is two items.",
        "- screen_text: other text and flat UI: captions, labels, banners, buttons. fixed = true when it stays at one "
        "place on the screen while the scene moves (it does not move with the 3D scene); price tags standing on the "
        "floor, bubbles over characters or signs in perspective are fixed = false. Give the exact text and a box.",
        "- hud: screen-fixed counters with changing numbers (coins, timers, scores) with a box slightly larger than "
        "the element; `what` also says how the number changes over time (e.g. counts up from \"12,580\" at 0 s to "
        "\"15,200\" at 3 s, then stays). A counter whose numbers change many times is one item per stretch with its "
        "values.",
        "- live_action: every stretch that is not game footage: live-action video (real people, real cars, real "
        "landscapes or weather filmed with a camera), real photos, and real human faces pasted onto game objects; "
        "start/end at the hard cuts around it. The game itself, its rendered cutscenes and the end card are not "
        "live_action. An empty list when there is none.",
        "- end_card_start: the second where the closing card begins (the transition into it included: wipe, flash, "
        "blur, logo zoom); the duration if there is none.",
    ]
    if edits_row:
        lines += [
            "- edits_check: an agent wrote these edits for this video: "
            + json.dumps({k: edits_row.get(k) for k in ("keep", "materials", "remove", "ui", "live")
                          if k in edits_row},
                         ensure_ascii=False),
            "Check every `materials` and `remove` item: ok; not_on_screen (the video never shows it: naming it makes the "
            "model draw it); wrong_time (shown only part of the time but untimed, or the times are wrong: give the "
            "right ones); wrong_colour; ambiguous (another object could be turned into it). Add `missing` items for "
            "prominent objects whose colour should be named, screen-fixed logos, warning lines, text, buttons or HUD that "
            "are not in `ui` (the models draw them from it), `ui` items whose text, digits, place or times differ from "
            "the video (field ui, verdict wrong_time or ambiguous, with the right description), and "
            "live-action stretches that are not in `live` (or `live` items that show the game). "
            "`suggestion` (in English, as it would go into the prompt) says what to write instead.",
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
    row = {"keep": keep, "materials": [timed(o) for o in facts.get("objects") or [] if o.get("what")]}
    # Logos, warning lines, HUD and screen-fixed text and UI stay as in the original (the user's standing request);
    # the models draw them from these descriptions (nothing is copied back or burned in). `remove` is only for what
    # the user asks to delete.
    def ui_item(item, text=""):
        what = item["what"] if not text or f'"{text}"' in item["what"] else f'{item["what"]} reading "{text}"'
        entry = {"what": what, "start": round(float(item.get("start", 0)), 2),
                 "end": round(float(item.get("end", keep[1])), 2)}
        if (box := _box(item.get("box"))):
            entry["box"] = box
        if entry["start"] <= keep[0] + .3 and entry["end"] >= keep[1] - .3:
            del entry["start"], entry["end"]
        return entry
    ui = [ui_item(h) for h in [*(facts.get("hud") or []), *(facts.get("logos") or [])] if h.get("what")]
    ui += [ui_item(t, t.get("text", "")) for t in facts.get("screen_text") or [] if t.get("fixed") and t.get("what")]
    if ui:
        row["ui"] = ui
    live = [{"what": item["what"], "start": round(max(keep[0], float(item["start"])), 2),
             "end": round(min(keep[1], float(item["end"])), 2)}
            for item in facts.get("live_action") or [] if item.get("what")]
    live = [item for item in live if item["end"] - item["start"] >= .2]
    if live:
        row["live"] = live
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
        lines.append("live-action (not game, delivered from the original): " + ("; ".join(
            f"{o['what']} ({o['start']:g}-{o['end']:g} s)" for o in facts.get("live_action") or []) or "none"))
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
