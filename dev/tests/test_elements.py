"""Run the independent element-variant workflow on actual source/asset media."""
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from reskin import elements, job, pipeline
from reskin.common import FFMPEG, probe, read, run, sha, video_info, write


class ElementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.original = self.root / "original.mp4"
        self.edited = self.root / "blue.mp4"
        self.sprite = self.root / "sprite.png"
        pipeline.ff(["-f", "lavfi", "-i", "color=black:s=64x48:r=30:d=1", "-f", "lavfi", "-i",
                     "sine=frequency=330:duration=1", "-c:v", "libx264", "-c:a", "aac"], self.original)
        pipeline.ff(["-f", "lavfi", "-i", "color=blue:s=64x48:r=30:d=1", "-c:v", "libx264"], self.edited)
        Image.new("RGBA", (8, 8), "red").save(self.sprite)
        self.order = {"style": "elements", "sources": [str(self.original)], "output_root": str(self.root / "delivery"),
                      "variants": [
                          {"name": "蓝箱", "element": "box", "replacement": "blue box", "candidate": str(self.edited),
                           "ranges": [[3, 6], [10, 20]]},
                          {"name": "红箱.mp4", "element": "box", "replacement": "red box", "ranges": [[2, 15]],
                           "overlay": {"image": str(self.sprite), "width": 8, "height": 8,
                                       "track": [[2, 0, 0], [14, 48, 12]]}}]}
        self.folder, self.state = job.init(self.order, self.root / "run" / "temp")

    def action(self, cmd, index=0, at=None):
        return elements.command(cmd, self.folder, self.state, self.state["variants"][index], at)

    def test_native_frame_range_overlays_review_delivery_and_audio_copy(self):
        for i in range(2):
            self.action("render", i)
            with self.assertRaisesRegex(ValueError, "approve"):
                self.action("assemble", i)
            with self.assertRaisesRegex(ValueError, "check"):
                self.action("approve", i)
            page = self.action("check", i, at=0.2)
            self.assertEqual(page["frames"], [6])
            report = self.action("check", i)
            self.assertTrue(all(Path(p).is_file() for p in report["sheets"]))
            self.assertIn(29, report["frames"])
            self.action("approve", i)
            delivery = Path(self.action("assemble", i))
            self.assertEqual(delivery.parent, Path(self.order["output_root"]).resolve())
            self.assertEqual(delivery.name, ("蓝箱.mp4", "红箱.mp4")[i])
            self.assertEqual(video_info(delivery)["frames"], 30)
            self.assertEqual(self.action("assemble", i), str(delivery))
            audio = ["-map", "0:a:0", "-c", "copy", "-f", "hash", "-"]
            self.assertEqual(run([FFMPEG, "-v", "error", "-i", delivery, *audio]),
                             run([FFMPEG, "-v", "error", "-i", self.original, *audio]))
            rgb = run([FFMPEG, "-v", "error", "-i", delivery, "-map", "0:v:0", "-pix_fmt", "rgb24",
                       "-f", "rawvideo", "-"], binary=True)
            def pixel(frame, x, y):
                start = ((frame * 48 + y) * 64 + x) * 3
                return tuple(rgb[start:start + 3])
            for frame in range(30):
                if i == 0:
                    self.assertEqual(pixel(frame, 2, 2)[2] > 180, 3 <= frame < 6 or 10 <= frame < 20, frame)
                elif 2 <= frame < 15:
                    x, y = (frame - 2) * 4 + 4, (frame - 2) + 4
                    self.assertGreater(pixel(frame, x, y)[0], 185, (frame, x, y))
                    red_columns = [column for column in range(64) if pixel(frame, column, y)[0] > 160]
                    self.assertAlmostEqual(min(red_columns), x - 4, delta=1)
                    self.assertAlmostEqual(max(red_columns), x + 3, delta=1)
                else:
                    self.assertLess(max(pixel(frame, 2, 2)), 10)
        self.assertEqual(sha(self.original), self.state["sources"][0]["sha256"])
        Image.new("RGBA", (8, 8), "green").save(self.sprite)
        with self.assertRaisesRegex(ValueError, "render"):
            self.action("assemble", 1)
        self.action("render", 1)
        with self.assertRaisesRegex(ValueError, "approve"):
            self.action("assemble", 1)

    def test_wrong_clock_and_protected_delivery(self):
        self.action("render")
        self.action("check")
        self.action("approve")
        output = Path(self.state["delivery_dir"]) / "蓝箱.mp4"
        output.parent.mkdir()
        output.write_bytes(b"unrelated")
        with self.assertRaisesRegex(FileExistsError, "already occupied"):
            self.action("assemble")
        self.assertEqual(output.read_bytes(), b"unrelated")
        pipeline.ff(["-f", "lavfi", "-i", "color=blue:s=64x48:r=15:d=1", "-c:v", "libx264"], self.edited)
        with self.assertRaisesRegex(ValueError, "frame count or presentation timing"):
            self.action("render")
        self.assertTrue(read(self.folder / "elements/V01/check.json")["approved"])

    def test_candidate_in_coarser_container_clock_keeps_the_same_picture_frame(self):
        candidate = self.root / "edited.mkv"
        pipeline.ff(["-f", "lavfi", "-i", "nullsrc=s=64x48:r=30:d=1", "-vf",
                     "geq=lum='if(mod(N,2),235,16)':cb=128:cr=128", "-c:v", "ffv1"], candidate)
        variant = self.state["variants"][0]
        variant.update(candidate=str(candidate), ranges=[[0, 30]])
        rendered = self.action("render")["video"]
        rgb = run([FFMPEG, "-v", "error", "-i", rendered, "-vf", "scale=2:2", "-map", "0:v:0",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], binary=True)
        self.assertEqual([rgb[n * 12] > 128 for n in range(30)], [bool(n % 2) for n in range(30)])

    def test_conflicting_variant_names_and_paths(self):
        for name in ("../unsafe", "蓝箱.MP4", "con", "NUL.mp4"):
            with self.subTest(name=name):
                self.order["variants"][1]["name"] = name
                with self.assertRaises(ValueError):
                    elements.variants_from_order(self.order, self.state["sources"], self.root / "delivery")

    def test_replanning_one_variant_preserves_sibling_and_blocks_stale_publication(self):
        for i in range(2):
            self.action("render", i)
            self.action("check", i)
            self.action("approve", i)
        self.order["variants"][0]["ranges"] = [[7, 16]]
        args = Namespace(command="plan", only=None, segment=None, input=write(self.root / "update.json", self.order),
                         accept=None, covers=[], reason=None, redo=False, generation_id=None, at=None)
        with redirect_stdout(StringIO()):
            elements.dispatch(args, self.folder, self.state)
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.action("assemble", 0)
        self.state = job.load(self.folder)
        with self.assertRaisesRegex(ValueError, "run render"):
            self.action("assemble", 0)
        self.assertTrue(Path(self.action("assemble", 1)).is_file())

    def test_target_selection_prioritizes_named_frames_without_reencoding(self):
        before = self.action("render")
        self.action("check")
        self.action("approve")
        selection = {"reason": "The crate crosses the center several times. The furnace stays at the edge.",
                     "frames": {"peak": 12, "early": 1, "repeat": [16, 18]}}
        self.order["variants"][0]["target_selection"] = selection
        self.order["variants"][0]["review_frames"] = [24]
        args = Namespace(command="plan", only=None, segment=None, input=write(self.root / "plan.json", self.order),
                         accept=None, covers=[], reason=None, redo=False, generation_id=None, at=None)
        output = StringIO()
        with redirect_stdout(output):
            elements.dispatch(args, self.folder, self.state)
        self.assertIn(selection["reason"], output.getvalue())
        self.state = job.load(self.folder)
        self.assertFalse(self.action("status")["approved"])
        self.assertTrue(self.action("status")["rendered"])
        with patch.object(elements, "render", side_effect=AssertionError):
            self.assertEqual(before, self.action("render"))
        page = self.action("check")
        self.assertEqual(page["target_selection"], selection)
        self.assertEqual(page["frames"][:4], [12, 1, 16, 18])
        self.assertIn(24, page["frames"])
        self.assertEqual(len(page["frames"]), len(set(page["frames"])))
        with Image.open(page["sheets"][0]) as sheet:
            self.assertGreater(sheet.getpixel((350, 50))[2], 200)  # Edited frame 12.
            self.assertLess(sheet.getpixel((1010, 50))[2], 30)    # Original frame 1.

    def test_source_first_plan_and_irregular_presentation_times(self):
        pipeline.ff(["-f", "lavfi", "-i", "testsrc2=s=64x48:r=30:d=1",
                     "-f", "lavfi", "-i", "sine=frequency=330:duration=1",
                     "-vf", r"select=not(eq(mod(n\,5)\,0)),setpts=PTS+0.1/TB",
                     "-fps_mode:v", "passthrough", "-c:v", "libx264", "-c:a", "aac"], self.original)
        order = dict(self.order)
        variants = order.pop("variants")
        folder, state = job.init(order, self.root / "survey-first" / "temp")
        self.assertEqual(state["variants"], [])
        plan_file = write(self.root / "plan.json", {"variants": [variants[1]]})
        args = Namespace(command="plan", only=None, segment=None, input=plan_file, accept=None, covers=[],
                         reason=None, redo=False, generation_id=None, at=None)
        with redirect_stdout(StringIO()):
            elements.dispatch(args, folder, state)
        state = job.load(folder)
        result = elements.command("render", folder, state, state["variants"][0])
        before, after = (probe(path)["streams"] for path in (self.original, result["video"]))
        for a, b in zip(before, after):
            self.assertAlmostEqual(float(a["start_time"]), float(b["start_time"]), delta=.001)
        self.assertEqual(elements.packet_times(self.original), elements.packet_times(result["video"]))
        offset = float(before[0]["start_time"])
        self.assertGreater(offset, .1)
        point = elements.command("check", folder, state, state["variants"][0], at=offset)
        self.assertEqual(point["frames"], [0])


if __name__ == "__main__":
    unittest.main()
