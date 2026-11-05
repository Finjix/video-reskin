"""Burned-in overlays: validation, mattes, drawn text and compositing (no media files needed)."""
import unittest

import numpy as np

from reskin import overlay, plan, prompts
from reskin.common import PROMPTS, read


class ValidationTests(unittest.TestCase):
    def test_good_items_pass(self):
        items = [{"what": "顶部横幅", "start": 2, "poly": [[.1, .3], [.2, .27], [.3, .3], [.2, .33]],
                  "colors": ["#ffffff", "#8c716a"]},
                 {"what": "字幕", "box": [.1, .8, .9, .86], "text": "钓到一条大金枪鱼！", "stroke": "#000000",
                  "fill": "#00000080", "ai": "remove"},
                 {"what": "气泡", "box": [.85, .53, .95, .59], "shape": "ellipse", "ai": "keep"}]
        self.assertIsNone(overlay.problem(items))

    def test_bad_items_are_named(self):
        cases = [
            ([{"box": [.1, .1, .2, .2]}], "needs what"),
            ([{"what": "x"}], "box"),
            ([{"what": "x", "box": [.3, .1, .2, .2]}], "box must be"),
            ([{"what": "x", "box": [0, 0, 1, 1]}], "30%"),
            ([{"what": "x", "box": [.1, .1, .2, .2], "follow": "scene"}], "screen-fixed"),
            ([{"what": "x", "box": [.1, .1, .2, .2], "colors": ["white"]}], "colour"),
            ([{"what": "x", "box": [.1, .1, .2, .2], "text": "a", "colors": ["#fff"]}], "draws its own"),
            ([{"what": "x", "box": [.1, .1, .2, .2], "ai": "maybe"}], "ai must be"),
            ([{"what": "x", "box": [.1, .1, .2, .2], "size": 3}], "unknown keys"),
        ]
        for items, words in cases:
            self.assertIn(words, overlay.problem(items) or "", items)

    def test_edits_accept_overlays(self):
        catalog = {"sources": [{"id": "S01", "duration": 10.0}]}
        good = {"sources": {"S01": {"overlays": [{"what": "牌子", "box": [.1, .1, .2, .2]}]}}}
        self.assertIn("overlays", plan.check_edits(good, {}, catalog)["sources"]["S01"])
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {"overlays": [{"box": [.1, .1, .2, .2]}]}}}, {}, catalog)


class PromptTests(unittest.TestCase):
    def test_burned_items_are_left_out_by_the_models(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": "积雪地面", "remove": ["游戏logo"],
               "overlays": [{"what": "the caption \"Big catch!\" at the bottom", "box": [.1, .1, .2, .2], "start": 2, "end": 4},
                            {"what": "需求气泡", "box": [.5, .5, .6, .6], "ai": "keep"}]}
        during = prompts.compose(style, "video", row, span=(1, 3))
        self.assertIn("the caption \"Big catch!\" at the bottom", during)
        self.assertIn("overlaid exactly by a program", during)
        self.assertNotIn("需求气泡", during)
        self.assertNotIn("overlaid exactly by a program", prompts.compose(style, "video", row, span=(5, 8)))
        for kind in ("anchor", "keyframe", "video"):
            self.assertNotIn("{", prompts.compose(style, kind, row, span=(2, 2)))
            self.assertIn("realistic materials", prompts.compose(style, kind, row, span=(2, 2)))


class PictureTests(unittest.TestCase):
    def test_drawn_text_fills_its_box(self):
        rgba = overlay.draw_text({"text": "$300", "color": "#ffffff", "stroke": "#000000"}, 120, 40)
        self.assertEqual(rgba.shape, (40, 120, 4))
        rows, cols = np.nonzero(rgba[..., 3] > 128)
        self.assertGreater(cols.max() - cols.min(), 60)
        self.assertGreater(rows.max() - rows.min(), 20)
        plate = overlay.draw_text({"text": "钓鱼", "fill": "#00000080"}, 80, 30)
        self.assertEqual(int(plate[15, 3, 3]), 128)

    def test_colour_key_takes_only_the_listed_colours(self):
        crop = np.zeros((10, 20, 3), np.uint8)
        crop[:, :10] = (250, 250, 250)        # white glyph
        crop[:, 10:] = (200, 140, 60)         # orange post in front of the sign
        key = overlay.colour_key(crop.astype(np.float32), [overlay._colour("#ffffff")], 40)
        self.assertGreater(key[5, 3], .99)
        self.assertLess(key[5, 16], .01)

    def test_composite_only_inside_region_and_time(self):
        base = np.zeros((60, 80, 3), np.uint8)
        original = np.full((60, 80, 3), 200, np.uint8)
        entry = {"what": "", "first": 0, "last": 5, "origin": (10, 10), "crop": (10, 10),
                 "alpha": np.ones((10, 10), np.float32), "kind": "original", "colours": [], "tolerance": 60.0}
        out = overlay.composite(base, original, 1, [entry])
        self.assertEqual(int(out[15, 15, 0]), 200)
        self.assertEqual(int(out[30, 30, 0]), 0)
        self.assertIs(overlay.composite(base, original, 7, [entry]), base)


if __name__ == "__main__":
    unittest.main()
