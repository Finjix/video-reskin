"""Small pure helpers: prompts, timelines, sizes, HTTP encoding, credentials, review mapping."""
import json
import os
import tempfile
import unittest

import numpy as np
from pathlib import Path

from reskin import assemble, images, net, prompts, review
from reskin.common import read, PROMPTS


class PromptTests(unittest.TestCase):
    def test_compose_fills_every_placeholder(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": "clear ice blocks", "remove": ["temporary popup", "floating arrow"],
               "extra": "Keep the snow white."}
        texts = {"scene": prompts.compose(style, "scene", row, views=3, look_image=4),
                 "subject": prompts.compose(style, "subject", row, what="hunter", description="blue winter coat"),
                 "video": prompts.compose(style, "video", row, extra="Keep the fish whole.", refs="Image 1 is the map.")}
        for kind, text in texts.items():
            self.assertNotIn("{", text, kind)
        self.assertIn("Images 1 to 3", texts["scene"])
        self.assertIn("Image 4", texts["scene"])
        self.assertIn("clear ice blocks", texts["scene"])
        self.assertIn("hunter", texts["subject"])
        self.assertIn("temporary popup; floating arrow", texts["video"])
        self.assertIn("Keep the fish whole.", texts["video"])
        self.assertIn("Image 1 is the map.", texts["video"])
        self.assertTrue(texts["video"].startswith("Edit the video: replace the materials of the input video frame for frame"))

    def test_ground_sentence_goes_to_scene_maps_and_videos_not_sheets(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": "x"}
        self.assertIn(style["ground"], prompts.compose(style, "scene", row, views=1, look_image=2))
        self.assertIn(style["ground"], prompts.compose(style, "video", row))
        self.assertNotIn(style["ground"], prompts.compose(style, "subject", row, what="a", description="b"))
        self.assertIn(style["look"], prompts.compose(style, "subject", row, what="a", description="b"))

    def test_video_prompt_keeps_the_inputs_own_cuts(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.compose(style, "video", {"materials": "x"})
        self.assertIn("moves and cuts only where the input does", text)
        self.assertNotIn("continuous", text)

    def test_timed_removals_only_where_on_screen(self):
        items = ["game logo", {"what": "warning line", "start": 0, "end": 10}, {"what": "watermark", "start": 30}]
        self.assertEqual(prompts.on_screen(items, (2, 2)), ["game logo", "warning line"])
        self.assertEqual(prompts.on_screen(items, (12, 20)), ["game logo"])
        self.assertEqual(prompts.on_screen(items, (25, 31)), ["game logo", "watermark"])
        self.assertIn("warning line (0-10 s)", prompts.describe(items))

    def test_timed_materials(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": ["snowy ground", {"what": "blue pickup truck", "start": 50, "end": 66}], "remove": []}
        self.assertNotIn("pickup truck", prompts.compose(style, "video", row, span=(0, 11)))
        self.assertIn("snowy ground, blue pickup truck", prompts.compose(style, "video", row, span=(48, 60)))
        self.assertIn("snowy ground", prompts.compose(style, "video", {"materials": "snowy ground"}, span=(0, 1)))

    def test_no_removal_clause_when_empty(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.compose(style, "video", {"materials": "x", "remove": []})
        self.assertNotIn("Remove these", text)


class PassthroughTimelineTests(unittest.TestCase):
    def test_originals_fill_the_gaps_on_the_clock(self):
        row = {"fps": 30.0, "keep": [0.0, 20.0],
               "segments": [{"id": "K01", "seam": "cut", "frames": [90, 300], "render_frames": [90, 300]},
                            {"id": "K02", "seam": "cut", "frames": [360, 600], "render_frames": [360, 600]}],
               "originals": [{"id": "N01", "frames": [0, 90]}, {"id": "N02", "frames": [300, 360]}]}
        lines = assemble.timeline(row, 24.0)
        self.assertEqual([l["id"] for l in lines], ["N01", "K01", "N02", "K02"])
        for a, b in zip(lines, lines[1:]):
            self.assertEqual(a["body"][1], b["body"][0])
        self.assertTrue(lines[0]["original"] and lines[2]["original"])
        self.assertEqual(assemble.runs_of(lines), [[0], [1], [2], [3]])

    def test_survey_suggests_passthrough(self):
        from reskin import survey
        result = {"end_card": {"start": 30.0}, "non_game": [{"start": 0, "end": 3.2, "what": "presenter"}],
                  "overlays": [{"what": "logo A", "start": 0, "end": 3, "remove": True},
                               {"what": "logo B", "start": 0, "end": 30, "remove": True}]}
        suggested = survey.suggested_edits(result, 35.0)
        self.assertEqual(suggested["passthrough"], [[0, 3.2]])
        self.assertEqual([r["what"] for r in suggested["remove"]], ["logo B"])


class SizeTests(unittest.TestCase):
    def test_target_sizes(self):
        self.assertEqual(assemble.target_size(1080, 1920), (1080, 1920))
        self.assertEqual(assemble.target_size(1920, 1080), (1920, 1080))
        self.assertEqual(assemble.target_size(3840, 2160), (1920, 1080))
        self.assertEqual(assemble.target_size(1080, 1350), (1080, 1350))

    def test_image_aspects(self):
        self.assertEqual(images.aspect_for(1920, 1080), "16:9")
        self.assertEqual(images.aspect_for(1080, 1920), "9:16")
        self.assertEqual(images.aspect_for(1080, 3000), "9:16")          # the nearest preset (4K needs one)


class SeamTests(unittest.TestCase):
    def moving(self, count=12, seed=1):
        import numpy as np
        rng = np.random.default_rng(seed)
        base = np.kron(rng.uniform(40, 200, (10, 6, 3)), np.ones((6, 6, 1)))    # smooth: small motion per step
        return np.stack([np.roll(base, i, axis=1) for i in range(count)]).astype(np.float32)

    def test_a_cut_passes_where_both_results_match(self):
        before = self.moving(count=12)
        found = assemble.choose_cut(assemble.cut_options(before, before + 1.0), 6)
        self.assertEqual(found["mode"], "cut")
        self.assertEqual(found["frame"], 6)              # every frame equal: the planned one

    def test_different_drawings_get_a_dissolve_at_the_least_visible_frame(self):
        import numpy as np
        before, after = self.moving(count=12, seed=1), self.moving(count=12, seed=2)
        after[8] = before[8] * .5 + after[8] * .5        # the drawings come closest around frame 8
        options = assemble.cut_options(before, after)
        found = assemble.choose_cut(options, 3)
        self.assertEqual(found["mode"], "dissolve")
        self.assertEqual(found["frame"], min(options, key=lambda o: max(o["ratio"], o["local_ratio"]))["frame"])

    def test_dissolve_runs_from_the_first_clip_to_the_second_without_blur(self):
        import numpy as np
        rng = np.random.default_rng(4)
        a, b = rng.uniform(0, 100, (40, 30, 3)).astype(np.float32), rng.uniform(150, 250, (40, 30, 3)).astype(np.float32)
        self.assertTrue(np.allclose(assemble.dissolve_frame(a, b, 0.0), a))
        self.assertTrue(np.allclose(assemble.dissolve_frame(a, b, 1.0), b))
        self.assertTrue(np.allclose(assemble.dissolve_frame(a, b, .5), (a + b) / 2))     # a blend, detail kept


class WarpTests(unittest.TestCase):
    def cost(self, truth, cols):
        import numpy as np
        rows = len(truth)
        cost = np.ones((rows, cols)) * 5
        for i, j in enumerate(truth):
            cost[i, j] = 0.1
        return cost

    def test_a_stall_in_the_result_is_skipped(self):
        # the result shows output frames 0-39 one to one, then holds for 8 frames, then goes on
        truth = list(range(40)) + [j + 8 for j in range(40, 96)]
        mapping, matched = assemble.warp(self.cost(truth, 104), 30)
        self.assertEqual(mapping, truth)
        self.assertLess(matched, 1)

    def test_a_result_that_starts_late_and_ends_early(self):
        truth = [min(j + 3, 79) for j in range(90)]
        mapping, _ = assemble.warp(self.cost(truth, 80), 30)
        self.assertEqual(mapping[:70], truth[:70])
        self.assertEqual(mapping[-1], 79)

    def test_place_hides_skips_where_the_result_stands_still(self):
        # speed 1.1 over 100 frames: 10 skips, all inside the still stretch 40-79, the knots hit exactly
        motion = np.ones(130)
        motion[40:80] = .01
        line = 1.1 * np.arange(101)
        picks = assemble.place(line, [0, 100], motion, 130)
        self.assertEqual((picks[0], picks[-1]), (0, 110))
        steps = [b - a for a, b in zip(picks, picks[1:])]
        self.assertEqual(steps.count(2), 10)
        self.assertEqual(steps.count(0), 0)
        self.assertTrue(all(40 <= picks[j] + 1 < 80 for j, d in enumerate(steps) if d == 2))
        self.assertEqual(len(picks), 101)


class TimelineTests(unittest.TestCase):
    def test_global_clock_has_no_drift(self):
        row = {"fps": 60.0, "keep": [0, 30.0], "segments": [
            {"id": "K01", "seam": "start", "frames": [0, 695], "render_frames": [0, 720]},
            {"id": "K02", "seam": "continuous", "frames": [695, 1330], "render_frames": [677, 1397]},
            {"id": "K03", "seam": "continuous", "frames": [1330, 1800], "render_frames": [1312, 1852]}]}
        lines = assemble.timeline(row, 24.0)
        self.assertEqual(lines[0]["out"][0], 0)
        self.assertEqual(lines[-1]["body"][1], 720)
        for a, b in zip(lines, lines[1:]):
            self.assertEqual(a["body"][1], b["body"][0])
            start, end = assemble.overlap(a, b)
            self.assertGreaterEqual(end - start, 12)
        self.assertEqual(assemble.runs_of(lines), [[0, 1, 2]])


class NetTests(unittest.TestCase):
    def test_multipart(self):
        body, content_type = net.multipart([("model", "m"), ("image[]", ("a.png", b"\x89PNG", "image/png"))])
        boundary = content_type.split("boundary=")[1]
        self.assertIn(f"--{boundary}--".encode(), body)
        self.assertIn(b'name="image[]"; filename="a.png"', body)
        self.assertIn(b"\x89PNG", body)

    def test_double_encoded_error(self):
        inner = {"type": "error", "error": {"type": "winky_permission_error", "message": "no"}}
        self.assertEqual(net.decode_json(json.dumps(json.dumps(inner)).encode()), inner)


class CredentialTests(unittest.TestCase):
    def test_round_trip(self):
        from reskin import credentials
        with tempfile.TemporaryDirectory() as folder:
            old = os.environ.get("VIDEO_RESKIN_HOME")
            os.environ["VIDEO_RESKIN_HOME"] = folder
            saved_env = os.environ.pop(credentials.ENV_KEY, None)
            try:
                with self.assertRaises(credentials.MissingCredential):
                    credentials.winky_key()
                credentials.save_winky_key("  lumos-test-key  ")
                self.assertEqual(credentials.winky_key(), "lumos-test-key")
                self.assertNotIn("lumos-test-key", Path(folder, "credentials.json").read_text(encoding="utf-8"))
                credentials.save_lumos_login("token-1", {"user_id": "u", "name": "名字"})
                self.assertEqual(credentials.lumos_refresh_token(), "token-1")
                self.assertEqual(credentials.summary()["lumos_user"]["name"], "名字")
                credentials.forget_lumos_login()
                with self.assertRaises(credentials.MissingCredential):
                    credentials.lumos_refresh_token()
            finally:
                if old is None:
                    os.environ.pop("VIDEO_RESKIN_HOME", None)
                else:
                    os.environ["VIDEO_RESKIN_HOME"] = old
                if saved_env is not None:
                    os.environ[credentials.ENV_KEY] = saved_env


class ReviewTests(unittest.TestCase):
    def test_gemini_watches_a_second_either_side_of_every_seam(self):
        self.assertEqual(review.SEAM_SPAN, 1.0)
        text = review.seam_prompt({"time": 15.04}, 14.04, True, "left", True)
        self.assertIn("1.00 s into this clip", text)
        self.assertIn("B must look like A", text)
        self.assertNotIn("B must look like A", review.seam_prompt({"time": 15.04}, 14.04, False, "left", False))

    def test_the_agent_sees_five_frames_per_seam_and_three_per_issue(self):
        self.assertEqual(review.SEAM_MOMENTS, (-.5, -.25, 0.0, .25, .5))
        self.assertEqual(review.issue_moments(10.0, 20.0, 55.0), [10.0, 15.0, 20.0])
        self.assertEqual(review.issue_moments(47.0, 47.2, 55.0), [46.8, 47.1, 47.4])
        self.assertEqual(review.issue_moments(0.0, 0.0, 55.0), [0.0, 0.0, 0.3])
        self.assertEqual(review.issue_moments(55.2, 55.3, 55.25), [54.95, 55.25, 55.25])

    def test_gemini_samples_six_frames_a_second_whatever_the_length(self):
        from reskin import winky
        self.assertEqual(winky.sample_fps(), 6.0)
        self.assertEqual(winky.sample_fps(24), 6.0)          # never above 6
        self.assertEqual(winky.sample_fps(2), 2.0)           # a lower rate may be asked for

    def test_motion_is_its_own_category_and_the_prompt_asks_for_it(self):
        self.assertIn("motion", review.CATEGORIES)
        self.assertIn("motion", review.SCHEMA["properties"]["issues"]["items"]["properties"]["category"]["enum"])
        text = review.prompt({"style": "realistic"}, {"remove": [], "materials": "snow"}, {"seams": []}, "left")
        self.assertIn("Compare movement", text)


class RedoTests(unittest.TestCase):
    def test_a_segment_goes_back_five_times_at_most(self):
        from reskin import render
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            render.bump(job, "S01:K02", "colour jump")
            render.bump(job, "S01:K02", "automatic retry", automatic=True)
            for reason in ("logo left", "logo still left", "crowd gone"):
                render.bump(job, "S01:K02", reason)
            self.assertEqual(render.bump(job, "S01:K02", "still wrong", "Keep the snow white.")["redos"], 5)
            with self.assertRaises(ValueError):
                render.bump(job, "S01:K02", "sixth time")
            row = read(job / "config/redo.json")["S01:K02"]
            self.assertEqual(row["attempt"], 7)
            self.assertEqual(row["prompt"], "Keep the snow white.")

    def test_results_that_left_the_source_are_set_aside_per_round(self):
        from reskin import render
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            output = job / "ai" / "S01-K01.mp4"
            output.parent.mkdir()
            for number, generation in enumerate(("aaaaaaaa1", "bbbbbbbb2")):
                output.write_bytes(b"x")
                kept = render.keep_rejected(job, "S01:K01", output, {"generation_id": generation, "round": 1,
                                                                    "follow": {"structure": .9 - number / 10, "edges": .97}})
                self.assertFalse(output.exists())
                self.assertEqual(len(kept), number + 1)
            output.write_bytes(b"x")
            self.assertEqual(len(render.keep_rejected(job, "S01:K01", output, {"generation_id": "cccccccc", "round": 2,
                                                                                "follow": {}})), 1)

    def test_replace_drops_earlier_corrective_sentences(self):
        from reskin import render
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            render.bump(job, "S01:K01", "fish too early", "Keep the collection box empty.")
            self.assertNotIn("prompt", render.bump(job, "S01:K01", "camera moved", replace=True))



class ChainTests(unittest.TestCase):
    """Segments of one shot render one after the other; each continuing one takes the previous result's frame."""
    ROW = {"segments": [{"id": "K01", "seam": "start"}, {"id": "K02", "seam": "continuous"},
                        {"id": "K03", "seam": "cut"}, {"id": "K04", "seam": "continuous"}]}

    def test_previous_is_the_segment_before_in_the_same_shot(self):
        from reskin import render
        ids = [None if p is None else p["id"] for p in (render.previous_of(self.ROW, s) for s in self.ROW["segments"])]
        self.assertEqual(ids, [None, "K01", None, "K03"])

    def test_the_previous_frame_is_described_first(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.references_clause(style, [{"role": "previous"}, {"role": "scene", "name": "camp"},
                                                 {"role": "subject", "what": "hunter"}])
        self.assertTrue(text.startswith("Image 1 is the restyled frame where this clip begins"))
        self.assertIn("Image 2: the whole location", text)
        self.assertIn("Image 3 is a three-view sheet (front, side, back) of hunter", text)
        self.assertTrue(text.endswith("that the input video does not show."))
        self.assertNotIn("{", text)

    def test_a_motion_strip_is_a_motion_reference_and_the_rest_stay_look_only(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.references_clause(style, [{"role": "scene"}, {"role": "subject", "what": "conveyor belt"},
                                                 {"role": "motion", "what": "whole tuna on the conveyor belt", "step": .05}])
        self.assertIn("Image 3 is a motion reference only, not a look: three moments of the input video, 0.05 s apart", text)
        self.assertIn("how the whole tuna on the conveyor belt move", text)
        self.assertIn("The other reference images are for look only", text)
        self.assertNotIn("motion, timing and content come from the input video", text)
        self.assertNotIn("{", text)

    def test_clips_go_to_seedance_at_24_fps(self):
        from reskin import render
        self.assertEqual(render.CLIP_FPS, 24)

    def test_two_scene_maps_are_named_together(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.references_clause(style, [{"role": "scene"}, {"role": "scene"}])
        self.assertTrue(text.startswith("Images 1 and 2: the whole location"))
        self.assertEqual(prompts.references_clause(style, []), "")

    def test_a_shot_renders_in_order_and_stops_after_a_failure(self):
        import threading
        import time
        from reskin import render
        order, lock, running = [], threading.Lock(), set()
        original = render.render_segment

        def fake(job, state, row, sid, seg, style):
            with lock:
                previous = render.previous_of(row, seg)
                self.assertFalse(previous and previous["id"] in running, "a segment started before the one it continues finished")
                running.add(seg["id"])
                order.append(seg["id"])
            time.sleep(.05)
            with lock:
                running.discard(seg["id"])
            if seg["id"] == "K03":
                raise RuntimeError("boom")
            return {"key": f"{sid}:{seg['id']}"}
        render.render_segment = fake
        try:
            with tempfile.TemporaryDirectory() as folder:
                results = render.run_all(Path(folder), {}, {"sources": {"S01": self.ROW}}, {}, workers=4)
        finally:
            render.render_segment = original
        self.assertLess(order.index("K01"), order.index("K02"))
        self.assertNotIn("K04", order)
        self.assertIn("previous segment S01:K03 failed", [r for r in results if r["key"] == "S01:K04"][0]["error"])


class GateTests(unittest.TestCase):
    """A continuing segment is submitted only after the segment before it is approved, and an approved segment
    keeps its result when the one before it is redone."""
    ROW = {"fps": 24, "width": 1080, "height": 1920,
           "segments": [{"id": k, "seam": seam, "render_frames": [0, 96]}
                        for k, seam in (("K01", "start"), ("K02", "continuous"), ("K03", "continuous"))]}

    def setUp(self):
        from reskin import render, lumos
        self.render, self.lumos = render, lumos
        self.folder = tempfile.TemporaryDirectory()
        self.job = Path(self.folder.name)
        self.previous_frame = {"K02": "frame-a", "K03": "frame-a"}
        self.submitted = []
        self.saved = (render.inputs_for, lumos.upload, render.cut_clip)

        def inputs_for(job, state, row, sid, seg, style, chain=True):
            refs = [{"path": "scene.png", "role": "scene"}]
            hashes = ["scene-sha"]
            if render.previous_of(row, seg) and chain:
                refs.insert(0, {"path": "chain.png", "role": "previous"})
                hashes.insert(0, self.previous_frame[seg["id"]])
            return {"source": "src", "frames": [0, 10], "fps": 24, "duration": 4, "pictures": hashes,
                    "references": refs, "prompt": "p", "model": "m", "resolution": "1080p", "attempt": 1}

        def upload(*args, **kwargs):
            self.submitted.append(args)
            raise RuntimeError("submitted")
        render.inputs_for, lumos.upload = inputs_for, upload
        render.cut_clip = lambda *args, **kwargs: None

    def tearDown(self):
        self.render.inputs_for, self.lumos.upload, self.render.cut_clip = self.saved
        self.folder.cleanup()

    def finish(self, seg_id, generation, verdict="pass"):
        """A finished result of `seg_id`, current for the inputs it has now, checked with `verdict` (None: not
        checked)."""
        seg = next(s for s in self.ROW["segments"] if s["id"] == seg_id)
        inputs = self.render.inputs_for(self.job, {}, self.ROW, "S01", seg, {})
        (self.job / "ai").mkdir(exist_ok=True)
        (self.job / "ai" / f"S01-{seg_id}.mp4").write_bytes(b"x")
        write_record = {"digest": self.render.core(inputs), "pinned": self.render.core(inputs, pinned=True),
                        "generation_id": generation, "status": "done"}
        from reskin.common import write
        write(self.render.state_path(self.job, f"S01:{seg_id}"), write_record)
        if verdict:
            write(self.job / "reports/check" / f"S01-{seg_id}-a{generation}.json",
                  {"segment": f"S01:{seg_id}", "generation_id": generation, "verdict": verdict,
                   "issues": [] if verdict == "pass" else [{"n": 1}]})

    def test_approval_needs_a_passed_check_of_the_current_result(self):
        self.finish("K01", "gen-k01", verdict=None)
        with self.assertRaises(ValueError):            # not checked
            self.render.approve(self.job, "S01:K01")
        self.finish("K01", "gen-k01b", verdict="fail")
        with self.assertRaises(ValueError):            # failed, nothing dismissed
            self.render.approve(self.job, "S01:K01")
        self.assertFalse(self.render.approved(self.job, "S01:K01"))
        self.render.approve(self.job, "S01:K01", dismiss="the logo Gemini saw is the original's HUD icon")
        self.assertTrue(self.render.approved(self.job, "S01:K01"))
        self.assertIn("S01:K01", read(self.job / "config/dismissed.json"))

    def segment(self, seg_id):
        return next(s for s in self.ROW["segments"] if s["id"] == seg_id)

    def test_nothing_is_submitted_before_the_previous_segment_is_approved(self):
        self.finish("K01", "gen-k01")
        with self.assertRaises(self.render.Waiting):
            self.render.render_segment(self.job, {}, self.ROW, "S01", self.segment("K02"), {})
        self.assertEqual(self.submitted, [])
        self.render.approve(self.job, "S01:K01")
        state = {"sources": [{"id": "S01", "path": "source.mp4"}]}
        with self.assertRaises(RuntimeError):
            self.render.render_segment(self.job, state, self.ROW, "S01", self.segment("K02"), {})
        self.assertEqual(len(self.submitted), 1)

    def test_an_approved_segment_survives_a_redo_of_the_one_before(self):
        self.finish("K01", "gen-k01")
        self.render.approve(self.job, "S01:K01")
        self.finish("K02", "gen-k02")
        self.render.approve(self.job, "S01:K02")
        self.render.bump(self.job, "S01:K01", "warning text left")
        self.assertFalse(self.render.approved(self.job, "S01:K01"))
        self.previous_frame["K02"] = "frame-b"          # K01 was drawn again
        result = self.render.render_segment(self.job, {}, self.ROW, "S01", self.segment("K02"), {})
        self.assertTrue(result["reused"])
        self.assertEqual(self.submitted, [])

    def test_an_approved_segment_survives_new_reference_pictures(self):
        self.finish("K01", "gen-k01")
        self.render.approve(self.job, "S01:K01")
        saved = self.render.inputs_for

        def remade(*args, **kwargs):                     # the scene map was made again, a sheet renumbered
            return {**saved(*args, **kwargs), "pictures": ["scene-sha-new", "sheet-sha"]}
        self.render.inputs_for = remade
        try:
            result = self.render.render_segment(self.job, {}, self.ROW, "S01", self.segment("K01"), {})
        finally:
            self.render.inputs_for = saved
        self.assertTrue(result["reused"])
        self.assertEqual(self.submitted, [])

    def test_an_unapproved_segment_after_a_changed_one_waits(self):
        self.finish("K01", "gen-k01")
        self.render.approve(self.job, "S01:K01")
        self.finish("K02", "gen-k02")                    # rendered, never approved
        self.render.bump(self.job, "S01:K01", "warning text left")
        self.finish("K01", "gen-k01-b")
        self.previous_frame["K02"] = "frame-b"
        with self.assertRaises(self.render.Waiting):
            self.render.render_segment(self.job, {}, self.ROW, "S01", self.segment("K02"), {})
        self.assertEqual(self.submitted, [])

    def test_a_waiting_segment_holds_the_rest_of_its_shot(self):
        original = self.render.render_segment

        def fake(job, state, row, sid, seg, style):
            if seg["id"] == "K02":
                raise self.render.Waiting("waiting for S01:K01")
            return {"key": f"{sid}:{seg['id']}"}
        self.render.render_segment = fake
        try:
            results = self.render.run_all(self.job, {}, {"sources": {"S01": self.ROW}}, {}, workers=2)
        finally:
            self.render.render_segment = original
        self.assertEqual([bool(r.get("waiting")) for r in results], [False, True, True])


class SurveyHandoverTests(unittest.TestCase):
    """What the agent no longer reads off the catalog sheets comes from the survey as text."""

    answer = {"scenes": [], "subjects": [],
              "end_card": {"present": True, "start": 55.3, "how": "white flash"},
              "overlays": [
                  {"kind": "logo", "text": "无尽冬日", "what": 'the game logo "无尽冬日" in the top-left corner',
                   "where": "top-left", "start": 0, "end": 15.0, "at": 5, "box_2d": [0, 0, 63, 287]},
                  {"kind": "logo", "text": "无尽冬日", "what": 'the game logo "无尽冬日" in the bottom-right corner',
                   "where": "bottom-right", "start": 15.0, "end": 58.0, "at": 30, "box_2d": [930, 750, 990, 990]},
                  {"kind": "hud", "text": "60", "what": "cash counter", "where": "top-right", "start": 49, "end": 55,
                   "at": 50, "box_2d": [0, 800, 53, 919]}]}

    def test_overlays_and_end_card_are_normalised(self):
        from reskin import survey
        result = survey.normalise(self.answer, 58.9)
        self.assertEqual(result["end_card"], {"start": 55.3, "how": "white flash"})
        self.assertEqual([o["remove"] for o in result["overlays"]], [False, False, False])
        self.assertEqual(result["overlays"][0]["box"], [0.0, 0.0, 0.287, 0.063])

    def test_suggested_edits_keep_logos_and_cut_the_end_card(self):
        from reskin import survey
        suggested = survey.suggested_edits(survey.normalise(self.answer, 58.9), 58.9)
        self.assertEqual(suggested["keep"], [0, 55.3])
        self.assertEqual(suggested["remove"], [])
        self.assertTrue(all("cash counter" not in r["what"] for r in suggested["remove"]))

    def test_a_measured_still_box_at_the_same_place_is_set_beside_the_span(self):
        from reskin import survey
        result = survey.normalise(self.answer, 58.9)
        survey.measured_spans(result, [{"box": [0.0, 0.0, 0.287, 0.063], "start": 0.0, "end": 15.0},
                                       {"box": [0.8, 0.0, 0.919, 0.053], "start": 49.5, "end": 55.5}])
        self.assertNotIn("measured", result["overlays"][0])  # retained overlays need no removal measurement
        self.assertNotIn("measured", result["overlays"][1])       # nothing measured in that corner
        self.assertNotIn("measured", result["overlays"][2])       # the HUD is not a removal

    def test_warning_is_retained_and_cached_deletion_flags_are_ignored(self):
        from reskin import survey
        answer = {**self.answer, "overlays": [
            {**self.answer["overlays"][0], "kind": "warning", "what": "small legal text"}]}
        result = survey.normalise(answer, 58.9)
        self.assertFalse(result["overlays"][0]["remove"])
        result["overlays"][0]["remove"] = True  # older survey cache
        self.assertEqual(survey.suggested_edits(result, 58.9)["remove"], [])

    def test_no_end_card_keeps_the_whole_video(self):
        from reskin import survey
        result = survey.normalise({**self.answer, "end_card": {"present": False, "start": 58.9, "how": ""}}, 58.9)
        self.assertIsNone(result["end_card"])
        self.assertEqual(survey.suggested_edits(result, 58.9)["keep"], [0, 58.9])


class RefsCheckTests(unittest.TestCase):
    row = {"segments": [{"subjects": [2, 5]}, {"subjects": [5]}],
           "subjects": [{"what": f"thing {k}", "start": float(k * 10)} for k in range(1, 6)]}

    def test_only_subjects_a_segment_takes_are_shown_and_checked(self):
        from reskin import refs
        self.assertEqual(refs.used_subjects(self.row), {2, 5})

    def test_the_scene_check_names_what_comes_after_its_views(self):
        from reskin import refs
        text = refs.check_prompt({"kind": "scene", "row": self.row, "item": {"views": [0.5, 12.0]}})
        self.assertIn("thing 2 (from 20 s)", text)
        self.assertNotIn("thing 1 (", text)
        sheet = refs.check_prompt({"kind": "subject", "row": self.row, "item": {"what": "whole tuna"}})
        self.assertIn("whole tuna", sheet)


if __name__ == "__main__":
    unittest.main()
