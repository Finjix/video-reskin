"""Local patches on real video (FFmpeg test pattern): the patched clip keeps its length, switches into the patch and
back without a frame offset, and matches the patch's colours to the segment."""
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from reskin import common, patch
from reskin.assemble import frames_rgb
from reskin.common import FFMPEG, run


def have_ffmpeg() -> bool:
    try:
        run([FFMPEG, "-version"])
        return True
    except Exception:
        return False


@unittest.skipUnless(have_ffmpeg(), "FFmpeg not available")
class PatchApplyTests(unittest.TestCase):
    def setUp(self):
        self.job = Path(tempfile.mkdtemp(prefix="vr-patch-"))

    def tearDown(self):
        shutil.rmtree(self.job, ignore_errors=True)

    def clip(self, name: str, start: int, frames: int, tone: str = "") -> Path:
        out = self.job / f"{name}.mp4"
        tone = f",lutrgb=r='{tone}':g='{tone}':b='{tone}'" if tone else ""
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=96x160:rate=30:duration=12",
             "-vf", f"trim=start_frame={start}:end_frame={start + frames},setpts=PTS-STARTPTS{tone},format=yuv420p",
             "-c:v", "libx264", "-crf", "12", "-r", "30", out])
        return out

    def test_patch_switches_in_and_out_cleanly(self):
        self.check("")

    def test_patch_colours_are_matched(self):
        self.check("val*0.85+12")         # the patch came out darker and flatter than its segment

    def check(self, tone: str):
        base = self.clip("base", 0, 300)
        patched_ai = self.clip("patch-ai", 90, 120, tone)
        common.write(self.job / "state/render/S01-K01.json", {"status": "done", "generation_id": "g"})
        common.write(self.job / "config/patches.json",
                     {"S01:K01": [{"id": "P1", "base": "g", "render_frames": [90, 210], "reason": "test"}]})
        common.write(self.job / "state/render/S01-K01-P1.json",
                     {"status": "done", "generation_id": "gp", "output": str(patched_ai)})
        row = {"fps": 30, "keep": [0.0, 10.0]}
        line = {"id": "K01", "out": [0, 300], "body": [0, 300]}
        result, joins = patch.apply(self.job, "S01", row, line, base, 30, (96, 160), [])
        self.assertNotEqual(result, base)
        self.assertEqual(int(common.video_info(result)["frames"]), 300)
        self.assertEqual(joins[0]["in"]["jump"], "clean")
        self.assertEqual(joins[0]["out"]["jump"], "clean")
        def difference(frame):
            return float(np.abs(frames_rgb(base, frame, 1, 30)[0] - frames_rgb(result, frame, 1, 30)[0]).mean())
        # Outside the patch only the re-encode differs (the test pattern is noisy at this size); inside it the picture
        # is the same moment (no frame offset) with the colours matched back (17 levels off before matching).
        outside = max(difference(60), difference(250))
        self.assertLess(outside, 6.0)
        for frame in (100, 150, 200):
            self.assertLess(difference(frame), outside + 2.5, f"frame {frame}")


if __name__ == "__main__":
    unittest.main()
