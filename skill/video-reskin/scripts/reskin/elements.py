"""Native-frame element edits, review and delivery for independent variants of a source."""
from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from bisect import bisect_left
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from reskin import element_seedance, elements_review as review, runtime
from reskin.common import FFMPEG, FFPROBE, digest, probe, read, run, sha, video_info, write


def integers(row, count):
    return isinstance(row, list) and len(row) == count and all(type(n) is int for n in row)


BANKNOTES = re.compile(r"钞票|鈔票|纸币|紙幣|现钞|現鈔|现金|現金|纸钞|紙鈔|钱捆|钱堆|钱叠|"
                       r"\bbank[ -]?notes?\b|\b(?:dollar|paper|green) bills?\b|^bills?$|"
                       r"\bcash\b(?![- ]register)|\bpaper (?:currency|money)\b|"
                       r"\b(?:stack|bundle|pile)s? of (?:green )?money\b", re.I)
FROZEN = re.compile(r"冰冻|冰封|冰壳|覆冰|结冰|凍結|冻结|霜冻|(?:冻|凍)(?:木|肉|鱼|魚|鸡|雞|虾|蝦|牛|羊|猪|豬)|"
                    r"\bfrozen\b|\bice[- ](?:encased|covered|crusted|coated|wrapped)|\bfrost[- ]covered", re.I)


def protect_banknotes(targets, label):
    for target in targets:
        name = unicodedata.normalize("NFKC", target["element"]).strip()
        name = re.split(r"\s+(?:next to|beside|near)\s+", name, maxsplit=1, flags=re.I)[0]
        name = re.split(r"(?:旁边|旁邊|旁|周围|周圍)的", name)[-1]
        if BANKNOTES.search(name):
            raise ValueError(f"{label}: banknotes are protected; select a different target")


def target_selection(selection, label):
    if isinstance(selection, str) and selection.strip():
        return {"reason": selection.strip()}
    if not isinstance(selection, dict):
        raise ValueError(f"{label}: target_selection takes a reason or an object with optional named frames")
    moments = selection.get("frames", {})
    if not isinstance(moments, dict):
        raise ValueError(f"{label}: target_selection.frames maps names to source frame numbers")
    for name, value in moments.items():
        points = value if isinstance(value, list) else [value]
        if not isinstance(name, str) or not name.strip() or any(type(n) is not int or n < 0 for n in points):
            raise ValueError(f"{label}: each named selection frame must be a nonnegative frame number")
    return selection


def target_spec(spec, default_ranges, label):
    if not isinstance(spec, dict):
        raise ValueError(f"{label}: expected a target object")
    result = {}
    for field in ("element", "replacement"):
        value = spec.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}: describe the {field}")
        result[field] = value.strip()
    if "replacement_key" in spec:
        key = spec["replacement_key"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label}: replacement_key must be a nonempty shared name for this appearance")
        result["replacement_key"] = key.strip()
    ranges = spec.get("ranges", default_ranges)
    if not isinstance(ranges, list) or not ranges:
        raise ValueError(f"{label}: ranges must list the target's visible source frame intervals")
    end = 0
    for interval in ranges:
        if not integers(interval, 2) or not end <= interval[0] < interval[1]:
            raise ValueError(f"{label}: ranges must be ordered disjoint [start, end) frame intervals")
        end = interval[1]
    result["ranges"] = ranges
    if "target_selection" in spec:
        result["target_selection"] = target_selection(spec["target_selection"], label)
    return result


def combined_ranges(targets):
    result = []
    for start, end in sorted(span for target in targets for span in target["ranges"]):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def distinct_replacements(rows):
    owners = {}
    frozen_only = []
    for row in rows:
        targets = row.get("targets", [row])
        protect_banknotes(targets, row["id"])
        frozen_only.append(all(FROZEN.search(t["replacement"] + " " + t.get("replacement_key", "")) for t in targets))
        for target in targets:
            for label in (target["replacement"], target.get("replacement_key", target["replacement"])):
                identity = "".join(unicodedata.normalize("NFKC", label).casefold().split())
                owner = owners.setdefault(identity, row["id"])
                if owner != row["id"]:
                    raise ValueError(f"{row['id']}: replacement {label!r} repeats {owner}; choose a distinct result")
    if len(rows) > 1 and all(frozen_only):
        raise ValueError("include a non-frozen replacement design in a batch of variants")


def variants_from_order(order, sources, delivery, job=None):
    requested = order.get("variants")
    if not isinstance(requested, list) or not requested:
        raise ValueError("elements requires one or more variants")
    source_ids = {s["id"] for s in sources}
    protected = {Path(s["path"]).resolve() for s in sources}
    seen_names, rows = set(), []
    clocks = {}
    surveys = order.get("surveys", {})
    if not isinstance(surveys, dict) or set(surveys) - source_ids:
        raise ValueError("surveys maps source IDs to their gameplay survey")
    for index, variant in enumerate(requested, 1):
        vid = f"V{index:02d}"
        if not isinstance(variant, dict):
            raise ValueError(f"{vid}: expected a variant object")
        name = variant.get("name", "")
        if (not isinstance(name, str) or not name.strip() or name != name.strip() or name.endswith(".")
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or PureWindowsPath(name).is_reserved()):
            raise ValueError(f"{vid}: give a filename without a directory or reserved characters")
        name = name[:-4] if name.lower().endswith(".mp4") else name
        if not name or name.casefold() in seen_names:
            raise ValueError(f"{vid}: delivery names must be nonempty and unique")
        seen_names.add(name.casefold())
        sid = variant.get("source", sources[0]["id"] if len(sources) == 1 else None)
        if sid not in source_ids:
            raise ValueError(f"{vid}: specify a source ID from this work order")
        row = {"id": vid, "source": sid, "name": name}
        if "targets" in variant:
            entries = variant["targets"]
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{vid}: targets must contain one or more target objects")
            targets = [target_spec(t, variant.get("ranges"), f"{vid} target {i}") for i, t in enumerate(entries, 1)]
            row.update(targets=targets, ranges=combined_ranges(targets),
                       element=" + ".join(t["element"] for t in targets),
                       replacement=" + ".join(t["replacement"] for t in targets))
            if "target_selection" in variant:
                row["target_selection"] = target_selection(variant["target_selection"], vid)
        else:
            row.update(target_spec(variant, None, vid))
        frames = variant.get("review_frames", [])
        if not isinstance(frames, list) or any(type(n) is not int or n < 0 for n in frames):
            raise ValueError(f"{vid}: review_frames must contain source frame numbers")
        row["review_frames"] = frames
        motion = variant.get("motion_review", {})
        if not isinstance(motion, dict):
            raise ValueError(f"{vid}: motion_review maps action names to [start, end) source frame intervals")
        for label, interval in motion.items():
            if (not isinstance(label, str) or not label.strip() or not integers(interval, 2)
                    or not 0 <= interval[0] < interval[1]):
                raise ValueError(f"{vid}: each motion_review entry needs a name and a nonempty [start, end) frame interval")
        if motion:
            row["motion_review"] = motion
        if "seedance" not in variant:
            raise ValueError(f"{vid}: describe the Seedance edit in seedance.stages")
        if sid not in clocks:
            path = next(s["path"] for s in sources if s["id"] == sid)
            clocks[sid] = [*packet_times(path), video_info(path)["duration"]]
        source = next(s for s in sources if s["id"] == sid)
        row["seedance"] = element_seedance.plan(variant["seedance"], row["ranges"], vid, clocks[sid], surveys.get(sid),
                                               job=job, source=source, targets=row.get("targets", [row]))
        protected.update(Path(p) for segment in row["seedance"]["segments"] for p in segment["references"])
        rows.append(row)
    if any((delivery / (row["name"] + ".mp4")).resolve() in protected for row in rows):
        raise ValueError("a delivery filename would overwrite an input")
    distinct_replacements(rows)
    return rows


def render_key(source, variant):
    if sha(Path(source["path"])) != source["sha256"]:
        raise ValueError(f"source changed: {source['path']}; initialize the new source in its own job")
    signatures = [element_seedance.signature(source, segment) for segment in variant["seedance"]["segments"]]
    return digest([variant["source"], variant["element"], variant["replacement"], signatures])


def packet_times(path):
    """Presentation-order timestamps including irregular spacing and independent of B-frame coding order."""
    data = json.loads(run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=time_base:packet=pts,flags", "-of", "json", path]))
    base = Fraction(data["streams"][0]["time_base"])
    pts = sorted(int(p["pts"]) for p in data["packets"] if "D" not in p.get("flags", ""))
    if not pts:
        raise ValueError(f"no playable video frames: {path}")
    return [float((t - pts[0]) * base) for t in pts]


def playback_times(path):
    metadata = probe(path)
    video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    offset = float(video.get("start_time", 0)) - float(metadata["format"].get("start_time", 0))
    return [offset + time for time in packet_times(path)]


def frame_near(times, at):
    end = min(bisect_left(times, at), len(times) - 1)
    before = max(0, end - 1)
    return before if at - times[before] <= abs(times[end] - at) else end


def periodic_frames(times, first, end, seconds):
    """Cover each requested playback interval, including variable-frame-rate clips."""
    elapsed = times[end - 1] - times[first]
    return {frame_near(times, times[first] + i * seconds) for i in range(int(elapsed / seconds) + 1)}


def check_clock(times, path):
    other = packet_times(path)
    if len(times) != len(other) or any(abs(t - u) > .0011 for t, u in zip(times, other)):
        raise ValueError(f"frame count or presentation timing differs from source: {path}")


def render(job, source, variant, key, output, *, submit_only=False):
    original = Path(source["path"])
    info = video_info(original)
    streams = probe(original)
    video = next(s for s in streams["streams"] if s["codec_type"] == "video")
    times = packet_times(original)
    if variant["ranges"][-1][1] > len(times) or any(i >= len(times) for i in variant["review_frames"]):
        raise ValueError("range or review frame exceeds source frame count")
    start = float(video.get("start_time", 0)) - float(streams["format"].get("start_time", 0))
    generations = element_seedance.obtain(output.parent, source, variant, times, info, budget_job=job,
                                          submit_only=submit_only)
    if submit_only:
        return None
    inputs = ["-threads:v", "3", "-i", original]
    graph_parts = []
    previous = "0:v:0"
    for i, generation in enumerate(generations, 1):
        a, b = generation["range"]
        inputs.extend(["-threads:v", "2", "-i", generation["raw"]])
        if generation["width"] * info["height"] != generation["height"] * info["width"]:
            # Providers can pad a few pixels to meet encoder alignment. Wider changes imply a different shot.
            aspect_error = abs(generation["width"] / generation["height"] / (info["width"] / info["height"]) - 1)
            if aspect_error > .03:
                raise ValueError(f"unexpected Seedance aspect ratio for {generation['generation_id']}")
        speed = (generation["input_frames_24fps"] / 24) / generation["raw_duration"]
        shift = int((start + times[a]) * 1_000_000)
        graph_parts.append(f"[{i}:v:0]scale={info['width']}:{info['height']}:flags=lanczos,setsar=1,"
                           f"format=yuv420p,settb=1/1000000,setpts=(PTS-STARTPTS)*{speed:.12f}+{shift}[gen{i}]")
        enabled = "+".join(f"between(n,{max(a, lo)},{min(b, hi)-1})" for lo, hi in variant["ranges"] if lo < b and a < hi)
        graph_parts.append(f"[{previous}][gen{i}]overlay=ts_sync_mode=nearest:eof_action=repeat:"
                           f"enable='{enabled}'[base{i}]")
        previous = f"base{i}"
    graph_parts.append(f"[{previous}]trim=end_frame={len(times)},pad=ceil(iw/2)*2:ceil(ih/2)*2[out]")
    graph = ";".join(graph_parts)
    audio = ["-c:a", "copy"]
    for i, stream in enumerate(s for s in streams["streams"] if s["codec_type"] == "audio"):
        if stream["codec_name"] not in {"aac", "ac3", "eac3", "alac", "mp3", "opus", "flac"}:
            audio += [f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]
    color = []
    for field, flag in (("color_range", "-color_range"), ("color_space", "-colorspace"),
                        ("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc")):
        if video.get(field) and video[field] != "unknown":
            color.extend([flag, video[field]])
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as td:
        partial = Path(td) / "render.mp4"
        graph_file = Path(td) / "overlay.ffgraph"
        graph_file.write_text(graph, encoding="utf-8")
        run([FFMPEG, "-v", "error", "-filter_complex_threads", "2", *inputs, "-/filter_complex", graph_file,
             "-map", "[out]", "-map", "0:a?", *audio, "-c:v", "libx264", "-profile:v", "high",
             "-pix_fmt", "yuv420p", "-crf", "16", "-preset", "fast", "-threads:v", "6",
             "-fps_mode:v", "passthrough", "-enc_time_base:v", video["time_base"], *color, "-movflags", "+faststart",
             partial])
        check_clock(times, partial)
        if abs(video_info(partial)["duration"] - info["duration"]) > 1 / info["fps"] + .001:
            raise ValueError("rendered video duration differs from source")
        checksum = sha(partial)
        partial.replace(output)
    receipt = {"key": key, "sha256": checksum, "video": str(output), "generations": generations}
    write(job / "elements" / variant["id"] / "render.json", receipt)
    return receipt


def sample_frames(variant, times):
    count = len(times)
    frames = {0, count - 1, *variant["review_frames"]}
    for start, end in {tuple(span) for target in variant.get("targets", [variant]) for span in target["ranges"]}:
        frames.update((start - 1, start, (start + end - 1) // 2, end - 1, end))
        frames.update(periodic_frames(times, start, end, 5))
    priority = {}
    for target in [variant, *variant.get("targets", [])]:
        for named in target.get("target_selection", {}).get("frames", {}).values():
            for n in named if isinstance(named, list) else [named]:
                if n >= count:
                    raise ValueError(f"selection frame {n} exceeds the source frame count")
                priority[n] = None
    return [*priority, *sorted(n for n in frames if 0 <= n < count and n not in priority)]


def motion_ranges(variant):
    """Keep the requested action names and fill uncovered parts of each generated source interval."""
    clips = dict(variant.get("motion_review", {}))
    named = sorted(clips.values())
    for segment in variant.get("seedance", {}).get("segments", []):
        start, end = segment["range"]
        cursor = start
        for first, after in [*named, [end, end]]:
            if first > cursor:
                stop = min(first, end)
                name = f"Frames {cursor}–{stop - 1}"
                while name in clips:
                    name += " (auto)"
                clips[name] = [cursor, stop]
            cursor = max(cursor, after)
            if cursor >= end:
                break
    return dict(sorted(clips.items(), key=lambda item: item[1]))


def motion_valid(record, interval, video):
    return (isinstance(record, dict) and record.get("frames") == interval and record.get("video") == str(video)
            and video.is_file() and record.get("sha256") == sha(video))


def motion_path(folder, interval):
    start, end = interval
    return folder / f"motion-{start}-{end}.mp4"


def motion_evidence(source, result, ranges, times, info, folder, previous):
    clips = {}
    by_interval = {}
    for name, interval in ranges.items():
        start, end = interval
        if (start, end) in by_interval:
            clips[name] = by_interval[start, end]
            continue
        video = motion_path(folder, interval)
        record = previous.get(name, {})
        if not motion_valid(record, interval, video):
            with TemporaryDirectory(dir=folder) as td:
                temp = Path(td) / "pair.mp4"
                until = times[end] if end < len(times) else times[0] + info["duration"]
                last_hold = until - times[end - 1]
                review.motion_clip(source, result, interval, (info["width"], info["height"]), last_hold, temp)
                check_clock([t - times[start] for t in times[start:end]], temp)
                if abs(video_info(temp)["duration"] - (until - times[start])) > .0011:
                    raise ValueError("motion review duration differs from the source interval")
                checksum = sha(temp)
                temp.replace(video)
            record = {"frames": interval, "video": str(video), "sha256": checksum}
        clips[name] = by_interval[start, end] = record
    return clips


def publish(output, delivery, checksum, prior):
    delivery.parent.mkdir(parents=True, exist_ok=True)
    with runtime.locked(str(delivery.resolve()).casefold()):
        if delivery.exists():
            existing = sha(delivery)
            if existing == checksum:
                return
            if existing != prior:
                raise FileExistsError(f"delivery filename is already occupied: {delivery}")
        with TemporaryDirectory(dir=delivery.parent) as td:
            partial = Path(td) / "delivery.mp4"
            shutil.copyfile(output, partial)
            if sha(partial) != checksum:
                raise OSError("delivery copy failed verification")
            if prior and delivery.exists():
                partial.replace(delivery)
            elif os.name == "nt":
                partial.rename(delivery)  # Fails if another producer claimed the filename.
            else:
                os.link(partial, delivery)


def command(action, job, state, variant, at=None, *, submit_only=False):
    """Run one element command for one variant. submit_only (refs and render) only submits the generations."""
    if "seedance" not in variant:
        raise ValueError("this variant has no Seedance plan: submit it again with plan --input")
    source = next(s for s in state["sources"] if s["id"] == variant["source"])
    folder = job / "elements" / variant["id"]
    output = folder / "render.mp4"
    receipt_path, checks_path = folder / "render.json", folder / "check.json"
    receipt = read(receipt_path) if receipt_path.exists() else {}
    checks = read(checks_path) if checks_path.exists() else {}
    selection = {k: variant[k] for k in ("targets", "target_selection", "motion_review", "replacement_key")
                 if k in variant}
    if action in ("refs", "render", "approve", "assemble"):
        distinct_replacements(state["variants"])
    if action == "refs":
        if sha(Path(source["path"])) != source["sha256"]:
            raise ValueError("source changed; initialize the new source in its own job")
        return element_seedance.reference_images(source, variant["seedance"], wait=not submit_only)
    missing = [p for segment in variant["seedance"]["segments"] for p in segment["references"]
               if not Path(p).is_file()]
    if missing and action == "status":
        return {"status": "awaiting reference images", "missing": missing}
    if missing:
        raise ValueError("missing reference images: run refs for planned Lumos images or restore the supplied references")
    for name in variant["seedance"].get("image_tasks", {}):
        path = Path(name)
        task = path.with_name("request.json")
        if not task.is_file() or read(task).get("sha256") != sha(path):
            if action == "status":
                return {"status": "reference image needs verification", "path": name}
            raise ValueError(f"run refs to verify the Lumos reference: {path}")
    key = render_key(source, variant)
    rows = receipt.get("generations", [])
    current = (receipt.get("key") == key and output.is_file() and receipt["sha256"] == sha(output)
               and len(rows) == len(variant["seedance"]["segments"])
               and all(r.get("status") == "succeeded" and r.get("model") == element_seedance.MODEL
                       and r.get("generation_mode") == "video_edit" and r.get("generation_id")
                       and r.get("raw_sha256") and r.get("range") == seg["range"]
                       and r.get("key") == element_seedance.signature(source, seg)
                       for r, seg in zip(rows, variant["seedance"]["segments"])))
    if action == "render":
        return receipt if current else render(job, source, variant, key, output, submit_only=submit_only)
    review_key = digest([key, variant["review_frames"], selection, element_seedance.CASH_GUIDANCE])
    review_folder = folder / "review" / (review_key[:12] + "-" + str(receipt.get("sha256", ""))[:12])
    motion = motion_ranges(variant)
    prior_clips = checks.get("motion_clips", {})
    checked = (current and checks.get("render_sha256") == receipt["sha256"] and checks.get("key") == review_key
               and checks.get("sheets") and all(Path(p).is_file() for p in checks["sheets"])
               and all(motion_valid(prior_clips.get(name, {}), span, motion_path(review_folder, span))
                       for name, span in motion.items()))
    approved = bool(checked and checks.get("approved"))
    if action == "status":
        requests = []
        for index, segment in enumerate(variant["seedance"]["segments"], 1):
            identity = element_seedance.signature(source, segment)
            state_file = element_seedance.segment_folder(folder, index, identity) / "request.json"
            request = read(state_file) if state_file.is_file() else {}
            requests.append({"range": segment["range"], "status": request.get("status", "waiting"),
                             "generation_id": request.get("generation_id")})
        return {"element": variant["element"], "replacement": variant["replacement"],
                **selection, "rendered": current, "checked": bool(checked), "approved": approved,
                "segments": requests, "generation_ids": [r["generation_id"] for r in requests if r["generation_id"]]}
    if not current:
        raise ValueError("run render for the current source and replacement asset first")
    if action == "check":
        info = video_info(output)
        times = playback_times(output)
        if at is None:
            frames = sample_frames(variant, times)
            if any(end > len(times) for start, end in motion.values()):
                raise ValueError("motion_review interval exceeds source frame count")
        else:
            if not times[0] <= at < times[0] + info["duration"]:
                raise ValueError("--at must be within the source duration")
            frames = [frame_near(times, at)]
        evidence = review.element_sheets(source["path"], output, frames, times, review_folder,
                                         full_size=at is not None)
        if at is not None:
            return evidence
        if motion:
            evidence["motion_clips"] = motion_evidence(source["path"], output, motion, times, info, review_folder,
                                                      prior_clips)
        report = {**evidence, **selection, "key": review_key, "render_sha256": receipt["sha256"], "approved": approved,
                  "element": variant["element"], "replacement": variant["replacement"], "video": str(output),
                  "generation_ids": [r["generation_id"] for r in receipt["generations"]]}
        write(checks_path, report)
        return report
    if action == "approve":
        if not checked:
            raise ValueError("run check and inspect its source/result pairs before approving")
        checks["approved"] = True
        write(checks_path, checks)
        return {"approved": True}
    if action == "assemble":
        if not approved:
            raise ValueError("inspect check results and approve the current variant before delivery")
        delivery = Path(state["delivery_dir"]) / (variant["name"] + ".mp4")
        prior = folder / "delivery.json"
        previous = read(prior) if prior.is_file() else {}
        previous_sha = previous.get("sha256") if previous.get("path") == str(delivery) else None
        from reskin import job as jobs
        with runtime.locked(str(job / jobs.STATE)):
            if variant not in read(job / jobs.STATE)["variants"]:
                raise ValueError("variant plan changed; render and review the current plan")
            publish(output, delivery, receipt["sha256"], previous_sha)
            write(prior, {"path": str(delivery), "sha256": receipt["sha256"]})
        return str(delivery)
    raise ValueError(f"unsupported elements command: {action}")


def dispatch(args, job, state):
    """Report each selected source or variant, allowing unrelated variants to finish independently."""
    style_options = any(getattr(args, name, None) for name in
                        ("segment", "accept", "covers", "reason", "redo", "generation_id"))
    if args.command == "plan":
        if args.only or style_options or args.at is not None:
            raise ValueError("elements plan takes --input with the job's variants")
        if args.input:
            from reskin import job as jobs
            order = read(args.input)
            with runtime.locked(str(job / jobs.STATE)):
                state = jobs.load(job)
                order.setdefault("surveys", state.get("surveys", {}))
                variants = variants_from_order(order, state["sources"], Path(state["delivery_dir"]), job)
                state["variants"] = variants
                state["surveys"] = order["surveys"]
                write(job / jobs.STATE, state)
        print(json.dumps({"variants": state["variants"], "planned_segments": sum(len(v.get("seedance", {}).get("segments", []))
                         for v in state["variants"])}, ensure_ascii=False))
        return
    survey = args.command in ("analyze", "survey")
    if not survey and args.command not in ("refs", "render", "check", "approve", "assemble", "status"):
        raise ValueError("elements uses analyze/survey, plan, refs, render, check, approve and assemble")
    if style_options or args.input:
        raise ValueError("elements accepts --only to select variant IDs, or source IDs for analyze/survey")
    if args.at is not None and args.command not in ("survey", "check"):
        raise ValueError("--at applies to survey or check")
    items = state["sources" if survey else "variants"]
    selected = set(args.only or [])
    unknown = selected - {item["id"] for item in items}
    if unknown:
        raise ValueError(f"unknown selection: {', '.join(sorted(unknown))}")
    if not items:
        if args.command == "status":
            print(json.dumps({"status": "awaiting variant plan", "sources": [s["id"] for s in state["sources"]]}))
            return
        raise ValueError("survey the source, then run plan --input with its element variants")
    chosen = [item for item in items if not selected or item["id"] in selected]
    if args.command in ("refs", "render") and len(chosen) > 1:
        # Submit every variant's generations before waiting on any, so the variants generate at the same time.
        for item in chosen:
            try:
                with runtime.locked(str(job / "elements" / item["id"])):
                    command(args.command, job, state, item, submit_only=True)
            except (ValueError, RuntimeError, OSError, KeyError):
                pass  # the pass below reports it
    errors = []
    for item in chosen:
        key = item["id"]
        try:
            if survey:
                info = video_info(item["path"])
                result = info
                if args.command == "survey":
                    times = playback_times(item["path"])
                    if args.at is None:
                        frames = sorted(periodic_frames(times, 0, len(times), 3) | {len(times) - 1})
                    else:
                        if not times[0] <= args.at < times[0] + info["duration"]:
                            raise ValueError("--at must be within the source duration")
                        frames = [frame_near(times, args.at)]
                    result = review.element_sheets(item["path"], None, frames, times, job / "elements" / key / "survey",
                                                   full_size=args.at is not None)
            else:
                with nullcontext() if args.command == "status" else runtime.locked(str(job / "elements" / key)):
                    result = command(args.command, job, state, item, args.at)
            print(json.dumps({"source" if survey else "variant": key, "result": result}, ensure_ascii=False))
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            errors.append(f"{key}: {exc}")
    if errors:
        raise ValueError("; ".join(errors))
