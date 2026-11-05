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
        # GOF3D10374 at 33.6 s (other logs, water and textures; it passed as slight under the old limits)
        self.assertEqual(assemble.jump_verdict({"mismatch": 12.42, "motion": 4.81, "local": 19.51}), "visible")
        self.assertEqual(assemble.jump_verdict({"mismatch": 9.0, "motion": 2.0}), "visible")
        self.assertEqual(assemble.jump_verdict({"mismatch": 5.0, "motion": 1.5, "local": 13.0}), "slight")
        self.assertEqual(assemble.jump_verdict({"mismatch": 6.0, "motion": 4.0}), "clean")
        self.assertEqual(assemble.worst("clean", None, "slight"), "slight")

    def test_inside_jump_sees_a_step_where_the_own_frames_start(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from reskin.common import FFMPEG
        if not Path(FFMPEG).is_file():
            self.skipTest("no FFmpeg in the runtime cache")
        with tempfile.TemporaryDirectory() as folder:
            smooth = Path(folder) / "smooth.mp4"
            subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x160:rate=24", "-t", "2",
                            "-pix_fmt", "yuv420p", str(smooth)], check=True)
            self.assertEqual(assemble.inside_jump(smooth, 24, 24.0)["jump"], "clean")
            jumped = Path(folder) / "jumped.mp4"         # the same motion, but from frame 24 on in other colours
            graph = ("[0:v]split[a][b];[a]trim=end_frame=24[a1];[b]trim=start_frame=24,setpts=PTS-STARTPTS,"
                     "lutrgb=r='val*0.6':g='255-val'[b1];[a1][b1]concat=n=2:v=1[v]")
            subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x160:rate=24", "-t", "2",
                            "-filter_complex", graph, "-map", "[v]", "-pix_fmt", "yuv420p", str(jumped)], check=True)
            self.assertEqual(assemble.inside_jump(jumped, 24, 24.0)["jump"], "visible")


class LookTests(unittest.TestCase):
    def test_a_look_step_at_a_seam_stands_out(self):
        rng = np.random.default_rng(5)
        scan = {"fps": 24.0, "saturation": np.abs(rng.normal(.2, .1, 600)), "colour": np.abs(rng.normal(.5, .2, 600))}
        scan["saturation"][299] = 2.3          # the pair ending at 12.5 s (GOF3D10012's 26.4 s seam: 2.31 vs 0.19)
        verdict = assemble.look_verdict(scan, 12.5, [12.5])
        self.assertEqual(verdict["jump"], "visible")
        self.assertGreater(verdict["saturation"]["z"], 8)
        self.assertEqual(assemble.look_verdict(scan, 20.0, [12.5, 20.0])["jump"], "clean")   # an ordinary moment

    def test_phase_shift_finds_a_pan(self):
        rng = np.random.default_rng(6)
        a = rng.uniform(0, 255, (64, 48)).astype(np.float32)
        b = np.roll(np.roll(a, 3, axis=0), -2, axis=1)
        self.assertEqual(assemble._phase_shift(a, b), (3, -2))


class HudTests(unittest.TestCase):
    def test_hud_boxes(self):
        self.assertIsNone(plan.hud_problem([{"box": [0, .1, .28, .15], "what": "金币"}]))
        for bad in ([{"box": [.3, .1, .2, .15]}], [{"box": [0, 0, .9, .9]}], [{"rect": [0, 0, .1, .1]}], {"box": []}):
            self.assertIsNotNone(plan.hud_problem(bad))


if __name__ == "__main__":
    unittest.main()
