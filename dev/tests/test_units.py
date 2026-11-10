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
        row = {"materials": "真实的冰块", "remove": ["游戏logo", "底部警示语"], "extra": "雪地洁白"}
        texts = {"scene": prompts.compose(style, "scene", row, views=3, look_image=4),
                 "subject": prompts.compose(style, "subject", row, what="猎人", description="蓝色冬装"),
                 "video": prompts.compose(style, "video", row, extra="鱼不要变形", refs="Image 1 is the map.")}
        for kind, text in texts.items():
            self.assertNotIn("{", text, kind)
        self.assertIn("Images 1 to 3", texts["scene"])
        self.assertIn("Image 4", texts["scene"])
        self.assertIn("真实的冰块", texts["scene"])
        self.assertIn("猎人", texts["subject"])
        self.assertIn("游戏logo, 底部警示语", texts["video"])
        self.assertIn("鱼不要变形", texts["video"])
        self.assertIn("Image 1 is the map.", texts["video"])
        self.assertTrue(texts["video"].startswith("Edit the video: replace the materials of the input video frame for frame"))

    def test_timed_removals_only_where_on_screen(self):
        items = ["游戏logo", {"what": "底部警示语", "start": 0, "end": 10}, {"what": "右上角水印", "start": 30}]
        self.assertEqual(prompts.removals(items, (2, 2)), ["游戏logo", "底部警示语"])
        self.assertEqual(prompts.removals(items, (12, 20)), ["游戏logo"])
        self.assertEqual(prompts.removals(items, (25, 31)), ["游戏logo", "右上角水印"])
        self.assertIn("底部警示语 (0-10 s)", prompts.describe(items))

    def test_timed_materials(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": ["积雪地面", {"what": "蓝色皮卡车", "start": 50, "end": 66}], "remove": []}
        self.assertNotIn("皮卡车", prompts.compose(style, "video", row, span=(0, 11)))
        self.assertIn("积雪地面, 蓝色皮卡车", prompts.compose(style, "video", row, span=(48, 60)))
        self.assertIn("积雪地面", prompts.compose(style, "video", {"materials": "积雪地面"}, span=(0, 1)))

    def test_no_removal_clause_when_empty(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.compose(style, "video", {"materials": "x", "remove": []})
        self.assertNotIn("Remove the", text)


class SizeTests(unittest.TestCase):
    def test_target_sizes(self):
        self.assertEqual(assemble.target_size(1080, 1920, "1080p"), (1080, 1920))
        self.assertEqual(assemble.target_size(1920, 1080, "1080p"), (1920, 1080))
        self.assertEqual(assemble.target_size(3840, 2160, "1080p"), (1920, 1080))
        self.assertEqual(assemble.target_size(1080, 1350, "1080p"), (1080, 1350))
        self.assertEqual(assemble.target_size(1080, 1920, "2K"), (1440, 2560))
        self.assertEqual(assemble.target_size(1920, 1080, "2K"), (2560, 1440))

    def test_image_aspects(self):
        self.assertEqual(images.aspect_for(1920, 1080), "16:9")
        self.assertEqual(images.aspect_for(1080, 1920), "9:16")
        self.assertEqual(images.aspect_for(1080, 3000), "9:16")          # the nearest preset (4K needs one)
        self.assertEqual(images.aspect_for(1000, 1000 * 2.1, exact=True), "adaptive")


class SeamTests(unittest.TestCase):
    def moving(self, count=12, seed=1):
        import numpy as np
        rng = np.random.default_rng(seed)
        base = np.kron(rng.uniform(40, 200, (10, 6, 3)), np.ones((6, 6, 1)))    # smooth: small motion per step
        return np.stack([np.roll(base, i, axis=1) for i in range(count)]).astype(np.float32)

    def test_matching_results_are_clean(self):
        before = self.moving()
        self.assertEqual(assemble.measure(before, before + 1.0)["verdict"], "clean")

    def test_different_drawings_are_visible(self):
        self.assertEqual(assemble.measure(self.moving(seed=1), self.moving(seed=2))["verdict"], "visible")

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
    def test_issue_maps_to_segments(self):
        assembled = {"segments": [{"id": "K01", "start": 0, "end": 10}, {"id": "K02", "start": 10, "end": 20}]}
        self.assertEqual(review.segment_for(3, 4, assembled), ["K01"])
        self.assertEqual(review.segment_for(9.9, 10.1, assembled), ["K01", "K02"])
        self.assertEqual(review.segment_for(25, 26, assembled), ["K02"])

    def test_the_agent_sees_five_frames_per_seam_and_a_still_every_three_seconds(self):
        self.assertEqual(review.SEAM_MOMENTS, (-1.0, -.5, 0.0, .5, 1.0))
        self.assertEqual(review.every(10.0, review.STILL_STEP, 24.0), [0.0, 3.0, 6.0, 9.0])


class RedoTests(unittest.TestCase):
    def test_a_segment_goes_back_twice_at_most(self):
        from reskin import render
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            render.bump(job, "S01:K02", "colour jump")
            render.bump(job, "S01:K02", "automatic retry", automatic=True)
            self.assertEqual(render.bump(job, "S01:K02", "still wrong", "雪地为白色")["redos"], 2)
            with self.assertRaises(ValueError):
                render.bump(job, "S01:K02", "third time")
            row = read(job / "config/redo.json")["S01:K02"]
            self.assertEqual(row["attempt"], 4)
            self.assertEqual(row["prompt"], "雪地为白色")

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
            render.bump(job, "S01:K01", "fish too early", "收集框保持空的")
            self.assertNotIn("prompt", render.bump(job, "S01:K01", "camera moved", replace=True))


if __name__ == "__main__":
    unittest.main()


class ChainTests(unittest.TestCase):
    """Segments of one shot render one after the other; each continuing one takes the previous result's frame."""
    ROW = {"segments": [{"id": "K01", "seam": "start"}, {"id": "K02", "seam": "continuous"},
                        {"id": "K03", "seam": "cut"}, {"id": "K04", "seam": "continuous"}]}

    def test_previous_is_the_segment_before_in_the_same_shot(self):
        from reskin import render
        ids = [None if p is None else p["id"] for p in (render.previous_of(self.ROW, s) for s in self.ROW["segments"])]
        self.assertEqual(ids, [None, "K01", None, "K03"])

    def test_the_previous_frame_is_described_first(self):
        from reskin import render
        text = render.references_clause([{"role": "previous"}, {"role": "scene", "name": "camp"}])
        self.assertTrue(text.startswith("Image 1 is the restyled frame where this clip begins"))
        self.assertIn("Image 2 is the whole location", text)

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
