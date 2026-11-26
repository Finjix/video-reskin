"""Native-clock editing, review and publication with real media and a simulated Seedance service."""
import shutil
import re
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from reskin import element_seedance, elements, job, lumos, net, pipeline
from reskin.common import FFMPEG, probe, read, run, sha, video_info, write


class ElementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.original = self.root / "original.mp4"
        self.reference = self.root / "crate.png"
        pipeline.ff(["-f", "lavfi", "-i", "color=black:s=64x48:r=30:d=1", "-f", "lavfi", "-i",
                     "sine=frequency=330:duration=1", "-c:v", "libx264", "-c:a", "aac"], self.original)
        Image.new("RGB", (64, 48), "blue").save(self.reference)
        self.order = {"style": "elements", "sources": [str(self.original)], "output_root": str(self.root / "delivery"),
                      "variants": [
                          self.variant("蓝箱", "blue case", [[3, 6], [10, 20]], "BLUE_CASE"),
                          self.variant("红箱.mp4", "red case", [[2, 15]], "RED_CASE")]}
        self.folder, self.state = job.init(self.order, self.root / "run" / "temp")
        self.requests = {}
        self.enterContext(patch.object(lumos, "upload", side_effect=self.upload))
        self.submit = self.enterContext(patch.object(lumos, "submit_video_edit", side_effect=self.submit_generation))
        self.enterContext(patch.object(lumos, "wait", side_effect=self.wait_generation))
        self.enterContext(patch.object(net, "download", side_effect=self.download_generation))

    def variant(self, name, replacement, ranges, prompt):
        return {"name": name, "element": "crate", "replacement": replacement, "ranges": ranges,
                "seedance": {"segments": [self.segment(span, prompt) for span in ranges]}}

    def segment(self, span, prompt):
        return {"range": span, "references": [str(self.reference)], "prompt": prompt}

    def upload(self, path, generation_type):
        path = Path(path)
        self.assertTrue(path.is_file())
        return {"media_id": str(path), "media_path": str(path), "media_url": path.as_uri(), "reference_type": "BASE"}

    def submit_generation(self, video, images, prompt, **kwargs):
        self.assertTrue(images)
        generation_id = f"generation-{len(self.requests) + 1}"
        self.requests[generation_id] = (video["media_path"], prompt)
        return {"generation_id": generation_id, "status": "Pending"}

    def wait_generation(self, generation_id, **kwargs):
        return {"generation_id": generation_id, "status": "Succeeded",
                "generation_config": {"model": lumos.VIDEO_MODEL, "generation_mode": "video_edit"},
                "generations": {"media_result": [{"media_url": "https://test.invalid/" + generation_id}]}}

    def download_generation(self, url, destination, **kwargs):
        # Only the service is simulated. Upload clips, native-clock cuts, audio, previews and delivery are real.
        uploaded, prompt = self.requests[url.rsplit("/", 1)[-1]]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "BOTH_OBJECTS" in prompt or "LEFT_ONLY" in prompt:
            filters = "drawbox=x=0:y=0:w=iw/2:h=ih:c=red:t=fill"
            if "BOTH_OBJECTS" in prompt:
                interval = re.search(r"([\d.]+)s to ([\d.]+)s: BOTH_OBJECTS", prompt)
                # The renderer samples the 24 fps model picture nearest each native source frame.
                enabled = f":enable='gte(t,{float(interval[1]) - 0.5 / 24})*lt(t,{interval[2]})'" if interval else ""
                filters += ",drawbox=x=iw/2:y=0:w=iw/2:h=ih:c=blue:t=fill" + enabled
        else:
            color = "blue" if "BLUE_CASE" in prompt else "red"
            filters = f"drawbox=x=0:y=0:w=iw:h=ih:c={color}:t=fill"
        pipeline.ff(["-i", uploaded, "-vf", filters, "-an", "-c:v", "libx264"], destination)
        return destination

    def action(self, cmd, index=0, at=None):
        return elements.command(cmd, self.folder, self.state, self.state["variants"][index], at)

    def plan(self, variants):
        args = Namespace(command="plan", only=None, segment=None, input=write(self.root / "plan.json",
                         {"variants": variants}), accept=None, covers=[], reason=None, redo=False, generation_id=None, at=None)
        output = StringIO()
        with redirect_stdout(output):
            elements.dispatch(args, self.folder, self.state)
        self.state = job.load(self.folder)
        return output.getvalue()

    @staticmethod
    def pixels(path):
        return run([FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0", "-pix_fmt", "rgb24",
                    "-f", "rawvideo", "-"], binary=True)

    @staticmethod
    def pixel(rgb, width, height, frame, x, y):
        pos = ((frame * height + y) * width + x) * 3
        return rgb[pos:pos + 3]

    def test_model_segments_native_frame_ranges_original_audio_names_and_approval(self):
        for index in range(2):
            with self.subTest(index=index):
                variant = self.state["variants"][index]
                receipt = self.action("render", index)
                self.assertEqual([g["range"] for g in receipt["generations"]], [[0, 30]])
                for generation in receipt["generations"]:
                    self.assertEqual(generation["model"], lumos.VIDEO_MODEL)
                    self.assertIn(generation["generation_id"], self.requests)
                    self.assertEqual(len(generation["raw_sha256"]), 64)
                with self.assertRaisesRegex(ValueError, "approve"):
                    self.action("assemble", index)
                with self.assertRaisesRegex(ValueError, "check"):
                    self.action("approve", index)
                self.assertEqual(self.action("check", index, at=0.2)["frames"], [6])
                report = self.action("check", index)
                self.assertTrue(all(Path(p).is_file() for p in report["sheets"]))
                self.assertIn(29, report["frames"])
                self.assertEqual([r["frames"] for r in report["motion_clips"].values()], [[0, 30]])
                self.action("approve", index)
                delivery = Path(self.action("assemble", index))
                self.assertEqual(delivery.parent, Path(self.order["output_root"]).resolve())
                self.assertEqual(delivery.name, ("蓝箱.mp4", "红箱.mp4")[index])
                self.assertEqual(self.action("assemble", index), str(delivery))
                self.assertEqual(elements.packet_times(delivery), elements.packet_times(self.original))
                audio = ["-map", "0:a:0", "-c", "copy", "-f", "hash", "-"]
                self.assertEqual(run([FFMPEG, "-v", "error", "-i", delivery, *audio]),
                                 run([FFMPEG, "-v", "error", "-i", self.original, *audio]))
                rgb = self.pixels(delivery)
                for frame in range(30):
                    edited = any(a <= frame < b for a, b in variant["ranges"])
                    channel = 2 if index == 0 else 0
                    self.assertEqual(self.pixel(rgb, 64, 48, frame, 20, 10)[channel] > 180, edited, frame)

    def test_protect_cash_and_mix_frozen_and_non_frozen_designs(self):
        first, second = [dict(v) for v in self.order["variants"]]
        for target in ("角色背负的绿钞票", "车旁的纸币", "bundles of banknotes"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "banknotes"):
                self.plan([{**first, "element": target}])
        self.plan([{**first, "element": "钞票旁边的车斗", "replacement": "覆冰的六棱冻木"},
                   {**second, "element": "cash register", "replacement": "engraved brass till"}])
        first["replacement"] = "有透明冰壳的冻木"
        second["replacement"] = "frozen beef slabs"
        self.plan([first])
        with self.assertRaisesRegex(ValueError, "non-frozen"):
            self.plan([first, second])
        # A stored plan written before cash became protected must also stop before it can submit or publish.
        self.state["variants"][0]["element"] = "green paper money"
        for command in ("render", "approve", "assemble"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "banknotes"):
                self.action(command)
        self.submit.assert_not_called()

    def test_lumos_reference_generation_and_reuse(self):
        variant = self.order["variants"][0]
        stage = variant["seedance"]["segments"][0]
        del stage["references"]
        stage.update(reference_frame=4, reference_prompt="Show the blue case.")
        self.plan([variant])
        with patch.object(lumos, "submit_image_to_image", return_value={"generation_id": "image-1"}) as submit, \
             patch.object(lumos, "wait", return_value={}), patch.object(lumos, "result_urls", return_value=["url"]), \
             patch.object(net, "download", side_effect=lambda url, path: shutil.copyfile(self.reference, path)):
            refs = self.action("refs")["references"]
            self.assertTrue(all(Path(p).is_file() for p in refs))
            self.action("refs")
            submit.assert_called_once()
            self.assertEqual(submit.call_args.kwargs["model"], pipeline.IMAGE_MODEL)
            self.assertIn("Show the blue case.", submit.call_args.args[1])
        self.submit.assert_not_called()

    def test_many_action_notes_use_the_same_semantic_cuts_as_style_edits(self):
        times = [i / 30 for i in range(2101)]
        survey = {"non_game": [{"start": 7, "end": 9}], "end_card_start": 65,
                  "cuts": [{"time": 34, "kind": "hard_cut"}, {"time": 48, "kind": "action"}]}
        spans = [[0, 210], [270, 1950]]
        stages = [self.segment([a, min(a + 17, end)], f"stage at {a}") for start, end in spans
                  for a in range(start, end, 17)]
        planned = element_seedance.plan({"stages": stages}, spans, "V01", times, survey)["segments"]
        expected = pipeline.build_plan({"frames": 70 * 24}, survey)["segments"]
        self.assertEqual([row["range"] for row in planned],
                         [[round(x * 30 / 24) for x in s["frames"]] for s in expected])
        self.assertLess(len(planned), len(stages))
        self.assertTrue(all(len(s["references"]) == 1 for s in planned))

    def test_protected_delivery_and_reference_changes(self):
        self.action("render")
        self.action("check")
        self.action("approve")
        output = Path(self.state["delivery_dir"]) / "蓝箱.mp4"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b"unrelated")
        with self.assertRaisesRegex(FileExistsError, "already occupied"):
            self.action("assemble")
        self.assertEqual(output.read_bytes(), b"unrelated")
        Image.new("RGB", (64, 48), "green").save(self.reference)
        with self.assertRaises(ValueError):
            self.action("assemble")

    def test_conflicting_variant_names_and_paths(self):
        for name in ("../unsafe", "蓝箱.MP4", "con", "NUL.mp4"):
            with self.subTest(name=name):
                self.order["variants"][1]["name"] = name
                with self.assertRaises(ValueError):
                    elements.variants_from_order(self.order, self.state["sources"], self.root / "delivery")

    def test_replanning_one_variant_preserves_sibling_and_blocks_stale_publication(self):
        for index in range(2):
            self.action("render", index)
            self.action("check", index)
            self.action("approve", index)
        before = self.state
        self.order["variants"][0] = self.variant("蓝箱", "blue case", [[7, 16]], "BLUE_CASE")
        self.plan(self.order["variants"])
        with self.assertRaises(ValueError):
            elements.command("assemble", self.folder, before, before["variants"][0])
        with self.assertRaises(ValueError):
            self.action("assemble", 0)
        self.assertTrue(Path(self.action("assemble", 1)).is_file())

    def test_target_selection_and_named_motion_without_regeneration(self):
        before = self.action("render")
        self.action("check")
        self.action("approve")
        selection = {"reason": "The crate crosses the center repeatedly. The furnace stays at the edge.",
                     "frames": {"peak": 12, "early": 1, "repeat": [16, 18]}}
        variant = self.order["variants"][0]
        variant.update(target_selection=selection, review_frames=[24], motion_review={"crossing": [7, 13]})
        self.assertIn(selection["reason"], self.plan(self.order["variants"]))
        self.assertFalse(self.action("status")["approved"])
        self.assertTrue(self.action("status")["rendered"])
        num_calls = self.submit.call_count
        self.assertEqual(before, self.action("render"))
        self.assertEqual(num_calls, self.submit.call_count)
        page = self.action("check")
        self.assertEqual(page["target_selection"], selection)
        self.assertEqual(page["frames"][:4], [12, 1, 16, 18])
        self.assertIn(24, page["frames"])
        self.assertEqual(len(page["frames"]), len(set(page["frames"])))
        with Image.open(page["sheets"][0]) as sheet:
            self.assertGreater(sheet.getpixel((350, 50))[2], 200)
            self.assertLess(sheet.getpixel((1010, 50))[2], 30)
        clip = Path(page["motion_clips"]["crossing"]["video"])
        self.assertEqual([r["frames"] for r in page["motion_clips"].values()], [[0, 7], [7, 13], [13, 30]])
        self.assertEqual(video_info(clip)["frames"], 6)
        self.action("approve")
        clip.write_bytes(b"stale review media")
        with self.assertRaisesRegex(ValueError, "check"):
            self.action("approve")
        self.assertFalse(self.action("check")["approved"])
        self.assertEqual(video_info(clip)["frames"], 6)

    def test_multiple_targets_in_one_seedance_delivery_with_overlaps_and_gaps(self):
        variant = {"name": "two-targets", "ranges": [[3, 11]],
                   "targets": [{"element": "left object", "replacement": "red case", "ranges": [[3, 11], [14, 18]],
                                "target_selection": {"frames": {"clear": 4}}},
                               {"element": "right object", "replacement": "blue pot",
                                "target_selection": {"frames": {"clear": 9}}}],
                   "seedance": {"segments": [self.segment([3, 11], "BOTH_OBJECTS"), self.segment([14, 18], "LEFT_ONLY")]},
                   "motion_review": {"交叠与退场": [2, 12], "第二次出现": [13, 19]}}
        self.plan([variant])
        spec = self.state["variants"][0]
        self.assertEqual(spec["ranges"], [[3, 11], [14, 18]])
        self.assertEqual(spec["targets"][1]["ranges"], [[3, 11]])
        receipt = self.action("render")
        self.assertEqual(len(receipt["generations"]), 1)
        report = self.action("check")
        self.assertEqual(report["targets"], spec["targets"])
        self.assertEqual(report["frames"][:2], [4, 9])
        self.assertEqual(read(self.folder / "elements/V01/check.json")["motion_clips"], report["motion_clips"])
        with patch.object(elements.review, "motion_clip", side_effect=AssertionError):
            self.assertEqual(self.action("check")["motion_clips"], report["motion_clips"])
        self.action("approve")
        delivery = Path(self.action("assemble"))
        self.assertEqual(list(delivery.parent.glob("*.mp4")), [delivery])
        rgb = self.pixels(delivery)
        for n in range(30):
            self.assertEqual(self.pixel(rgb, 64, 48, n, 8, 24)[0] > 128, 3 <= n < 11 or 14 <= n < 18)
            self.assertEqual(self.pixel(rgb, 64, 48, n, 53, 24)[2] > 128, 3 <= n < 11)
        for clip in report["motion_clips"].values():
            first, end = clip["frames"]
            media = Path(clip["video"])
            self.assertEqual(clip["sha256"], sha(media))
            self.assertEqual(video_info(media)["frames"], end - first)
            actual = self.pixels(media)
            for i, n in enumerate(range(first, end)):
                self.assertLess(max(self.pixel(actual, 128, 72, i, 8, 48)), 30)
                self.assertEqual(self.pixel(actual, 128, 72, i, 72, 48)[0] > 128, 3 <= n < 11 or 14 <= n < 18)
                self.assertEqual(self.pixel(actual, 128, 72, i, 117, 48)[2] > 128, 3 <= n < 11)

    def test_distinct_replacements_across_variants_including_grouped_targets(self):
        first, second = self.order["variants"]
        first["targets"] = [{"element": "box", "replacement": "新形状", "replacement_key": "Ｃool Box"},
                            {"element": "cart", "replacement": "Steel Tray"}]
        for name, key in (("别的描述", "coolbox"), ("steel   ＴＲＡＹ", None), ("CoolBox", None)):
            with self.subTest(name=name):
                second["replacement"] = name
                if key:
                    second["replacement_key"] = key
                else:
                    second.pop("replacement_key", None)
                with self.assertRaisesRegex(ValueError, "repeats V01"):
                    self.plan(self.order["variants"])
        second["replacement"] = "distinct basket"
        self.plan(self.order["variants"])
        self.assertEqual(len(self.state["variants"]), 2)

    def test_motion_intervals_use_valid_native_bounds(self):
        variant = self.order["variants"][0]
        for invalid in ({"action": [9, 9]}, {"action": [-1, 4]}, {"": [0, 5]}, [0, 5]):
            with self.subTest(invalid=invalid):
                variant["motion_review"] = invalid
                with self.assertRaisesRegex(ValueError, "motion_review"):
                    self.plan(self.order["variants"])
        variant["motion_review"] = {"past end": [24, 31]}
        self.plan(self.order["variants"])
        self.action("render")
        with self.assertRaisesRegex(ValueError, "exceeds source"):
            self.action("check")

    def test_legacy_local_outputs_remain_inspectable_without_new_production(self):
        # Simulate a previously rendered work order, including a now-disallowed repeated outcome.
        for spec in self.state["variants"]:
            spec.pop("seedance")
            spec["candidate"] = str(self.root / "archived-candidate.mp4")
            spec["replacement"] = "blue case"
        shutil.copy2(self.original, self.root / "archived-candidate.mp4")
        variant = self.state["variants"][0]
        write(self.folder / job.STATE, self.state)
        self.state = job.load(self.folder)
        folder = self.folder / "elements/V01"
        folder.mkdir(parents=True, exist_ok=True)
        render = folder / "render.mp4"
        shutil.copy2(self.original, render)
        write(folder / "render.json", {"key": "historical-render-receipt", "sha256": sha(render), "video": str(render)})
        self.action("status")
        self.assertTrue(self.action("check")["sheets"])
        for action in ("render", "approve", "assemble"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "(?i)seedance|legacy|histor"):
                self.action(action)
        self.assertFalse(self.submit.called)
        with self.assertRaisesRegex(ValueError, "(?i)seedance|local"):
            self.plan([variant])

    def test_source_first_survey_native_clock_and_full_vfr_motion_hold(self):
        pipeline.ff(["-f", "lavfi", "-i", "testsrc2=s=64x48:r=30:d=1", "-f", "lavfi", "-i",
                     "sine=frequency=330:duration=1", "-vf", r"select=not(between(n\,4\,10)),setpts=PTS+0.1/TB",
                     "-fps_mode:v", "passthrough", "-c:v", "libx264", "-c:a", "aac"], self.original)
        order = dict(self.order)
        del order["variants"]
        folder, state = job.init(order, self.root / "survey-first" / "temp")
        self.assertEqual(state["variants"], [])
        offset = float(probe(self.original)["streams"][0]["start_time"])
        self.assertGreater(offset, 0)
        times = elements.playback_times(self.original)
        self.assertAlmostEqual(times[0], offset, delta=.001)
        self.assertAlmostEqual(times[4] - times[3], 8 / 30, delta=.001)
        intervals = {"irregular": [1, 4], "occlusion": [1, 4], "end": [len(times) - 2, len(times)]}
        with patch.object(elements.review, "motion_clip", wraps=elements.review.motion_clip) as make_clip:
            clips = elements.motion_evidence(self.original, self.original, intervals, times, video_info(self.original),
                                             folder, {})
        self.assertEqual(make_clip.call_count, 2)
        self.assertEqual(clips["irregular"], clips["occlusion"])
        for evidence in clips.values():
            start, end = evidence["frames"]
            got = elements.packet_times(evidence["video"])
            self.assertEqual(len(got), end - start)
            for expected, actual in zip(times[start:end], got):
                self.assertAlmostEqual(expected - times[start], actual, delta=.001)
            until = times[end] if end < len(times) else times[0] + video_info(self.original)["duration"]
            self.assertAlmostEqual(video_info(evidence["video"])["duration"], until - times[start], delta=.001)


if __name__ == "__main__":
    unittest.main()
