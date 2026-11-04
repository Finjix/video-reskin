"""HUD matte geometry: the crop must be even so it matches FFmpeg's yuv420p crop in assemble."""
import shutil
import tempfile
import unittest
from pathlib import Path

from reskin import hud
from reskin.common import FFMPEG, run


def have_ffmpeg() -> bool:
    try:
        run([FFMPEG, "-version"])
        return True
    except Exception:
        return False


@unittest.skipUnless(have_ffmpeg(), "FFmpeg not available")
class HudMatteTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vr-hud-"))
        self.source = self.dir / "src.mp4"
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=3",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", self.source])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_crop_is_even_for_odd_boxes(self):
        # GOF3D10057 at 480p: this box gave a 161-wide matte against a 160-wide yuv420p crop.
        for box in ([0.0, 0.095, 0.285, 0.195], [0.013, 0.553, 0.069, 0.585], [0.81, 0.1, 1.0, 0.173]):
            item = hud.matte(self.source, [0.0, 3.0], {"box": box}, (480, 854), self.dir / "m.png")
            for key in ("x", "y", "w", "h"):
                self.assertEqual(item[key] % 2, 0, (box, key, item[key]))
            self.assertLessEqual(item["x"] + item["w"], 480)
            self.assertLessEqual(item["y"] + item["h"], 854)


if __name__ == "__main__":
    unittest.main()
