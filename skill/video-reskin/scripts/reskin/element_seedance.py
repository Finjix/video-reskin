"""Edit original source intervals with Seedance and retain the remote generation and downloaded media evidence."""
from __future__ import annotations

import unicodedata
from bisect import bisect_left
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from reskin import lumos, net, runtime
from reskin.common import FFMPEG, digest, log, read, run, sha, video_info, write

MODEL = lumos.VIDEO_MODEL
RESOLUTION = lumos.VIDEO_RESOLUTION


def plan(value, ranges, label, clock, survey=None):
    from reskin.pipeline import FPS, build_plan
    segments = value.get("stages", value.get("segments")) if isinstance(value, dict) else None
    if not isinstance(segments, list) or not segments:
        raise ValueError(f"{label}: seedance.segments must describe the original video intervals to edit")
    result = []
    union = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError(f"{label}: expected a Seedance segment")
        span = segment.get("range")
        if (not isinstance(span, list) or len(span) != 2 or not all(type(x) is int for x in span)
                or not 0 <= span[0] < span[1] or (union and span[0] < union[-1][1])):
            raise ValueError(f"{label}: Seedance frame intervals must be ordered and disjoint")
        references = segment.get("references")
        if (not isinstance(references, list) or not 1 <= len(references) <= 30
                or any(not isinstance(p, str) or not p.strip() for p in references)):
            raise ValueError(f"{label}: provide 1 to 30 reference images per Seedance segment")
        prompt = segment.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{label}: provide the edit prompt for each Seedance segment")
        result.append({"range": span, "references": [str(Path(p).expanduser().resolve()) for p in references],
                       "prompt": prompt.strip()})
        if union and span[0] == union[-1][1]:
            union[-1][1] = span[1]
        else:
            union.append(list(span))
    covered = []
    for start, end in ranges:
        if covered and covered[-1][1] == start:
            covered[-1][1] = end
        else:
            covered.append([start, end])
    if union != covered:
        raise ValueError(f"{label}: Seedance segments must cover exactly the target ranges")
    # Style and element edits share the same continuous runs, semantic cuts and end-card handling.
    duration = clock[-1]
    survey = survey if survey is not None else {"non_game": [], "end_card_start": duration, "cuts": []}
    if not isinstance(survey, dict) or not all(k in survey for k in ("non_game", "end_card_start", "cuts")):
        raise ValueError(f"{label}: a source survey needs non_game, end_card_start and cuts in seconds")
    working_frames = max(1, round(duration * FPS))
    style_plan = build_plan({"frames": working_frames}, survey)
    boundaries = [float(t) for span in survey["non_game"] for t in (span["start"], span["end"])]
    boundaries.append(float(survey["end_card_start"]))
    native_times = {round(t * FPS): t for t in boundaries}
    def native(n):
        if n == working_frames:
            return len(clock) - 1
        return min(len(clock) - 1, bisect_left(clock, native_times.get(n, n / FPS) - 1e-8))
    outputs = []
    if any(b > len(clock) - 1 for a, b in ranges):
        raise ValueError(f"{label}: target range exceeds source")
    for begin, end in style_plan["originals"]:
        a, b = native(begin), native(end)
        if any(lo < b and a < hi for lo, hi in ranges):
            raise ValueError(f"{label}: target overlaps non-game footage or end card")
    for section in style_plan["segments"]:
        start, end = map(native, section["frames"])
        active = [stage for stage in result if stage["range"][0] < end and start < stage["range"][1]]
        if not active:
            continue
        refs = list(dict.fromkeys(p for stage in active for p in stage["references"]))
        if len(refs) > 30:
            raise ValueError(f"{label}: select at most 30 distinct references for this shared style segment")
        notes = []
        for stage in active:
            a, b = stage["range"]
            t0, t1 = clock[max(a, start)] - clock[start], clock[min(b, end)] - clock[start]
            notes.append(f"{t0:.4f}s to {t1:.4f}s: {stage['prompt']}")
        prompt = (active[0]["prompt"] if len(active) == 1 and active[0]["range"] == [start, end] else
                  "Follow the input timeline in seconds. Preserve the original scene when the target is absent.\n" +
                  "\n".join(notes))
        outputs.append({"range": [start, end], "references": refs, "prompt": prompt})
    return {"segments": outputs}


def signature(source, segment):
    return digest([MODEL, RESOLUTION, "video_edit", source["sha256"], segment["range"], segment["prompt"],
                   [sha(Path(p)) for p in segment["references"]]])


def segment_folder(folder, index, key):
    root = folder / "seedance"
    expected = root / f"{index:02d}-{key[:16]}"
    if expected.exists():
        return expected
    # Reuse a surviving edit when a preceding interval is removed or split during review.
    for candidate in sorted(root.glob(f"*-{key[:16]}/request.json")):
        if read(candidate).get("key") == key:
            return candidate.parent
    return expected


def generation_limit(value):
    if type(value) is not int or value < 0:
        raise ValueError("generation_limit must be a nonnegative total number of video submissions for this job")
    return value


def submission_history(job, ledger, names, attempt, order):
    """Import old request IDs and the earlier key-only reservations once, including failures."""
    old = read(ledger) if ledger.exists() else []
    if old and all(isinstance(row, dict) for row in old):
        return old
    rows, seen_ids, scopes = [], set(), {}
    for path in (job / "elements").glob("*/seedance/*/request.json"):
        request = read(path)
        slot = path.parents[2].name
        key = request["key"]
        record = {"key": key, "source": request["source_sha256"], "slot": slot,
                  "variant": names.get(slot, slot), "range": request["range"]}
        scopes.setdefault(key, []).append(record)
        for generation in [*request.get("failed_generation_ids", []), request.get("generation_id")]:
            if generation and generation not in seen_ids:
                rows.append(record)
                seen_ids.add(generation)
    known = Counter(row["key"] for row in rows)
    unresolved = set(old) - set(scopes)
    if unresolved:
        sources = {s["id"]: s for s in order.get("sources", [])}
        for variant in order.get("variants", []):
            source = sources.get(variant.get("source"))
            if not source:
                continue
            for segment in variant.get("seedance", {}).get("segments", []):
                try:
                    key = signature(source, segment)
                except OSError:
                    continue  # A historical reference can already have been archived.
                if key in unresolved:
                    slot = variant["id"]
                    scopes.setdefault(key, []).append({"key": key, "source": source["sha256"], "slot": slot,
                                                       "variant": names[slot], "range": segment["range"]})
        scopes.setdefault(attempt["key"], [attempt])
    for key in old:
        if known[key]:
            known[key] -= 1
        else:
            # An old timeout may have no receipt and therefore no recoverable assignment.
            matching = scopes.get(key, [])
            rows.append(matching[0] if len(matching) == 1 else {"key": key})
    return rows


def range_exhausted(rows, attempt, names, limit):
    """A shifted, split or merged interval still consumes the allowance of the original pictures."""
    start, end = attempt["range"]
    events = Counter()
    for row in rows:
        if "source" not in row:
            # Unattributable legacy reservations can already have charged this interval.
            lo, hi = start, end
        else:
            same_name = row["variant"] == attempt["variant"]
            renamed_slot = row["slot"] == attempt["slot"] and row["variant"] not in names.values()
            if row["source"] != attempt["source"] or not (same_name or renamed_slot):
                continue
            lo, hi = max(start, row["range"][0]), min(end, row["range"][1])
        if lo < hi:
            events[lo] += 1
            events[hi] -= 1
    depth = 0
    for frame in sorted(events):
        depth += events[frame]
        if depth >= limit:
            return True
    return False


def reserve_submission(job, key, source_hash, slot, span, *, check_only=False):
    """Reserve before the paid POST, even when its response might never arrive."""
    if job is None:
        raise ValueError("a job with a generation_limit is required before submitting a new Seedance task")
    job = job.resolve()
    state = job / "job.json"
    with runtime.locked(str(state)):
        order = read(state)
        limit = generation_limit(order.get("generation_limit", 0))
        ledger = job / "elements" / "submissions.json"
        names = {v["id"]: unicodedata.normalize("NFKC", v["name"]).casefold() for v in order.get("variants", [])}
        attempt = {"key": key, "source": source_hash, "variant": names.get(slot, slot), "slot": slot, "range": span}
        history = submission_history(job, ledger, names, attempt, order)
        if len(history) >= limit:
            raise ValueError(f"Seedance generation_limit reached ({len(history)}/{limit}); existing results remain reusable")
        from reskin.pipeline import MAX_RETRIES
        segment_limit = 1 + MAX_RETRIES
        if range_exhausted(history, attempt, names, segment_limit):
            raise ValueError(f"Seedance {slot} {span}: source frames already reached the {segment_limit}-submission limit "
                             f"(initial edit and {MAX_RETRIES} retries)")
        if not check_only:
            write(ledger, [*history, attempt])


def original_clip(folder, source, span, frame_count):
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "original.mp4"
    ready = False
    if clip.is_file():
        try:
            info = video_info(clip)
            ready = info["frames"] == frame_count and info["fps"] == 24
        except (ValueError, RuntimeError):
            pass  # The former fps-before-padding filter could leave a valid MP4 without any pictures.
    if not ready:
        partial = folder / "original.partial.mp4"
        start, end = span
        filters = (f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,"
                   f"tpad=stop_mode=clone:stop_duration={lumos.VIDEO_MIN_SECONDS},fps=24,"
                   "scale=w='min(iw,if(gte(iw,ih),-2,1080))':h='min(ih,if(gte(iw,ih),1080,-2))',"
                   "pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1")
        run([FFMPEG, "-v", "error", "-y", "-i", source["path"], "-map", "0:v:0", "-an", "-vf", filters,
             "-frames:v", str(frame_count), "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-threads", "4",
             "-pix_fmt", "yuv420p", partial])
        if video_info(partial)["frames"] != frame_count:
            raise ValueError(f"Seedance reference clip did not produce all {frame_count} input frames")
        partial.replace(clip)
    if clip.stat().st_size > 190_000_000:
        raise ValueError("Seedance reference video is too large; split this interval in the plan")
    return clip


def upload_reference(path, working, index):
    from PIL import Image, ImageOps
    from reskin.pipeline import UPLOAD_LIMIT

    with Image.open(path) as image:
        oriented = ImageOps.exif_transpose(image)
        w, h = oriented.size
        prepared = path
        if min(w, h) < 300 or path.stat().st_size > UPLOAD_LIMIT:
            if min(w, h) < 300:
                scale = 300 / min(w, h)
                oriented = oriented.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
            prepared = working / f"reference-{index}.jpg"
            oriented.convert("RGB").save(prepared, quality=95, subsampling=0)
    return lumos.upload(prepared, "video-to-video"), {
        "path": str(path), "sha256": sha(path), "uploaded_sha256": sha(prepared)}


def submit(folder, source, segment, count, identity, *, budget_job=None):
    with runtime.locked(str(folder.resolve())):
        state = folder / "request.json"
        previous = read(state) if state.exists() else {}
        if previous.get("generation_id") and previous.get("status") != "failed":
            return previous
        allowance = (budget_job, identity, source["sha256"], folder.parent.parent.name, segment["range"])
        reserve_submission(*allowance, check_only=True)
        clip = original_clip(folder, source, segment["range"], count)
        video = lumos.upload(clip, "video-to-video")
        with TemporaryDirectory(dir=folder) as td:
            prepared = [upload_reference(Path(p), Path(td), i) for i, p in enumerate(segment["references"])]
        images = [asset for asset, metadata in prepared]
        prompt = segment["prompt"] + "\n\n" + lumos.CASH_GUIDANCE
        reserve_submission(*allowance)
        started = lumos.submit_video_edit(video, images, prompt, resolution=RESOLUTION, model=MODEL)
        generation_id = started.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ValueError("Seedance submission returned no generation_id")
        receipt = {"key": identity, "range": segment["range"], "model": MODEL, "generation_mode": "video_edit",
                   "generation_id": generation_id, "status": "submitted", "source_sha256": source["sha256"],
                   "input_sha256": sha(clip), "input_frames_24fps": count, "prompt": prompt,
                   "reference_images": [metadata for asset, metadata in prepared]}
        if previous:
            receipt["failed_generation_ids"] = [*previous.get("failed_generation_ids", []), previous["generation_id"]]
        write(state, receipt)
        log(f"Seedance {segment['range']} submitted {receipt['generation_id']}")
        return receipt


def verify_result(result, expected_id, *, failed=False):
    if result.get("generation_id") != expected_id:
        raise ValueError(f"Seedance generation_id mismatch for {expected_id}")
    config = result.get("generation_config") or {}
    if not isinstance(config, dict):
        raise ValueError(f"invalid Seedance generation_config for {expected_id}")
    # Failed jobs can omit the model configuration. A successful result must positively identify both fields.
    for field, expected in (("model", MODEL), ("generation_mode", "video_edit")):
        if (not failed or field in config) and config.get(field) != expected:
            raise ValueError(f"Seedance {field} mismatch for {expected_id}")


def complete(folder, receipt):
    raw = folder / "model.mp4"
    state = folder / "request.json"
    if receipt.get("status") == "succeeded" and raw.is_file() and sha(raw) == receipt.get("raw_sha256"):
        return receipt
    try:
        result = lumos.wait(receipt["generation_id"])
    except lumos.GenerationFailed as error:
        verify_result(error.generation, receipt["generation_id"], failed=True)
        write(state, {**receipt, "status": "failed", "error": str(error)})
        raise
    verify_result(result, receipt["generation_id"])
    net.download(lumos.result_urls(result)[0], raw)
    info = video_info(raw)
    updated = {**receipt, "status": "succeeded", "raw_sha256": sha(raw), "raw": str(raw),
               "raw_duration": info["duration"], "width": info["width"], "height": info["height"]}
    write(state, updated)
    return updated


def obtain(folder, source, variant, times, info, *, budget_job=None):
    from concurrent.futures import ThreadPoolExecutor
    segments = variant["seedance"]["segments"]
    specs = []
    for index, segment in enumerate(segments):
        a, b = segment["range"]
        if b > len(times):
            raise ValueError("Seedance segment exceeds source frame count")
        end = times[b] if b < len(times) else info["duration"]
        duration = end - times[a]
        # A 30 s cut on the style planner's 24 fps clock can land just between two native source frames.
        if round(duration * 24) > 721 or duration <= 0:
            raise ValueError("split Seedance ranges into original video intervals of at most 30 seconds")
        key = signature(source, segment)
        path = segment_folder(folder, index + 1, key)
        specs.append((path, segment, min(720, max(lumos.VIDEO_MIN_SECONDS * 24, round(duration * 24))), key))
    # Persist each accepted ID before polling. A retry resumes every already-submitted interval.
    requests = [submit(path, source, segment, count, key, budget_job=budget_job) for path, segment, count, key in specs]
    def collect(pair):
        spec, receipt = pair
        return complete(spec[0], receipt)
    with ThreadPoolExecutor(max_workers=min(4, len(requests))) as pool:
        return list(pool.map(collect, zip(specs, requests)))
