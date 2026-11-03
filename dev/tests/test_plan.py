"""Segmentation: lengths stay inside Seedance's limits, seams land on cuts, overlaps only inside shots."""
import unittest

from reskin import plan


def catalog_row(duration=60.0, fps=30.0, cuts=()):
    bounds = [0, *[round(c * fps) for c in cuts], round(duration * fps)]
    shots = [{"n": i + 1, "start": a / fps, "end": b / fps, "frames": [a, b]} for i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:]))]
    return {"id": "S01", "duration": duration, "info": {"fps": fps, "frames": bounds[-1], "width": 1080, "height": 1920},
            "shots": shots, "end_card": None}


class SegmentTests(unittest.TestCase):
    def check(self, segments, fps, keep, maximum):
        self.assertEqual(segments[0]["frames"][0], round(keep[0] * fps))
        self.assertEqual(segments[-1]["frames"][1], round(keep[1] * fps))
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a["frames"][1], b["frames"][0])
        for seg in segments:
            length = (seg["render_frames"][1] - seg["render_frames"][0]) / fps
            self.assertLessEqual(length, maximum + 1e-6, seg)
            self.assertGreaterEqual(length, plan.MIN_SECONDS - 1e-6, seg)
            if seg["seam"] == "continuous":
                self.assertEqual(seg["frames"][0] - seg["render_frames"][0], round(plan.OVERLAP_SECONDS * fps))
            else:
                self.assertEqual(seg["frames"][0], seg["render_frames"][0])

    def test_single_long_shot(self):
        row = catalog_row(63.6, 60.0)
        motion = [1.0] * row["info"]["frames"]
        segments = plan.segment(row, [0, 59.08], 12.0, motion, [])
        self.check(segments, 60.0, [0, 59.08], 12.0)
        self.assertEqual(segments[0]["seam"], "start")
        self.assertTrue(all(s["seam"] == "continuous" for s in segments[1:]))

    def test_cuts_become_seams(self):
        row = catalog_row(40.0, 30.0, cuts=(8.0, 19.0, 30.0))
        segments = plan.segment(row, [0, 40.0], 12.0, [], [])
        self.check(segments, 30.0, [0, 40.0], 12.0)
        self.assertEqual([s["seam"] for s in segments], ["start", "cut", "cut", "cut"])

    def test_short_shots_are_packed(self):
        row = catalog_row(20.0, 30.0, cuts=(2.0, 4.0, 6.0, 8.0, 10.0, 12.0))
        segments = plan.segment(row, [0, 20.0], 12.0, [], [])
        self.check(segments, 30.0, [0, 20.0], 12.0)
        self.assertLess(len(segments), 4)
        self.assertTrue(any(s["inner_cuts"] for s in segments))

    def test_split_prefers_still_moment(self):
        fps = 30.0
        row = catalog_row(20.0, fps)
        motion = [10.0] * row["info"]["frames"]
        for i in range(round(9.5 * fps), round(10.5 * fps)):
            motion[i] = 0.0
        segments = plan.segment(row, [0, 20.0], 12.0, motion, [])
        self.check(segments, fps, [0, 20.0], 12.0)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[1]["frames"][0] / fps, 10.0, delta=.6)

    def test_keep_range_respected(self):
        row = catalog_row(65.0, 30.0, cuts=(10.33,))
        segments = plan.segment(row, [2.0, 61.0], 12.0, [], [])
        self.check(segments, 30.0, [2.0, 61.0], 12.0)


class EditsTests(unittest.TestCase):
    def test_rejects_bad_keep(self):
        catalog = {"sources": [catalog_row()]}
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {"keep": [10, 5]}}}, {}, catalog)
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S09": {}}}, {}, catalog)

    def test_timed_removal_items(self):
        catalog = {"sources": [catalog_row()]}
        plan.check_edits({"sources": {"S01": {"remove": [{"what": "logo", "start": 1, "end": 5}]}}}, {}, catalog)
        for bad in ([{"what": "logo", "start": 5, "end": 1}], [{"text": "logo"}], ["  "], "logo"):
            with self.assertRaises(ValueError):
                plan.check_edits({"sources": {"S01": {"remove": bad}}}, {}, catalog)

    def test_defaults_merge(self):
        catalog = {"sources": [catalog_row()]}
        edits = plan.check_edits({"defaults": {"remove": ["底部警示语"]},
                                  "sources": {"S01": {"remove": ["游戏logo"], "keep": [0, 50]}}}, {}, catalog)
        row = plan.source_edits(edits, "S01", catalog["sources"][0], {"default_materials": "x"})
        self.assertEqual(row["remove"], ["底部警示语", "游戏logo"])
        self.assertEqual(row["keep"], [0.0, 50.0])
        self.assertEqual(row["materials"], "x")


if __name__ == "__main__":
    unittest.main()
