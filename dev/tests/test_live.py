"""Live-action spans: delivered from the original, never keyframed or rendered."""
import unittest

from reskin import plan
from test_plan import catalog_row


class LiveSegmentTests(unittest.TestCase):
    def test_live_span_is_its_own_segment(self):
        row = catalog_row(60.0, 30.0, cuts=(8.0,))
        segments = plan.segment_all(row, [0, 60], 30, [], [], [{"start": 0, "end": 8.1, "what": "a man in a car"}])
        self.assertTrue(segments[0]["live"])
        self.assertEqual(segments[0]["frames"], [0, 240])           # snapped onto the cut at 8.0 s
        self.assertEqual(segments[0]["seconds"], 0.0)
        self.assertEqual(segments[1]["seam"], "cut")
        self.assertEqual([s["id"] for s in segments], [f"K{n:02d}" for n in range(1, len(segments) + 1)])
        self.assertEqual(segments[-1]["frames"][1], 1800)
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a["frames"][1], b["frames"][0])
        game = [s for s in segments if not s.get("live")]
        self.assertTrue(all(s["render_frames"][0] >= 240 for s in game))

    def test_live_span_in_the_middle(self):
        row = catalog_row(40.0, 30.0, cuts=(15.0, 20.0))
        segments = plan.segment_all(row, [0, 40], 30, [], [], [{"start": 15, "end": 20}])
        self.assertEqual([s.get("live", False) for s in segments], [False, True, False])
        self.assertEqual([s["seam"] for s in segments], ["start", "cut", "cut"])

    def test_without_live_unchanged(self):
        row = catalog_row(65.7, 30.0)
        self.assertEqual(plan.segment_all(row, [0, 65.7], 30, [], [47.5], []),
                         plan.segment(row, [0, 65.7], 30, [], [47.5]))

    def test_short_game_stretch_is_refused(self):
        row = catalog_row(30.0, 30.0)
        with self.assertRaises(ValueError):
            plan.segment_all(row, [0, 30], 30, [], [], [{"start": 2, "end": 10}])

    def test_anchor_avoids_live(self):
        row = catalog_row(60.0, 30.0, cuts=(40.0,))
        anchor = plan.auto_anchor(row, [0, 60], [1.0] * 1800, [{"start": 0, "end": 40}])
        self.assertGreaterEqual(anchor, 40)


class LiveEditsTests(unittest.TestCase):
    def check(self, row):
        return plan.check_edits({"sources": {"S01": row}}, {}, {"sources": [catalog_row(60.0, 30.0, cuts=(8.0,))]})

    def test_accepted_and_normalised(self):
        edits = self.check({"live": [{"start": 0, "end": 8, "what": "live-action car"}]})
        row = plan.source_edits(edits, "S01", catalog_row(60.0, 30.0), {"default_materials": "x"})
        self.assertEqual(row["live"], [{"start": 0.0, "end": 8.0, "what": "live-action car"}])

    def test_rejected(self):
        for bad in ({"live": [{"start": 5, "end": 2}]}, {"live": [{"start": 0, "end": 5}, {"start": 4, "end": 9}]},
                    {"live": [{"start": 0, "end": 8}], "anchor": 3}, {"live": [{"start": 0, "end": 60}]},
                    {"live": {"start": 0, "end": 8}}, {"live": [{"start": 0, "end": 8, "zoom": 1}]}):
            with self.assertRaises(ValueError, msg=bad):
                self.check(bad)

    def test_like_twin_takes_live(self):
        catalog = {"sources": [catalog_row(60.0, 30.0, cuts=(8.0,)), {**catalog_row(60.0, 30.0, cuts=(8.0,)), "id": "S02"}]}
        edits = plan.check_edits({"sources": {"S01": {"live": [{"start": 0, "end": 8}]}, "S02": {"like": "S01"}}},
                                 {}, catalog)
        row = plan.source_edits(edits, "S02", catalog["sources"][1], {"default_materials": "x"})
        self.assertEqual(row["live"][0]["end"], 8.0)


class LiveStepsTests(unittest.TestCase):
    def row(self):
        segments = plan.segment_all(catalog_row(60.0, 30.0, cuts=(8.0,)), [0, 60], 30, [], [], [{"start": 0, "end": 8}])
        return {"keep": [0, 60], "fps": 30.0, "anchor": 30, "segments": segments, "like": None, "style_from": None,
                "width": 1920, "height": 1080}

    def test_render_and_keyframes_skip_live(self):
        from reskin import render
        row = self.row()
        members = [seg for _, _, group in render.plan_order({"sources": {"S01": row}}) for seg in group]
        self.assertTrue(members and not any(seg.get("live") for seg in members))
        try:
            from reskin import keyframes
        except ImportError:
            self.skipTest("media packages missing")
        tasks = keyframes.tasks(None, {"sources": [{"id": "S01", "path": "x.mp4"}]}, {"sources": {"S01": row}},
                                {}, None, set())
        self.assertNotIn("S01:K01", [t["key"] for t in tasks])
        self.assertIn("S01:K02", [t["key"] for t in tasks])

    def test_lint_draft_lists_live(self):
        try:
            from reskin import lint
        except ImportError:
            self.skipTest("media packages missing")
        row = lint.draft({"live_action": [{"what": "a real snowy road with cars", "start": 0, "end": 5.2}],
                          "end_card_start": 50}, catalog_row(60.0, 30.0))
        self.assertEqual(row["live"], [{"what": "a real snowy road with cars", "start": 0.0, "end": 5.2}])

    def test_review_ignores_live(self):
        try:
            from reskin import review
        except ImportError:
            self.skipTest("media packages missing")
        assembled = {"segments": [{"id": "K01", "start": 0, "end": 8, "live": True}, {"id": "K02", "start": 8, "end": 60}]}
        self.assertTrue(review.inside_live(assembled, 1.0, 7.9))
        self.assertFalse(review.inside_live(assembled, 7.0, 9.0))


if __name__ == "__main__":
    unittest.main()
