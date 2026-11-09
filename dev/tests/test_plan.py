"""Segmentation: H3's whole-second 4-15 s clips, seams on cuts or fast camera moves, overlaps only inside shots;
edits validation; scenes and subjects per segment."""
import unittest

from reskin import plan


def catalog_row(duration=60.0, fps=30.0, cuts=()):
    bounds = [0, *[round(c * fps) for c in cuts], round(duration * fps)]
    shots = [{"n": i + 1, "start": a / fps, "end": b / fps, "frames": [a, b]} for i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:]))]
    return {"id": "S01", "duration": duration, "info": {"fps": fps, "frames": bounds[-1], "width": 1080, "height": 1920},
            "shots": shots, "end_card": None}


class SegmentTests(unittest.TestCase):
    def check(self, segments, fps, keep, maximum):
        half = round(plan.OVERLAP_SECONDS * fps / 2)
        self.assertEqual(segments[0]["frames"][0], round(keep[0] * fps))
        self.assertEqual(segments[-1]["frames"][1], round(keep[1] * fps))
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a["frames"][1], b["frames"][0])
        for number, seg in enumerate(segments):
            length = (seg["render_frames"][1] - seg["render_frames"][0]) / fps
            self.assertAlmostEqual(length, seg["render_seconds"], delta=1 / fps)
            self.assertLessEqual(seg["render_seconds"], maximum)
            self.assertGreaterEqual(seg["render_seconds"], plan.MIN_RENDER)
            self.assertLessEqual(seg["render_frames"][0], seg["frames"][0] - (half if seg["seam"] == "continuous" else 0))
            after = segments[number + 1]["seam"] if number + 1 < len(segments) else None
            self.assertGreaterEqual(seg["render_frames"][1], seg["frames"][1] + (half if after == "continuous" else 0))

    def test_single_long_shot(self):
        row = catalog_row(63.6, 60.0)
        segments = plan.segment(row, [0, 59.08], 15.0, [1.0] * row["info"]["frames"], [], [])
        self.check(segments, 60.0, [0, 59.08], 15)
        self.assertEqual(segments[0]["seam"], "start")
        self.assertTrue(all(s["seam"] == "continuous" for s in segments[1:]))
        self.assertLessEqual(len(segments), 6)

    def test_cuts_become_seams(self):
        row = catalog_row(40.0, 30.0, cuts=(8.0, 19.0, 30.0))
        segments = plan.segment(row, [0, 40.0], 15.0, [], [], [])
        self.check(segments, 30.0, [0, 40.0], 15)
        self.assertEqual([s["seam"] for s in segments], ["start", "cut", "cut", "cut"])

    def test_short_shots_are_packed(self):
        row = catalog_row(20.0, 30.0, cuts=(2.0, 4.0, 6.0, 8.0, 10.0, 12.0))
        segments = plan.segment(row, [0, 20.0], 15.0, [], [], [])
        self.check(segments, 30.0, [0, 20.0], 15)
        self.assertLessEqual(len(segments), 2)
        self.assertTrue(any(s["inner_cuts"] for s in segments))

    def test_seam_goes_where_the_camera_moves_fast(self):
        fps = 30.0
        row = catalog_row(24.0, fps)
        camera = [[0.02, 0.0]] * row["info"]["frames"]
        for i in range(round(10.8 * fps), round(11.2 * fps)):
            camera[i] = [0.9, 0.3]
        segments = plan.segment(row, [0, 24.0], 15.0, [1.0] * row["info"]["frames"], camera, [])
        self.check(segments, fps, [0, 24.0], 15)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[1]["frames"][0] / fps, 11.0, delta=.25)
        self.assertGreater(segments[1]["seam_score"], 1.0)
        self.assertGreater(segments[1]["seam_camera"][0], .5)

    def test_keep_range_respected_and_padding_stays_inside_the_source(self):
        row = catalog_row(31.0, 30.0, cuts=(10.33,))
        segments = plan.segment(row, [2.0, 30.9], 15.0, [], [], [])
        self.check(segments, 30.0, [2.0, 30.9], 15)
        self.assertLessEqual(segments[-1]["render_frames"][1], row["info"]["frames"])


class EditsTests(unittest.TestCase):
    def test_rejects_bad_keep_and_unknown_sources(self):
        catalog = {"sources": [catalog_row()]}
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {"keep": [10, 5]}}}, {}, catalog)
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {"keep": [10, 12]}}}, {}, catalog)    # shorter than H3's 4 s
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S09": {}}}, {}, catalog)
        with self.assertRaisesRegex(ValueError, "anchor"):
            plan.check_edits({"sources": {"S01": {"anchor": 3}}}, {}, catalog)

    def test_timed_removal_items(self):
        catalog = {"sources": [catalog_row()]}
        plan.check_edits({"sources": {"S01": {"remove": [{"what": "logo", "start": 1, "end": 5}]}}}, {}, catalog)
        for bad in ([{"what": "logo", "start": 5, "end": 1}], [{"text": "logo"}], ["  "], "logo"):
            with self.assertRaises(ValueError):
                plan.check_edits({"sources": {"S01": {"remove": bad}}}, {}, catalog)

    def test_scenes_and_subjects(self):
        catalog = {"sources": [catalog_row()]}
        good = {"scenes": [{"name": "营地", "start": 0, "end": 60, "views": [1, 20, 40]}],
                "subjects": [{"what": "猎人", "kind": "character", "start": 0, "end": 60, "at": 7.5,
                              "box": [.3, .4, .5, .6]}]}
        plan.check_edits({"sources": {"S01": good}}, {}, catalog)
        for bad in ({"scenes": []}, {"scenes": [{"name": "x", "views": [99]}]},
                    {"subjects": [{"what": "x", "at": 1, "box": [.5, .5, .4, .6]}]},
                    {"subjects": [{"what": "x", "kind": "spaceship", "at": 1, "box": [.1, .1, .2, .2]}]}):
            with self.assertRaises(ValueError):
                plan.check_edits({"sources": {"S01": bad}}, {}, catalog)

    def test_defaults_merge_and_survey_trimmed_to_keep(self):
        catalog = {"sources": [catalog_row()]}
        edits = plan.check_edits({"defaults": {"remove": ["底部警示语"]},
                                  "sources": {"S01": {"remove": ["游戏logo"], "keep": [0, 30]}}}, {}, catalog)
        survey = {"scenes": [{"name": "营地", "start": 0, "end": 60, "views": [1, 20, 45]}],
                  "subjects": [{"what": "a", "start": 0, "end": 60, "at": 50, "box": [0, 0, .1, .1]},
                               {"what": "b", "start": 40, "end": 60, "at": 45, "box": [0, 0, .1, .1]}]}
        row = plan.source_edits(edits, "S01", catalog["sources"][0], {"default_materials": "x"}, survey)
        self.assertEqual(row["remove"], ["底部警示语", "游戏logo"])
        self.assertEqual(row["keep"], [0.0, 30.0])
        self.assertEqual(row["materials"], "x")
        self.assertEqual(row["scenes"][0]["views"], [1, 20])
        self.assertEqual([s["what"] for s in row["subjects"]], ["a"])


class RefsTests(unittest.TestCase):
    def test_subjects_new_in_a_segment_come_first(self):
        fps = 30.0
        row = {"fps": fps, "scenes": [{"name": "a", "start": 0, "end": 30, "views": [1]},
                                      {"name": "b", "start": 30, "end": 60, "views": [40]}],
               "subjects": [{"what": "hunter", "start": 0, "end": 60}, {"what": "cash", "start": 20, "end": 60},
                            {"what": "truck", "start": 45, "end": 60}],
               "segments": [{"id": "K01", "render_frames": [0, 450]}, {"id": "K02", "render_frames": [441, 900]},
                            {"id": "K03", "render_frames": [1200, 1800]}]}
        plan.assign_refs(row)
        self.assertEqual(row["segments"][0]["subjects"], [1])
        self.assertEqual(row["segments"][1]["subjects"], [2, 1])
        self.assertEqual(row["segments"][1]["scenes"], [1, 2])
        self.assertEqual(row["segments"][2]["subjects"], [3, 1])          # at most plan.MAX_SHEETS
        self.assertEqual(row["segments"][2]["scenes"], [2])


if __name__ == "__main__":
    unittest.main()
