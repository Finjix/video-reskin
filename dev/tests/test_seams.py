"""Seams and HUD: chains per shot, balanced long-shot splits, the best-matching join, jump verdicts, HUD boxes."""
import unittest

import numpy as np

from reskin import assemble, plan, render
from test_plan import catalog_row


class ChainTests(unittest.TestCase):
    def test_chains_break_at_cuts(self):
        row = {"segments": [{"id": "K01", "seam": "start"}, {"id": "K02", "seam": "continuous"},
                            {"id": "K03", "seam": "cut"}, {"id": "K04", "seam": "continuous"}]}
        self.assertEqual([[s["id"] for s in chain] for chain in render.chains(row)], [["K01", "K02"], ["K03", "K04"]])

    def test_long_shot_splits_evenly(self):
        row = catalog_row(70.2, 30.0)
        segments = plan.segment(row, [0, 65.7], 30.0, [1.0] * row["info"]["frames"], [])
        self.assertEqual(len(segments), 3)
        lengths = [s["seconds"] for s in segments]
        self.assertLess(max(lengths) - min(lengths), 4.5)
        self.assertTrue(all(length <= 30 for length in lengths))


class JoinTests(unittest.TestCase):
    def test_join_picks_the_matching_frame(self):
        rng = np.random.default_rng(1)
        before = rng.uniform(0, 255, (15, 8, 8, 3)).astype(np.float32)
        after = rng.uniform(0, 255, (15, 8, 8, 3)).astype(np.float32)
        after[9] = before[9]
        identity = (np.ones(3), np.zeros(3))
        join = assemble.choose_join(before, after, identity, identity)
        self.assertEqual(join["frame"], 9)
        self.assertEqual(join["mismatch"], 0.0)

    def test_local_jump_sees_one_region(self):
        rng = np.random.default_rng(2)
        before = np.repeat(rng.uniform(60, 200, (1, 40, 24, 3)), 6, axis=0).astype(np.float32)
        after = before.copy()
        after[:, 8:12, 8:12] += 60          # one object changes colour, the rest is identical
        identity = (np.ones(3), np.zeros(3))
        join = assemble.choose_join(before, after, identity, identity)
        self.assertLess(join["mismatch"], 3)
        self.assertGreater(join["local"], 30)
        self.assertEqual(assemble.jump_verdict(join), "visible")

    def test_jump_verdicts(self):
        self.assertEqual(assemble.jump_verdict({"mismatch": 20.6, "motion": .56}), "visible")
        self.assertEqual(assemble.jump_verdict({"mismatch": 9.0, "motion": 2.0}), "slight")
        self.assertEqual(assemble.jump_verdict({"mismatch": 6.0, "motion": 4.0}), "clean")


class HudTests(unittest.TestCase):
    def test_hud_boxes(self):
        self.assertIsNone(plan.hud_problem([{"box": [0, .1, .28, .15], "what": "金币"}]))
        for bad in ([{"box": [.3, .1, .2, .15]}], [{"box": [0, 0, .9, .9]}], [{"rect": [0, 0, .1, .1]}], {"box": []}):
            self.assertIsNotNone(plan.hud_problem(bad))


if __name__ == "__main__":
    unittest.main()
