"""Native-frame single-object edits, review and delivery for independent variants of a source."""
from __future__ import annotations

import json
import os
import re
import shutil
from bisect import bisect_left
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from reskin import elements_review as review, runtime
from reskin.common import FFMPEG, FFPROBE, digest, probe, read, run, sha, video_info, write


def integers(row, count):
    return isinstance(row, list) and len(row) == count and all(type(n) is int for n in row)


def variants_from_order(order, sources, delivery):
    requested = order.get("variants")
    if not isinstance(requested, list) or not requested:
        raise ValueError("elements requires one or more variants")
    source_ids = {s["id"] for s in sources}
    protected = {Path(s["path"]).resolve() for s in sources}
    seen_names, rows = set(), []
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
        for field in ("element", "replacement"):
            value = variant.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{vid}: describe the {field}")
            row[field] = value.strip()
        ranges = variant.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError(f"{vid}: ranges must list the target's visible source frame intervals")
        end = 0
        for interval in ranges:
            if not integers(interval, 2) or not end <= interval[0] < interval[1]:
                raise ValueError(f"{vid}: ranges must be ordered disjoint [start, end) frame intervals")
            end = interval[1]
        row["ranges"] = ranges
        frames = variant.get("review_frames", [])
        if not isinstance(frames, list) or any(type(n) is not int or n < 0 for n in frames):
            raise ValueError(f"{vid}: review_frames must contain source frame numbers")
        row["review_frames"] = frames
        if "target_selection" in variant:
            selection = variant["target_selection"]
            if isinstance(selection, str) and selection.strip():
                selection = {"reason": selection.strip()}
            if not isinstance(selection, dict):
                raise ValueError(f"{vid}: target_selection takes a reason or an object with optional named frames")
            moments = selection.get("frames", {})
            if not isinstance(moments, dict):
                raise ValueError(f"{vid}: target_selection.frames maps names to source frame numbers")
            for label, value in moments.items():
                points = value if isinstance(value, list) else [value]
                if not label.strip() or any(type(n) is not int or n < 0 for n in points):
                    raise ValueError(f"{vid}: each named selection frame must be a nonnegative frame number")
            row["target_selection"] = selection
        if bool(variant.get("candidate")) == bool(variant.get("overlay")):
            raise ValueError(f"{vid}: provide exactly one edited candidate or tracked overlay")
        if variant.get("candidate"):
            candidate = Path(variant["candidate"]).expanduser().resolve()
            if candidate in protected:
                raise ValueError(f"{vid}: candidate must differ from the source")
            row["candidate"] = str(candidate)
        else:
            overlay = variant["overlay"]
            if not isinstance(overlay, dict) or not overlay.get("image"):
                raise ValueError(f"{vid}: overlay needs an image")
            track = overlay.get("track")
            if (not isinstance(track, list) or not track or any(not integers(p, 3) for p in track)
                    or track[0][0] < 0 or any(a[0] >= b[0] for a, b in zip(track, track[1:]))):
                raise ValueError(f"{vid}: track must list increasing [frame, x, y] points")
            size = [overlay.get("width"), overlay.get("height")]
            if not integers(size, 2) or min(size) < 1:
                raise ValueError(f"{vid}: overlay width and height must be positive pixel dimensions")
            row["overlay"] = {"image": str(Path(overlay["image"]).expanduser().resolve()),
                              "width": size[0], "height": size[1], "track": track}
        rows.append(row)
    protected.update(asset(row) for row in rows)
    if any((delivery / (row["name"] + ".mp4")).resolve() in protected for row in rows):
        raise ValueError("a delivery filename would overwrite an input")
    return rows


def asset(variant):
    return Path(variant["candidate"] if "candidate" in variant else variant["overlay"]["image"])


def render_keys(source, variant):
    original = Path(source["path"])
    if sha(original) != source["sha256"]:
        raise ValueError(f"source changed: {original}; initialize the new source in its own job")
    asset_sha = sha(asset(variant))
    legacy = {k: v for k, v in variant.items() if k != "target_selection"}
    recipe = {k: v for k, v in legacy.items() if k != "review_frames"}
    return digest([source["sha256"], recipe, asset_sha]), digest([source["sha256"], legacy, asset_sha])


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


def coordinate(track, axis):
    """Overlay's x/y evaluator counts from one, while source tracking points count from zero."""
    def interval(first, last):
        if first < last:
            split = (first + last + 1) // 2
            return f"if(lt(n-1,{track[split][0]}),{interval(first, split-1)},{interval(split, last)})"
        if first == len(track) - 1:
            return str(track[first][axis])
        left, right = track[first:first + 2]
        a, b = left[axis], right[axis]
        return f"{a}+({b}-{a})*(n-1-{left[0]})/{right[0]-left[0]}"
    return f"if(lt(n-1,{track[0][0]}),{track[0][axis]},{interval(0, len(track)-1)})"


def render(job, source, variant, key, output):
    original = Path(source["path"])
    info = video_info(original)
    streams = probe(original)
    video = next(s for s in streams["streams"] if s["codec_type"] == "video")
    times = packet_times(original)
    if variant["ranges"][-1][1] > len(times) or any(i >= len(times) for i in variant["review_frames"]):
        raise ValueError("range or review frame exceeds source frame count")
    start = float(video.get("start_time", 0)) - float(streams["format"].get("start_time", 0))
    enabled = "+".join(f"between(n,{a},{b-1})" for a, b in variant["ranges"])
    inputs = ["-i", original]
    position = "x=0:y=0"
    if "candidate" in variant:
        candidate = asset(variant)
        candidate_info = video_info(candidate)
        if any(info[k] != candidate_info[k] for k in ("width", "height")):
            raise ValueError("candidate display dimensions differ from source")
        if abs(info["duration"] - candidate_info["duration"]) > 1 / info["fps"] + .001:
            raise ValueError("candidate video duration differs from source")
        check_clock(times, candidate)
        inputs += ["-i", candidate]
        prep = "format=yuv420p"
    else:
        layer = variant["overlay"]
        if layer["track"][-1][0] >= len(times):
            raise ValueError("tracking point exceeds source frame count")
        inputs += ["-i", asset(variant)]
        prep = f"scale={layer['width']}:{layer['height']}:flags=lanczos,format=rgba"
        position = f"x='{coordinate(layer['track'], 1)}':y='{coordinate(layer['track'], 2)}'"
    graph = (f"[1:v:0]{prep},setpts=PTS-STARTPTS+({start:.9f})/TB[layer];"
             f"[0:v:0][layer]overlay={position}:ts_sync_mode=nearest:enable='{enabled}',"
             f"trim=end_frame={len(times)},pad=ceil(iw/2)*2:ceil(ih/2)*2[out]")
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
    receipt = {"key": key, "sha256": checksum, "video": str(output)}
    write(job / "elements" / variant["id"] / "render.json", receipt)
    return receipt


def sample_frames(variant, times):
    count = len(times)
    frames = {0, count - 1, *variant["review_frames"]}
    for start, end in variant["ranges"]:
        frames.update((start - 1, start, (start + end - 1) // 2, end - 1, end))
        frames.update(periodic_frames(times, start, end, 5))
    priority = {}
    for named in variant.get("target_selection", {}).get("frames", {}).values():
        for n in named if isinstance(named, list) else [named]:
            if n >= count:
                raise ValueError(f"selection frame {n} exceeds the source frame count")
            priority[n] = None
    return [*priority, *sorted(n for n in frames if 0 <= n < count and n not in priority)]


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


def command(action, job, state, variant, at=None):
    source = next(s for s in state["sources"] if s["id"] == variant["source"])
    folder = job / "elements" / variant["id"]
    output = folder / "render.mp4"
    receipt_path, checks_path = folder / "render.json", folder / "check.json"
    receipt = read(receipt_path) if receipt_path.exists() else {}
    checks = read(checks_path) if checks_path.exists() else {}
    selection = {"target_selection": variant["target_selection"]} if "target_selection" in variant else {}
    if action == "status" and not asset(variant).is_file():
        return {"element": variant["element"], "replacement": variant["replacement"], **selection,
                "status": "awaiting asset"}
    key, legacy = render_keys(source, variant)
    current = receipt.get("key") in (key, legacy) and output.is_file() and receipt["sha256"] == sha(output)
    if current and receipt["key"] == legacy:
        receipt["key"] = key
        write(receipt_path, receipt)
    if action == "render":
        return receipt if current else render(job, source, variant, key, output)
    review_key = digest([key, variant["review_frames"], selection])
    previous_key = digest([legacy, selection]) if selection else legacy
    checked = (current and checks.get("render_sha256") == receipt["sha256"]
               and checks.get("key") in (review_key, previous_key)
               and checks.get("sheets") and all(Path(p).is_file() for p in checks["sheets"]))
    approved = bool(checked and checks.get("approved"))
    if action == "status":
        return {"element": variant["element"], "replacement": variant["replacement"],
                **selection, "rendered": current, "checked": bool(checked), "approved": approved}
    if not current:
        raise ValueError("run render for the current source and replacement asset first")
    if action == "check":
        info = video_info(output)
        times = playback_times(output)
        if at is None:
            frames = sample_frames(variant, times)
        else:
            if not times[0] <= at < times[0] + info["duration"]:
                raise ValueError("--at must be within the source duration")
            frames = [frame_near(times, at)]
        evidence = review.element_sheets(source["path"], output, frames, times,
                                         folder / "review" / (review_key[:12] + "-" + receipt["sha256"][:12]),
                                         full_size=at is not None)
        if at is not None:
            return evidence
        report = {**evidence, **selection, "key": review_key, "render_sha256": receipt["sha256"], "approved": approved,
                  "element": variant["element"], "replacement": variant["replacement"], "video": str(output)}
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
                variants = variants_from_order(order, state["sources"], Path(state["delivery_dir"]))
                state["variants"] = variants
                write(job / jobs.STATE, state)
        print(json.dumps({"variants": state["variants"]}, ensure_ascii=False))
        return
    survey = args.command in ("analyze", "survey")
    if not survey and args.command not in ("render", "check", "approve", "assemble", "status"):
        raise ValueError("elements uses analyze/survey, then render, check, approve and assemble")
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
    errors = []
    for item in items:
        key = item["id"]
        if selected and key not in selected:
            continue
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
                with runtime.locked(str(job / "elements" / key)):
                    result = command(args.command, job, state, item, args.at)
            print(json.dumps({"source" if survey else "variant": key, "result": result}, ensure_ascii=False))
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            errors.append(f"{key}: {exc}")
    if errors:
        raise ValueError("; ".join(errors))
