"""Schema 2: a small, sequential frame-reference workflow. No schema-1 orchestration is used."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from copy import deepcopy
from pathlib import Path

from reskin import images, job as JOB, lumos, net, runtime, winky
from reskin.common import FFMPEG, ROOT, digest, read, run, sha, video_info, write

FPS = 24
MAX_FRAMES = 720


def obj(**fields):
    return {"type": "OBJECT", "properties": deepcopy(fields), "required": list(fields)}


def arr(item):
    return {"type": "ARRAY", "items": item}


NUM = {"type": "NUMBER"}
STR = {"type": "STRING"}
BOOL = {"type": "BOOLEAN"}
SPAN = obj(start=NUM, end=NUM)
SURVEY = obj(non_game=arr(SPAN), end_card_start=NUM,
             cuts=arr(obj(time=NUM, kind={"type": "STRING", "enum": ["motion", "camera", "hard_cut"]})),
             inventory=arr(obj(id=STR, name=STR, kind=STR, start=NUM, end=NUM, first_clear=NUM)))
FRAMES = obj(frames=arr(obj(at=NUM, covers=arr(STR))), references=arr(STR))
SELECT = obj(references=arr(STR))
COVER = obj(ok=BOOL, covers=arr(STR), note=STR)
REVIEW = obj(verdict={"type": "STRING", "enum": ["pass", "fail"]}, summary=STR,
             issues=arr(obj(start=NUM, end=NUM, description=STR)))
SURVEY['properties']['end_card_start']['description'] = 'Seconds; use the full video duration if there is no end card.'
SURVEY['properties']['inventory']['description'] = 'English content entries with unique IDs; all times are source seconds.'
FRAMES['properties']['references']['description'] = 'IDs of existing generated pictures needed by this segment; reuse rather than convert duplicates.'
COVER['properties']['covers']['description'] = 'Inventory IDs visibly converted to realistic materials in the result, not merely present in the source.'
REVIEW['properties']['issues']['description'] = 'Unresolved defects relative to source, in seconds from the comparison clip start; empty on pass.'


def prompt(kind):
    return read(ROOT / "assets/remake-prompts.json")[kind]


def ask(kind, schema, context, media=()):
    # All model-facing context must be English; user requests are never copied into prompts.
    encoded = json.dumps(context, ensure_ascii=False)
    if any(ord(c) > 127 for c in encoded):
        raise ValueError("model context must be English ASCII; correct the saved model response")
    answer = winky.gemini([*media, {"text": prompt(kind)}, {"text": encoded}], schema=schema)
    if not isinstance(answer, dict):
        raise ValueError("expected a structured model response")
    if any(ord(c) > 127 for c in json.dumps(answer, ensure_ascii=False)):
        raise ValueError("model response must use English")
    return answer


def ff(args, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    run([FFMPEG, "-v", "error", "-y", *args, partial])
    partial.replace(output)
    return output


def encode(filters):
    return ["-vf", filters, "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p"]


def still(source, frame, out):
    return ff(["-i", source, "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1"], out)


def load(job):
    state = JOB.load(job)
    if state.get("schema") != 2:
        raise ValueError("schema 1 job: continue with the old branch or init a new remake job")
    return state


def saved(job, name, default=None):
    path = job / (name + ".json")
    return read(path) if path.is_file() else ({} if default is None else default)


def put(job, name, value):
    write(job / (name + ".json"), value)
    return value


def working(job, sid):
    return job / "work" / f"{sid}-24.mp4"


def analyze(job, state, sid):
    source = next(s for s in state["sources"] if s["id"] == sid)
    out = working(job, sid)
    if not out.is_file():
        ff(["-i", source["path"], *encode("fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1")], out)
    info = video_info(out)
    return put(job, f"catalog/{sid}", info)


def survey(job, state, sid):
    from reskin.survey import proxy
    info = saved(job, f"catalog/{sid}") or analyze(job, state, sid)
    if saved(job, f"survey/{sid}"):
        return saved(job, f"survey/{sid}")
    path = proxy(working(job, sid), info, job / "work" / f"{sid}-survey.mp4")
    if path.stat().st_size > 256_000_000:
        raise ValueError("survey exceeds Gemini inline limit; use a shorter source")
    answer = ask("survey", SURVEY, {"duration": info["frames"] / FPS},
                 [winky.inline(path, fps=6)])
    return put(job, f"survey/{sid}", answer)


def split_run(a, b, candidates):
    """Minimum possible number of segments; prefer semantic candidates in the final seven seconds."""
    result = []
    while a < b:
        count = math.ceil((b - a) / MAX_FRAMES)
        if count == 1:
            end = b
        else:
            hi = min(a + MAX_FRAMES, b - (count - 1))
            lo = max(a + 1, b - (count - 1) * MAX_FRAMES, hi - 7 * FPS)
            options = [c for c in candidates if lo <= c <= hi]
            end = max(options) if options else hi
        result.append([a, end])
        a = end
    return result


def build_plan(info, survey):
    total = info["frames"]
    def frame(t):
        return min(total, max(0, round(float(t) * FPS)))
    originals = [[frame(x["start"]), frame(x["end"])] for x in survey["non_game"]]
    card = frame(survey["end_card_start"])
    if card < total:
        originals.append([card, total])
    merged = []
    for a, b in sorted(originals):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    cuts = [frame(x["time"]) for x in survey["cuts"]]
    hard = {frame(x["time"]) for x in survey["cuts"] if x["kind"] == "hard_cut"}
    segments, pos = [], 0
    for a, b in [*merged, [total, total]]:
        for start, end in split_run(pos, a, cuts):
            segments.append({"id": f"K{len(segments)+1:02d}", "frames": [start, end],
                             "continuous": bool(segments and segments[-1]["frames"][1] == start and start not in hard),
                             "transition_frames": 0})
        pos = b
    return {"total": total, "segments": segments, "originals": merged}


def record(job, sid, seg):
    return saved(job, f"state/{sid}-{seg['id']}")


def check_token(job, sid, plan, seg):
    i = plan["segments"].index(seg)
    previous = record(job, sid, plan["segments"][i-1]).get("generation_id") if i else None
    return digest([record(job, sid, seg).get("generation_id"), previous, seg["frames"], seg["transition_frames"]])


def approved(job, sid, plan, seg):
    r = record(job, sid, seg)
    return bool(r.get("generation_id") and r.get("approved") == check_token(job, sid, plan, seg))


def ready(job, sid, plan, seg):
    for prev in plan["segments"][:plan["segments"].index(seg)]:
        if not approved(job, sid, plan, prev):
            raise ValueError(f"waiting for {sid}:{prev['id']}: check and approve first")


def picture_issue(job, key, original, result, report):
    """Persist image-review evidence before stopping, so the agent can inspect the whole comparison."""
    from reskin.refs import strip
    sheet = job / 'review' / f'{key}-issue.png'
    sheet.parent.mkdir(parents=True,exist_ok=True)
    strip(report['note'],[Path(original)],Path(result)).save(sheet)
    put(job,f'review/{key}',{**report,'sheet':str(sheet)})
    return sheet


def refs(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    key = f"{sid}-{seg['id']}"
    existing = saved(job, f"references/{key}")
    predecessor = (record(job,sid,plan['segments'][plan['segments'].index(seg)-1]).get('generation_id')
                   if seg['continuous'] else None)
    if existing and saved(job,f"references/{key}-dependency",None).get('previous') == predecessor:
        return existing
    from reskin.refs import upload_copy
    survey = saved(job, f"survey/{sid}")
    a, b = seg["frames"]
    inventory = [x for x in survey["inventory"] if x["start"] < b/FPS and x["end"] > a/FPS]
    registry = saved(job, f"references/{sid}-registry", [])
    chosen = []
    if seg["continuous"]:
        prev = plan["segments"][plan["segments"].index(seg)-1]
        p = job / "references" / f"{key}-tail.png"
        previous = job / "ai" / f"{sid}-{prev['id']}.mp4"
        still(previous, video_info(previous)["frames"]-1, p)
        first = still(working(job, sid), a, job / "references" / f"{key}-first-source.png")
        # Only shot continuity decides tail use; material quality is reviewed elsewhere.
        tail_schema = obj(ok=BOOL, covers=arr(STR), note=STR)
        tail_schema['properties']['ok']['description'] = 'True only when Image 1 and Image 2 are consecutive views of the same continuous shot.'
        tail_schema['properties']['covers']['description'] = 'Inventory IDs visible in Image 1, regardless of their material style.'
        tail = ask("tail", tail_schema, {"operation": "same_shot_continuity", "inventory": inventory},
                   [winky.inline(upload_copy(p, 1536)), winky.inline(upload_copy(first, 1536))])
        put(job,f'review/{key}-tail',tail)
        if tail["ok"]:
            entry = {"id": key+"-tail", "path": str(p), "covers": [], "visible": tail["covers"], "at": a/FPS}
            chosen.append(entry)
    selection_path = f"references/{key}-frames"
    frame_selection = saved(job,selection_path)
    if not frame_selection:
        frame_selection = ask("frames", FRAMES, {"segment": [a/FPS, b/FPS], "inventory": inventory,
                 "existing": [{k:v for k,v in r.items() if k != "path"} for r in registry],
                 "covered_by_tail": chosen[0]["covers"] if chosen else []})
        put(job,selection_path,frame_selection)
    frames = list(frame_selection['frames'])
    if not chosen:
        frames.insert(0, {"at": a/FPS, "covers": []})
    seen = set()
    for spec in frames:
        at = min(b-1, max(a, round(spec["at"]*FPS)))
        if at in seen:
            continue
        seen.add(at)
        rid = f"{key}-f{at}"
        completed = next((r for r in registry if r['id']==rid and Path(r['path']).is_file()),None)
        if completed:
            chosen.append(completed)
            continue
        full = still(working(job, sid), at, job / "references" / f"{rid}-source.png")
        available = registry + [x for x in chosen if x["id"] not in {r["id"] for r in registry}]
        needed = {x['id'] for x in inventory}
        available = [r for r in available if needed.intersection(r['covers'])] or available[-1:]
        if available:
            select_schema = deepcopy(SELECT)
            select_schema['properties']['references'].update(minItems=1, maxItems=min(15, len(available)))
            select_schema['properties']['references']['items']['enum'] = [r['id'] for r in available]
            selection = ask("frames", select_schema, {"operation": "select_references", "at": at/FPS,
                            "available": [{k:v for k,v in r.items() if k != "path"} for r in available]},
                            [winky.inline(upload_copy(full, 1536)),
                             *[winky.inline(upload_copy(Path(r["path"]), 512)) for r in available]])
            ids = selection["references"]
            if not ids or len(ids) > 15 or len(set(ids)) != len(ids) or any(x not in {r["id"] for r in available} for x in ids):
                raise ValueError(f"invalid image reference selection (1-15 existing IDs required): {ids!r}; available: {[r['id'] for r in available]!r}")
            dependencies = [next(r for r in available if r["id"] == x) for x in ids]
            looks = [Path(r["path"]) for r in dependencies]
        else:
            dependencies = []
            looks = [ROOT / "assets/styles/realistic.jpg"]
        out = job / "references" / f"{rid}.png"
        task = saved(job, f"image-tasks/{rid}")
        if not task:
            uploaded = [lumos.upload(upload_copy(p), "image-to-image") for p in [full, *looks]]
            info = saved(job, f"catalog/{sid}")
            task = lumos.submit_image_to_image(uploaded, prompt("image"), model=images.IMAGE_MODEL,
                       aspect_ratio=images.aspect_for(info["width"], info["height"]), resolution="2K", quality="medium")
            put(job, f"image-tasks/{rid}", task)
        if not out.is_file():
            net.download(lumos.result_urls(lumos.wait(task["generation_id"]))[0], out)
        coverage = ask("frames", COVER, {"operation": "inspect_realistic_coverage", "inventory": inventory},
                       [winky.inline(upload_copy(full, 1536)), winky.inline(upload_copy(out, 1536))])
        if not coverage["ok"]:
            sheet = picture_issue(job,rid,full,out,coverage)
            raise ValueError(f"reference {rid} failed: {coverage['note']}; inspect {sheet}; run refs --redo --segment {sid}:{seg['id']}")
        entry = {"id": rid, "path": str(out), "at": at/FPS, "covers": coverage["covers"],
                 "dependencies": [r["id"] for r in dependencies], "generation_id": task["generation_id"]}
        chosen.append(entry)
        registry = [r for r in registry if r["id"] != rid] + [entry]
        put(job, f"references/{sid}-registry", registry)
    ids = frame_selection.get('references',[])
    if len(set(ids)) != len(ids) or any(x not in {r['id'] for r in registry} for x in ids):
        raise ValueError('invalid existing video reference IDs')
    chosen += [next(r for r in registry if r['id']==rid) for rid in ids if rid not in {r['id'] for r in chosen}]
    covered = {x for r in chosen for x in r["covers"]}
    missing = {x["id"] for x in inventory} - covered
    if missing or not chosen or len(chosen) > 30:
        raise ValueError(f"reference coverage incomplete or over 30 images: {sorted(missing)}")
    put(job,f"references/{key}-dependency",{'previous':predecessor})
    return put(job, f"references/{key}", chosen)


def render(job, sid, plan, seg):
    ready(job, sid, plan, seg)
    r = record(job, sid, seg)
    if r.get('frames') and r['frames'] != seg['frames']:
        raise ValueError('segment frames changed: explicitly redo this segment')
    if r.get("status") == "done":
        return r
    references = saved(job, f"references/{sid}-{seg['id']}", [])
    if not references:
        raise ValueError("run refs first")
    from reskin.refs import upload_copy
    a,b = seg["frames"]
    clip = job / "segments" / f"{sid}-{seg['id']}.mp4"
    if not clip.is_file():
        ff(["-ss", str(a/FPS), "-i", working(job, sid),
            *encode(f"fps=24,trim=end_frame={b-a},tpad=stop_mode=clone:stop_duration=2"),
            "-frames:v", str(max(48,b-a))], clip)
    if clip.stat().st_size > 190_000_000:
        raise ValueError("reference clip exceeds upload limit")
    if not r.get("generation_id"):
        uses_tail = references[0]["id"] == f"{sid}-{seg['id']}-tail"
        text = prompt("video") + (" " + prompt("continuation") if uses_tail else "")
        started = lumos.submit_video_edit(lumos.upload(clip, "video-to-video"),
                   [lumos.upload(upload_copy(Path(x["path"])), "video-to-video") for x in references], text)
        r.update(generation_id=started["generation_id"], status="submitted", prompt=text, frames=seg['frames'])
        put(job, f"state/{sid}-{seg['id']}", r)
    result = lumos.wait(r["generation_id"])
    raw = job / "raw" / f"{sid}-{seg['id']}-{r['generation_id']}.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.is_file():
        net.download(lumos.result_urls(result)[0], raw)
    info = video_info(raw)
    # Normalize generated duration once, without motion warping; trim short-clip padding.
    rendered = max(48,b-a)
    ff(["-i", raw, *encode(f"setpts=PTS*{rendered/FPS/info['duration']:.10f},fps=24,tpad=stop_mode=clone:stop_duration=1"),
        "-frames:v", str(b-a)], job / "ai" / f"{sid}-{seg['id']}.mp4")
    r.update(status="done")
    return put(job, f"state/{sid}-{seg['id']}", r)


def pieces(plan, upto=None):
    rows = [{"frames": x, "original": True} for x in plan["originals"]]
    rows += [{**s, "original": False} for s in plan["segments"]]
    end = next(s["frames"][1] for s in plan["segments"] if s["id"] == upto) if upto else plan["total"]
    return [r for r in sorted(rows, key=lambda r:r["frames"][0]) if r["frames"][0] < end]


def assemble(job, state, sid, plan, upto=None):
    from reskin.assemble import target_size
    info = saved(job, f"catalog/{sid}")
    w,h = target_size(info["width"], info["height"])
    clips = []
    for row in pieces(plan, upto):
        a,b = row["frames"]
        source = working(job,sid) if row["original"] else job / "ai" / f"{sid}-{row['id']}.mp4"
        out = job / "work/join" / f"{sid}-{a}.mp4"
        transition = int(row.get("transition_frames",0))
        if transition and clips:
            tail = still(clips[-1], video_info(clips[-1])["frames"]-1, out.with_suffix(".png"))
            graph = (f"[0:v]scale={w}:{h},setsar=1[v];[1:v]scale={w}:{h},setsar=1[t];"
                     f"[v][t]blend=all_expr='A*min(1,N/{transition})+B*(1-min(1,N/{transition}))'[o]")
            ff(["-i", source, "-loop", "1", "-i", tail, "-filter_complex", graph, "-map", "[o]",
                "-an", "-frames:v", str(b-a), "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p"], out)
        else:
            ff(["-ss", str(a/FPS if row["original"] else 0), "-i", source,
                *encode(f"scale={w}:{h},setsar=1,fps=24"), "-frames:v", str(b-a)],out)
        clips.append(out)
    manifest = job / "work" / f"{sid}-concat.txt"
    manifest.write_text("".join("file '" + str(p.resolve()).replace("\\","/").replace("'", "'\\''") + "'\n" for p in clips), encoding="utf-8")
    original = next(s for s in state["sources"] if s["id"] == sid)
    output = job / "work" / f"{sid}-preview.mp4" if upto else Path(state["delivery_dir"]) / f"{original['name']}_{sid}_realistic.mp4"
    args = ["-f","concat","-safe","0","-i",manifest]
    if not upto and video_info(original["path"])["audio"]:
        args += ["-i",original["path"],"-map","0:v:0","-map","1:a:0","-c:a","aac","-af","apad"]
    count = pieces(plan,upto)[-1]["frames"][1]
    ff([*args,"-c:v","copy","-t",str(count/FPS),"-movflags","+faststart"],output)
    made = video_info(output)
    if abs(made["duration"]-count/FPS) > 1/FPS + .001:
        raise ValueError("assembled duration differs by more than one frame")
    return output


def review_clip(job, sid, start, end, result, result_start, kind, folder, context):
    from reskin.review import grid_clip
    info = saved(job, f"catalog/{sid}")
    compare = grid_clip([{"path":working(job,sid),"start":start,"end":end,"label":"ORIGINAL"},
                         {"path":result,"start":result_start,"end":result_start+end-start,"label":"RESULT"}],
                        (info["width"], info["height"]), folder / f"{kind}.mp4")
    answer = ask(kind, REVIEW, context, [winky.inline(compare,fps=6)])
    if any(not 0 <= x["start"] <= x["end"] <= end-start+1/FPS for x in answer["issues"]):
        raise ValueError("review issue outside the clip")
    if answer["verdict"] == "pass" and answer["issues"]:
        raise ValueError("inconsistent review: pass with issues")
    from reskin.review import issue_moments, three_frames
    for index, issue in enumerate(answer['issues'],1):
        sheet = folder / f'{kind}-issue-{index}.png'
        three_frames([(str(working(job,sid)),lambda t:t+start,'ORIGINAL'),
                      (str(result),lambda t:t+result_start,'RESULT')],
                     issue_moments(issue['start'],issue['end'],max(0,end-start-1/FPS)),
                     (info['width'],info['height']),sheet)
        issue['sheet'] = str(sheet)
    return answer


def check(job,state,sid,plan,seg):
    ready(job,sid,plan,seg)
    r = record(job,sid,seg)
    if r.get("status") != "done":
        raise ValueError("render first")
    folder = job / "review" / f"{sid}-{seg['id']}-{r['generation_id']}"
    folder.mkdir(parents=True,exist_ok=True)
    a,b = seg["frames"]
    answer = {"token":check_token(job,sid,plan,seg),"seam":None,"segment":None,"verdict":"fail"}
    if a:
        cached = saved(job,f"checks/{sid}-{seg['id']}")
        if cached.get('token') == answer['token'] and cached.get('seam'):
            answer = cached
            if not answer.get('decision'):
                return answer
        else:
            answer = check_seam(job,state,sid,plan,seg,folder,answer)
            return put(job,f"checks/{sid}-{seg['id']}",answer)
    answer["token"] = check_token(job,sid,plan,seg)
    result = job / "ai" / f"{sid}-{seg['id']}.mp4"
    answer["segment"] = review_clip(job,sid,a/FPS,b/FPS,result,0,"segment",folder,{})
    answer["verdict"] = answer["segment"]["verdict"]
    answer['status'] = 'complete'
    return put(job,f"checks/{sid}-{seg['id']}",answer)


def check_seam(job,state,sid,plan,seg,folder,answer):
    a,b = seg['frames']
    # Judge whether a transition is needed on the untreated join, including on repeated checks.
    preview_plan = deepcopy(plan)
    preview_plan['segments'][plan['segments'].index(seg)]['transition_frames'] = 0
    preview = assemble(job,state,sid,preview_plan,seg["id"])
    start,end = max(0,a/FPS-1),min(b/FPS,a/FPS+1)
    seam = review_clip(job,sid,start,end,preview,start,"seam",folder,
                       {"join":a/FPS-start,"continuous":seg["continuous"],"transition_frames":0})
    answer["seam"] = seam
    answer['status'] = 'awaiting_agent_seam_decision'
    return answer

def choose_seam(job,sid,plan,seg,frames,reason):
    ready(job,sid,plan,seg)
    c = saved(job,f"checks/{sid}-{seg['id']}")
    if c.get('token') != check_token(job,sid,plan,seg) or not c.get('seam'):
        raise ValueError('check the current seam first')
    if not 0 <= frames <= 6 or (frames and not seg['continuous']) or not reason.strip():
        raise ValueError('use 0-6 frames for continuous seams, 0 for cuts; explain the visual inspection')
    seg['transition_frames'] = frames
    put(job,f'plans/{sid}',plan)
    c.update(token=check_token(job,sid,plan,seg),segment=None,verdict='fail',status='awaiting_segment_review',
             decision={'transition_frames':frames,'reason':reason})
    return put(job,f"checks/{sid}-{seg['id']}",c)


def approve(job,sid,plan,seg):
    ready(job,sid,plan,seg)
    c = saved(job,f"checks/{sid}-{seg['id']}")
    token = check_token(job,sid,plan,seg)
    if c.get("token") != token or c.get("verdict") != "pass":
        raise ValueError("approval requires a passed check of the current generation and seam")
    r = record(job,sid,seg)
    r["approved"] = token
    return put(job,f"state/{sid}-{seg['id']}",r)


def redo(job,sid,plan,seg,reason):
    r = record(job,sid,seg)
    if not r.get("generation_id"):
        raise ValueError("no generation to redo")
    if r.get("retries",0) >= 5:
        raise ValueError("five retries exhausted: this video is stopped")
    history = r.get("history",[]) + [{k:v for k,v in r.items() if k != "history"}]
    seg['transition_frames'] = 0
    put(job,f'plans/{sid}',plan)
    return put(job,f"state/{sid}-{seg['id']}",{"retries":r.get("retries",0)+1,"history":history,"reason":reason})


def reset_refs(job,sid,seg):
    key=f"{sid}-{seg['id']}"
    registry=saved(job,f'references/{sid}-registry',[])
    for task in (job/'image-tasks').glob(key+'-f*.json'):
        output=job/'references'/(task.stem+'.png')
        if output.is_file():
            archive=job/'reference-history'/f'{task.stem}-{sha(output)[:12]}.png'
            archive.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(output,archive)
            output.unlink()
        task.unlink()
    for suffix in ('','-frames','-dependency'):
        (job/'references'/(key+suffix+'.json')).unlink(missing_ok=True)
    put(job,f'references/{sid}-registry',[r for r in registry if not r['id'].startswith(key+'-f')])


def main():
    for stream in (sys.stdout,sys.stderr):
        stream.reconfigure(encoding="utf-8",errors="replace")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["auth","login","runtime","init","analyze","survey","plan","refs","render","check","seam","approve","redo","assemble","status"])
    p.add_argument("--job-dir",type=Path)
    p.add_argument("--work-order",type=Path)
    p.add_argument("--segment")
    p.add_argument("--only",nargs="*")
    p.add_argument("--reason",default="quality review failed")
    p.add_argument('--transition-frames',type=int,default=0)
    p.add_argument("--redo",action="store_true",help="explicitly remake this segment's reference images")
    args,extra = p.parse_known_args()
    if args.command in ("auth","login","runtime"):
        from reskin.cli import main as legacy
        return legacy()
    try:
        if extra:
            raise ValueError(f"unsupported remake arguments: {extra}")
        if args.command == "init":
            if not args.work_order:
                raise ValueError("--work-order is required")
            order = json.loads(sys.stdin.buffer.read().decode("utf-8-sig")) if str(args.work_order)=="-" else read(args.work_order)
            if order.get("style","realistic") != "realistic":
                raise ValueError("remake uses the realistic preset")
            order["options"] = {**order.get("options",{}),"fps":24,"max_segment_seconds":30}
            job,state = JOB.init(order,args.job_dir)
            state["schema"] = 2
            write(job / "job.json",state)
            print(f"job_dir: {job}")
            return
        if args.command in ('approve','redo','seam') and not args.segment:
            raise ValueError('--segment is required for approve and redo')
        if not args.job_dir:
            raise ValueError("--job-dir is required")
        runtime.ensure("media")
        job = args.job_dir.resolve()
        state = load(job)
        errors = []
        selected = args.only or []
        for source in state["sources"]:
            sid = source["id"]
            if selected and not any(x == sid or x.startswith(sid+":") for x in selected):
                continue
            if args.segment and not args.segment.startswith(sid+":"):
                continue
            try:
                if args.command == "analyze":
                    result = analyze(job,state,sid)
                elif args.command == "survey":
                    result = survey(job,state,sid)
                elif args.command == "plan":
                    result = put(job,f"plans/{sid}",build_plan(saved(job,f"catalog/{sid}"),saved(job,f"survey/{sid}")))
                else:
                    plan = saved(job,f"plans/{sid}")
                    if not plan:
                        raise ValueError("run plan first")
                    if args.command == "assemble":
                        if not all(approved(job,sid,plan,s) for s in plan["segments"]):
                            raise ValueError("all segments must be approved before delivery")
                        result = str(assemble(job,state,sid,plan))
                    elif args.command == "status":
                        result = [{"segment":s["id"],"approved":approved(job,sid,plan,s),**record(job,sid,s)} for s in plan["segments"]]
                    else:
                        wanted = args.segment or next((x for x in selected if x.startswith(sid+":")),None)
                        seg = next((s for s in plan["segments"] if (sid+":"+s["id"]==wanted if wanted else not approved(job,sid,plan,s))),None)
                        if seg is None:
                            if wanted:
                                raise ValueError(f"unknown segment {wanted}")
                            result = "all segments approved"
                        else:
                            if args.redo:
                                if args.command != 'refs':
                                    raise ValueError('--redo is only for refs')
                                ready(job,sid,plan,seg)
                                reset_refs(job,sid,seg)
                            if args.command == 'seam':
                                result = choose_seam(job,sid,plan,seg,args.transition_frames,args.reason)
                            else:
                                function = {"refs":refs,"render":render,"check":check,"approve":approve,"redo":redo}[args.command]
                                result = function(job,state,sid,plan,seg) if args.command=="check" else function(job,sid,plan,seg,args.reason) if args.command=="redo" else function(job,sid,plan,seg)
                print(json.dumps({"source":sid,"result":result},ensure_ascii=False))
            except (ValueError,RuntimeError,OSError,KeyError) as e:
                errors.append(f"{sid}: {e}")
        if errors:
            raise ValueError("; ".join(errors))
    except Exception as e:
        print(f"error: {e}",file=sys.stderr)
        raise SystemExit(2)
