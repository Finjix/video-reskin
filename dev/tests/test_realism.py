"""Realism upkeep: midframes (previews sent to Seedance as references), previews carrying corrective sentences, the
realism timeline of the checks and accepted findings passed on to later checks."""
import tempfile
import unittest
from pathlib import Path

from reskin import preview, render, review
from reskin.common import PROMPTS, read, write


def row(midframes=None):
    fps = 30.0
    segs = [{"id": "K01", "frames": [0, 688], "render_frames": [0, 688], "seam": "start", "seconds": 22.93},
            {"id": "K02", "frames": [688, 1369], "render_frames": [673, 1369], "seam": "continuous", "seconds": 23.2},
            {"id": "K03", "frames": [1369, 1971], "render_frames": [1354, 1971], "seam": "continuous", "seconds": 20.57}]
    return {"fps": fps, "keep": [0.0, 65.7], "segments": segs, "materials": ["积雪地面"], "remove": [],
            "midframes": midframes, "width": 1080, "height": 1920}


class MidframeTests(unittest.TestCase):
    def test_none_by_default(self):
        self.assertEqual(preview.midframes(row(), row()["segments"][2]), [])

    def test_explicit_times_inside_their_segment(self):
        r = row([10.0, 46.0, 50.65, 57.5, 60.68])
        k03 = r["segments"][2]
        # 46.0 is within MIDFRAME_LEAD of K03's first rendered frame (45.13 s)
        self.assertEqual(preview.midframes(r, k03), [50.65, 57.5, 60.68])
        self.assertEqual(preview.midframes(r, r["segments"][0]), [10.0])
        self.assertIn(57.5, preview.moments(r))

    def test_auto_takes_regular_moments_and_caps(self):
        r = row("auto")
        for seg in r["segments"]:
            mids = preview.midframes(r, seg)
            self.assertLessEqual(len(mids), preview.MIDFRAME_LIMIT)
            for t in mids:
                self.assertEqual(preview.segment_at(r, t)["id"], seg["id"])
        r = row([float(t) for t in range(47, 65)])
        self.assertEqual(len(preview.midframes(r, r["segments"][2])), preview.MIDFRAME_LIMIT)

    def test_preview_carries_corrections(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            write(job / "config/redo.json", {"S01:K02": {"attempt": 2, "prompt": "黄鳍金枪鱼保持金黄色。"}})
            text = preview.corrections(job, "S01", row(), 40.0)
            self.assertTrue(text.startswith("这张画面是本段第18秒"))
            self.assertTrue(text.endswith("黄鳍金枪鱼保持金黄色。"))
            self.assertEqual(preview.corrections(job, "S01", row(), 10.0), "")

    def test_video_prompt_names_midframes(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            r = row([50.65, 57.5])
            state = {"sources": [{"id": "S01", "sha256": "x"}], "options": {"resolution": "480p"}}
            write(job / "config/redo.json", {"S01:K03": {"attempt": 2, "prompt": "纠正语句。"}})
            style = read(PROMPTS)["presets"]["realistic"]
            previous = {"generation_id": "g1"}
            with self.assertRaises(ValueError):
                render.inputs_for(job, state, r, "S01", r["segments"][2], style, previous)
            for t in (50.65, 57.5):
                path = preview.image_path(job, "S01", t)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"png")
            inputs = render.inputs_for(job, state, r, "S01", r["segments"][2], style, previous)
            self.assertEqual(inputs["midframes"], [50.65, 57.5])
            self.assertIn("图片2是本段约第6秒", inputs["prompt"])
            self.assertIn("图片3是本段约第12秒", inputs["prompt"])
            self.assertTrue(inputs["prompt"].rstrip().endswith("纠正语句。"))
            plain = render.inputs_for(job, state, row(), "S01", row()["segments"][2], style, previous)
            self.assertNotIn("midframes", plain)
            # adding midframes to a rendered segment counts like a new keyframe
            self.assertEqual(render.cause_of({"digest": "old", "inputs": render.core_inputs(plain)}, inputs, {}),
                             "keyframe")


class RealismTests(unittest.TestCase):
    def test_low_scores_become_issues(self):
        ranges = review.realism_ranges({"realism": [
            {"start": 0, "end": 10, "score": 5, "cartoonish": []},
            {"start": 10, "end": 15, "score": 3, "cartoonish": ["雪堆"]},
            {"start": 15, "end": 22, "score": 2, "cartoonish": ["皮卡车", "顾客"]}]}, offset=45.0)
        self.assertEqual(ranges[1]["start"], 55.0)
        found = review.realism_issues(ranges, [])
        self.assertEqual([i["severity"] for i in found], ["medium", "high"])
        self.assertIn("皮卡车、顾客", found[1]["description"])
        covered = [{"category": "not_realistic", "start": 56.0, "end": 57.0}]
        self.assertEqual(len(review.realism_issues(ranges, covered)), 1)
        self.assertIn("3/5 (雪堆)", review.realism_line(ranges))
        flat = review.realism_ranges({"realism": [{"start": t, "end": t + 10, "score": 3, "cartoonish": ["人物", x]}
                                                  for t, x in ((0, "钞票"), (10, "鱼肉"), (20, "钞票"))]})
        merged = review.realism_issues(flat, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (0.0, 30.0))
        self.assertEqual(merged[0]["where"], "人物、钞票、鱼肉")

    def test_schemas_use_only_string_enums(self):
        # Gemini rejects a responseSchema with enum values that are not strings (Winky error 400).
        from reskin import lint
        def walk(node, path):
            if isinstance(node, dict):
                if "enum" in node:
                    self.assertEqual(node.get("type"), "STRING", path)
                    self.assertTrue(all(isinstance(v, str) for v in node["enum"]), path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{path}[{i}]")
        schemas = {name: value for module in (review, lint) for name, value in vars(module).items()
                   if name.endswith("SCHEMA") and isinstance(value, dict)}
        self.assertIn("SCHEMA", schemas)
        for name, value in schemas.items():
            walk(value, name)

    def test_scores_are_clamped(self):
        ranges = review.realism_ranges({"realism": [{"start": 0, "end": 1, "score": 9, "cartoonish": []},
                                                    {"start": 1, "end": 2, "score": 0, "cartoonish": []}]})
        self.assertEqual([r["score"] for r in ranges], [5, 5])

    def test_accepted_findings_reach_later_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            write(job / "state/render/S01-K01.json", {"generation_id": "g", "gate": {
                "generation_id": "g", "accepted": True, "note": "小色差",
                "issues": [{"severity": "medium", "description": "虚线框变成绿色"},
                           {"severity": "low", "description": "轻微闪烁"}]}})
            write(job / "state/render/S01-K02.json", {"generation_id": "h", "gate": {
                "generation_id": "h", "held": True, "issues": [{"severity": "high", "description": "吊机消失"}]}})
            notes = review.accepted_notes(job, "S01")
            self.assertEqual(notes, ["虚线框变成绿色 (accepted: 小色差)"])
            text = review.segment_prompt({"style": "realistic"}, {"materials": "x", "remove": []}, "left", known=notes)
            self.assertIn("虚线框变成绿色", text)
            self.assertIn("realism", text)


class IdentityTests(unittest.TestCase):
    def test_times_spread_over_the_range(self):
        times = review.identity_times(0.0, 65.7)
        self.assertEqual(len(times), review.IDENTITY_STILLS)
        self.assertLess(times[0], 1.0)
        self.assertGreater(times[-1], 64.0)
        self.assertEqual(len(review.identity_times(0.0, 20.0, 3.0, 8)), 7)

    def test_references_are_the_shot_keyframes(self):
        r = row()
        r["anchor"] = 36.9
        refs = review.identity_references(Path("job"), "S01", r, ["K03"])
        self.assertEqual([label for label, _, _ in refs], ["anchor 36.9 s", "keyframe 0.0 s"])
        self.assertTrue(str(refs[1][2]).endswith("S01-K01.png"))

    def test_closeups_and_answer(self):
        from PIL import Image
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            paths = {}
            for name in ("k-src", "k", "p1-src", "p1", "p2-src", "p2"):
                paths[name] = job / f"{name}.png"
                Image.new("RGB", (540, 960), "white").save(paths[name])
            frames = review.identity_frames([("keyframe 0.0 s", paths["k-src"], paths["k"])], (540, 960),
                                            stills=[("8.0 s", paths["p1-src"], paths["p1"]),
                                                    ("57.5 s midframe", paths["p2-src"], paths["p2"])])
            self.assertEqual([f["label"] for f in frames], ["REF keyframe 0.0 s", "8.0 s", "57.5 s midframe"])
            self.assertEqual(frames[2]["t"], 57.5)
            self.assertEqual(max(frames[0]["original"].size), review.FRAME_SIDE)
            detection = {"subjects": [{"name": "主角", "description": "玩家角色"}, {"name": "皮卡车", "description": "车"}],
                         "frames": [{"index": 1, "boxes": [{"subject": "主角", "box_2d": [400, 300, 500, 360]}]},
                                    {"index": 2, "boxes": [{"subject": "主角", "box_2d": [450, 500, 560, 560]}]},
                                    {"index": 3, "boxes": [{"subject": "主角", "box_2d": [100, 100, 200, 150]},
                                                           {"subject": "皮卡车", "box_2d": [0, 0, 100, 100]},
                                                           {"subject": "路人", "box_2d": [0, 0, 100, 100]}]}]}
            answer = {"pairs": [
                {"pair": 1, "difference": "high", "reference_look": "深蓝色派克大衣、灰色毛领的成年男子",
                 "look": "浅蓝色外套、白色兜帽的Q版小人"},
                {"pair": 2, "difference": "medium", "reference_look": "深蓝色派克大衣", "look": "浅蓝色外套"},
                {"pair": 7, "difference": "high", "reference_look": "?", "look": "?"}]}
            with mock.patch.object(review.winky, "gemini", side_effect=[detection, answer]) as gemini,                     mock.patch.object(review.winky, "inline", return_value={}):
                subjects, issues, sheets = review.identity_check(job, "S01", frames, {"materials": "x"}, "a video",
                                                                 (540, 960), job / "S01-identity")
            self.assertEqual(gemini.call_count, 2)
            self.assertEqual([p.name for p in sheets], ["S01-identity-subjects.jpg", "S01-identity-frames.jpg"])
            self.assertTrue(all(p.is_file() for p in sheets))
            # the protagonist has three close-ups; the truck only one (left out), the unknown subject ignored
            # the protagonist has a REF and two later close-ups (two pairs); the truck shows once (no pair); the
            # unknown subject and the out-of-range pair number are ignored
            self.assertEqual([(s["what"], len(s["columns"])) for s in subjects], [("主角", 2)])
            self.assertEqual(len(issues), 1)
            self.assertEqual((issues[0]["category"], issues[0]["severity"]), ("identity", "high"))
            self.assertIn("深蓝色派克大衣", issues[0]["description"])
            self.assertIn("Q版小人", issues[0]["description"])
        self.assertIn("identity", review.CATEGORIES)
        self.assertIn("the last ones too", review.identity_prompt({"materials": "x"}, "a video", True))

    def test_stretches_from_column_verdicts(self):
        subjects = [{"what": "主角", "reference_look": "成年男子", "columns": [
            {"label": "0.2 s", "difference": "none", "look": "成年男子"},
            {"label": "11.7 s", "difference": "medium", "look": "深灰色"},
            {"label": "23.2 s", "difference": "high", "look": "Q版小人"},
            {"label": "27.1 s", "difference": "high", "look": "Q版小人"},
            {"label": "34.8 s", "difference": "low", "look": "接近"},
            {"label": "50.1 s", "difference": "high", "look": "Q版小人"}]}]
        issues = review.identity_issues(subjects, [])
        self.assertEqual([(i["start"], i["end"], i["severity"]) for i in issues],
                         [(11.7, 27.1, "high"), (50.1, 50.1, "high")])
        sustained = [{"what": "顾客", "reference_look": "x", "columns": [
            {"label": f"{t} s", "difference": "medium", "look": "y"} for t in (10, 20, 30)]}]
        self.assertEqual(review.identity_issues(sustained, [])[0]["severity"], "high")
        self.assertIn("深灰色；Q版小人", issues[0]["description"])

    def test_pair_verdict_from_attributes(self):
        real = {"build": "realistic", "rendering": "photo_real", "colours": "深蓝外套、灰色毛领"}
        chibi = {"build": "chibi", "rendering": "game_cg", "colours": "亮蓝外套、白色毛边"}
        stocky = {"build": "stylized", "rendering": "photo_real", "colours": "深蓝外套"}
        self.assertEqual(review.pair_difference({"left": real, "middle": chibi, "same_colours": False,
                                                 "difference": "low"}), "high")
        # one step in build alone (a heavy parka read as stocky) is low; with other colours it counts
        self.assertEqual(review.pair_difference({"left": real, "middle": stocky, "same_colours": True,
                                                 "difference": "low"}), "low")
        self.assertEqual(review.pair_difference({"left": real, "middle": stocky, "same_colours": False,
                                                 "difference": "low"}), "medium")
        self.assertEqual(review.pair_difference({"left": real, "middle": real, "same_colours": True,
                                                 "difference": "low"}), "low")
        blue = {"build": "not_a_figure", "rendering": "photo_real", "colours": "蓝色"}
        red = {"build": "not_a_figure", "rendering": "photo_real", "colours": "红色"}
        # an upgrade the original makes too: not a difference
        self.assertEqual(review.pair_difference({"left": blue, "middle": red, "same_colours": False,
                                                 "original_changes_too": True, "difference": "medium"}), "low")
        self.assertEqual(review.pair_difference({"left": blue, "middle": red, "same_colours": False,
                                                 "original_changes_too": False, "difference": "low"}), "medium")
        # build still counts when the original changes colour too
        self.assertEqual(review.pair_difference({"left": real, "middle": chibi, "same_colours": False,
                                                 "original_changes_too": True, "difference": "low"}), "high")
        crane = {"build": "not_a_figure", "rendering": "photo_real", "colours": "红色"}
        self.assertEqual(review.pair_difference({"left": crane, "middle": crane, "same_colours": True,
                                                 "difference": "none"}), "none")

    def test_closeup_is_a_square_around_the_box(self):
        from PIL import Image
        tile = review.closeup(Image.new("RGB", (480, 854)), [.4, .4, .45, .48], side=150)
        self.assertEqual(tile.size, (150, 150))

class WipeTests(unittest.TestCase):
    def test_boxes_for_the_clip(self):
        r = {"remove": ["底部警示语", {"what": "右上角logo", "start": 0, "end": 16.5, "box": [0.7, 0.08, 0.97, 0.15],
                                       "fill": "mirror"},
                        {"what": "左下角logo", "start": 37.5, "end": 55.2, "box": [0.03, 0.85, 0.3, 0.92],
                         "fill": "mirror"}]}
        self.assertEqual(render.clean_boxes(r, 10.0, 30.0), [[0.7, 0.08, 0.97, 0.15, 0.0, 6.5]])
        self.assertEqual(render.clean_boxes(r, 30.0, 50.0), [[0.03, 0.85, 0.3, 0.92, 7.5, 20.0]])
        graph, label = render.wipe_graph([[0.0, 0.0, 1.0, 0.05, 0.0, 6.5], [0.1, 0.93, 0.9, 1.0, 2.0, 3.0]], 1080, 1920)
        self.assertEqual(label, "w1")
        self.assertIn("crop=1080:118:0:118,vflip", graph)        # top box (grown by the feather): from below
        self.assertIn("crop=908:156:86:1608,vflip", graph)       # bottom box: from above
        import numpy as np
        frame = np.full((1920, 1080, 3), 200, dtype=np.uint8)
        frame[1450:1920, 760:1080] = np.random.default_rng(1).integers(0, 255, (470, 320, 3))   # busy fence above and below
        chosen = render.wipe_sources([[0.71, 0.85, 0.97, 0.915, 0, 1]], 1080, 1920, frame)
        self.assertEqual(chosen[0][2], "hflip")                    # the plain snow beside it wins
        self.assertIn("overlay=86:1764:enable='between(t,2.000,3.000)'", graph)
        with_masks, _ = render.wipe_graph([[0.0, 0.0, 1.0, 0.05, 0.0, 6.5]], 1080, 1920, masks=[1])
        self.assertIn("[1:v]format=gray[w0m];[w0p][w0m]alphamerge[w0q]", with_masks)
        # a rounding sliver of overlap does not count
        self.assertEqual(render.clean_boxes({"remove": [{"what": "x", "start": 10.33, "end": 24.5,
                                                         "box": [0.7, 0.9, 1, 1]}]}, 0.0, 10.333), [])

    def test_glyph_boxes_carry_their_mask(self):
        r = {"remove": [{"what": "右上角logo", "start": 0, "end": 16.5, "box": [0.7, 0.08, 0.97, 0.15]},
                        {"what": "左下角logo", "start": 0, "end": 9, "box": [0.03, 0.85, 0.3, 0.92], "fill": "mirror"}]}
        logos = {0: {"hash": "abc123", "path": "m.png", "region": [0, 0, 10, 10]}}
        self.assertEqual(render.clean_boxes(r, 0.0, 10.0, logos),
                         [[0.7, 0.08, 0.97, 0.15, 0.0, 10.0, "glyph:abc123"], [0.03, 0.85, 0.3, 0.92, 0.0, 9.0]])
        with self.assertRaises(ValueError):          # a letter-by-letter wipe never runs without its mask
            render.clean_boxes(r, 0.0, 10.0, {})
        from reskin import plan
        self.assertIsNone(plan.items_problem(r["remove"], "remove"))
        self.assertIsNotNone(plan.items_problem([{"what": "x", "box": [0, 0, .2, .2], "fill": "blur"}], "remove"))

    def test_logo_mask_is_the_still_letters(self):
        """A static logo (two white bars) over a scene that pans: the mask covers the bars, not the scene."""
        import subprocess
        import numpy as np
        from reskin import logos
        from reskin.common import FFMPEG
        if not Path(FFMPEG).is_file():
            self.skipTest("no FFmpeg in the runtime cache")
        rng = np.random.default_rng(3)
        world = (rng.integers(0, 255, (100, 60, 3)).repeat(8, 0).repeat(8, 1)).astype(np.uint8)
        frames = []
        for k in range(40):
            frame = world[k * 4:k * 4 + 480, k * 3:k * 3 + 270].copy()
            frame[400:410, 20:120] = 255
            frame[440:450, 20:120] = 255
            frames.append(frame)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "logo.mp4"
            subprocess.run([str(FFMPEG), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "270x480",
                            "-r", "30", "-i", "-", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv444p", str(source)],
                           input=np.stack(frames).tobytes(), check=True)
            box = [10 / 270, 390 / 480, 130 / 270, 460 / 480]
            soft = logos.measure(source, box, 0.0, 1.3, (270, 480))
        x0, y0, _, _ = logos.region(box, (270, 480))
        bars = soft[400 - y0:410 - y0, 30 - x0:110 - x0]
        scene = soft[90 - 24 + 0:90 - 24 + 6, 0:6] if soft.shape[0] > 90 else soft[:4, :4]
        self.assertGreater(float(bars.mean()), .9)
        self.assertLess(float(scene.mean()), .1)
        # the gap between the bars is scene that passes by: left alone apart from the grown edge
        self.assertLess(float(soft[424 - y0:426 - y0, 30 - x0:110 - x0].mean()), .5)

    def test_pushpull_fills_from_around(self):
        import numpy as np
        from reskin import logos
        values = np.zeros((40, 60, 3), np.float32)
        values[:, :30] = (200, 40, 40)
        values[:, 30:] = (40, 40, 200)
        known = np.ones((40, 60), np.float32)
        known[15:25, 10:20] = 0                 # a hole in the red half
        filled = logos.pushpull(values * known[..., None], known)
        self.assertGreater(float(filled[20, 15, 0]), 150)          # red comes in from around, not black
        self.assertLess(float(filled[20, 15, 2]), 90)
        frame = np.zeros((40, 60, 3), np.uint8)
        frame[:] = (200, 40, 40)
        frame[18:22, 12:18] = 255                                   # a white letter
        soft = np.zeros((40, 60), np.float32)
        soft[17:23, 11:19] = 1
        logos.wipe(frame, [0, 0, 60, 40], soft)
        self.assertLess(int(frame[20, 15, 1]), 90)                   # the letter is gone, the red is back

    def test_wipe_check_moments(self):
        from reskin import review
        times = review.wipe_times(16.5, 36.67)
        self.assertEqual(len(times), review.WIPE_MOMENTS)
        self.assertTrue(16.5 < times[0] < times[-1] < 36.67)
        self.assertEqual(review.wipe_times(5.0, 5.1), [])              # a sliver has no moments
        self.assertIn("upside_down", review.WIPE_SCHEMA["properties"]["problems"]["items"]["properties"]["problem"]["enum"])
        text = review.wipe_prompt(3)
        self.assertIn("These 3 sheets", text)
        self.assertTrue(set(review.WIPE_STRICT) <= set(review.WIPE_PROBLEMS))

    def test_wipe_check_merges_neighbouring_moments(self):
        from unittest import mock
        from reskin import review, winky, ledger
        sheets = [{"sheet": "a.jpg", "what": "底部警示语", "times": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], "box": [0, .9, 1, 1]}]
        answer = {"problems": [{"sheet": 1, "column": c, "problem": "smear", "severity": "low", "description": "糊"}
                               for c in (1, 2, 3, 6)]
                  + [{"sheet": 1, "column": 4, "problem": "upside_down", "severity": "low", "description": "倒"},
                     {"sheet": 2, "column": 1, "problem": "smear", "severity": "low", "description": "不存在的图"}]}
        with mock.patch.object(review, "wipe_sheets", return_value=sheets), \
                mock.patch.object(winky, "inline", return_value={}), \
                mock.patch.object(winky, "gemini", return_value=answer), \
                mock.patch.object(ledger, "add"):
            issues, _ = review.wipe_check(Path("."), "S01", {}, {}, "src.mp4", Path("x"))
        spans = sorted((i["start"], i["end"], i["severity"]) for i in issues)
        self.assertEqual(spans, [(0.7, 3.3, "low"), (3.7, 4.3, "high"), (5.7, 6.3, "low")])
        self.assertIn("3 个时刻", next(i for i in issues if i["start"] == 0.7)["description"])

    def test_glyph_wiped_clip_renders(self):
        import subprocess
        import numpy as np
        from PIL import Image
        from reskin.common import FFMPEG
        if not Path(FFMPEG).is_file():
            self.skipTest("no FFmpeg in the runtime cache")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "src.mp4"
            subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30",
                            "-t", "2", "-pix_fmt", "yuv420p", str(source)], check=True)
            mask = Path(folder) / "mask.png"
            Image.fromarray(np.full((40, 60), 255, np.uint8)).save(mask)
            logos = {0: {"hash": "h1", "path": str(mask), "region": [10, 400, 70, 440]}}
            out = render.cut_clip(source, [0, 45], 30.0, 270, 480, Path(folder) / "clip.mp4",
                                  clean=[[0.7, 0.08, 0.97, 0.15, 0.0, 1.0], [0.0, 0.85, 0.3, 0.95, 0.0, 1.5, "glyph:h1"]],
                                  logos=logos)
            self.assertTrue(out.is_file() and out.stat().st_size > 0)
            from reskin.common import video_info
            self.assertEqual(round(video_info(out)["duration"] * 30), 45)

    def test_wiped_clip_renders(self):
        import subprocess
        from reskin.common import FFMPEG
        if not Path(FFMPEG).is_file():
            self.skipTest("no FFmpeg in the runtime cache")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "src.mp4"
            subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30",
                            "-t", "2", "-pix_fmt", "yuv420p", str(source)], check=True)
            out = render.cut_clip(source, [0, 45], 30.0, 270, 480, Path(folder) / "clip.mp4",
                                  clean=[[0.7, 0.08, 0.97, 0.15, 0.0, 1.0], [0.0, 0.9, 1.0, 1.0, 0.5, 1.5]])
            self.assertTrue(out.is_file() and out.stat().st_size > 0)

    def test_after_only_boxes_do_not_touch_the_render(self):
        r = {"remove": [{"what": "logo", "start": 0, "end": 15, "box": [0, 0, .27, .05], "wipe": "after"}]}
        self.assertEqual(render.clean_boxes(r, 0.0, 28.0), [])
        from reskin import plan
        self.assertIsNone(plan.items_problem(r["remove"], "remove"))
        self.assertIsNotNone(plan.items_problem([{"what": "x", "box": [0, 0, .2, .2], "wipe": "never"}], "remove"))

    def test_after_items_stay_out_of_prompts(self):
        from reskin import prompts
        style = read(PROMPTS)["presets"]["realistic"]
        r = {"materials": "x", "remove": [{"what": "角落logo甲", "box": [0, 0, .2, .1], "wipe": "after"},
                                          {"what": "底部警示语乙", "box": [0, .9, 1, 1]}]}
        text = prompts.compose(style, "video", r)
        self.assertNotIn("角落logo甲", text)      # wiped by the program: never named
        self.assertNotIn("底部警示语乙", text)
        r["remove"].append("右上角水印丙")
        self.assertIn("右上角水印丙", prompts.compose(style, "video", r))      # no box: the model is told

    def test_feather_mask_is_opaque_at_the_frame_border(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path = render.feather_mask(86, 1764, 908, 156, 1080, 1920, Path(folder) / "m.png")
            with Image.open(path) as image:
                mask = np.asarray(image)
        self.assertEqual(int(mask[-1, mask.shape[1] // 2]), 255)    # bottom edge on the frame border: opaque
        self.assertEqual(int(mask[mask.shape[0] // 2, mask.shape[1] // 2]), 255)
        self.assertLess(int(mask[0, mask.shape[1] // 2]), 60)          # top edge into the scene: soft
        self.assertLess(int(mask[mask.shape[0] // 2, 0]), 60)

    def test_remove_box_is_validated(self):
        from reskin import plan
        self.assertIsNone(plan.items_problem([{"what": "logo", "start": 0, "end": 5, "box": [0, 0, .3, .05]}], "remove"))
        self.assertIsNotNone(plan.items_problem([{"what": "logo", "box": [.5, 0, .3, .05]}], "remove"))
        self.assertIsNotNone(plan.items_problem([{"what": "鱼", "box": [0, 0, .3, .05]}], "materials"))


if __name__ == "__main__":
    unittest.main()
