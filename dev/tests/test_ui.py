"""The `ui` field: screen-fixed interface the models draw from descriptions (hud and overlays are gone)."""
import unittest

from reskin import plan, prompts
from reskin.common import PROMPTS, read


class ValidationTests(unittest.TestCase):
    def test_edits_reject_overlays_and_hud(self):
        catalog = {"sources": [{"id": "S01", "duration": 10.0}]}
        for key in ("overlays", "hud"):
            with self.assertRaises(ValueError) as caught:
                plan.check_edits({"sources": {"S01": {key: [{"what": "牌子", "box": [.1, .1, .2, .2]}]}}}, {}, catalog)
            self.assertIn("`ui`", str(caught.exception))

    def test_edits_accept_ui(self):
        catalog = {"sources": [{"id": "S01", "duration": 10.0}]}
        good = {"sources": {"S01": {"ui": [{"what": "the coin counter \"12,580\" at the top left", "box": [0, 0, .2, .05]},
                                           {"what": "the caption \"Big catch!\" at the bottom", "start": 2, "end": 4}]}}}
        self.assertEqual(len(plan.check_edits(good, {}, catalog)["sources"]["S01"]["ui"]), 2)
        for bad in ([{"box": [.1, .1, .2, .2]}], [{"what": "x", "box": [.3, .1, .2, .2]}], [{"what": "x", "text": "a"}],
                    [{"what": "x", "start": 5, "end": 2}], {"what": "x"}):
            self.assertIsNotNone(plan.ui_problem(bad), bad)


class PromptTests(unittest.TestCase):
    def test_ui_is_drawn_by_the_models_while_on_screen(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": "积雪地面", "remove": ["游戏logo"],
               "ui": [{"what": "the caption \"Big catch!\" at the bottom", "start": 2, "end": 4},
                      "the coin counter \"12,580\" in bold white digits at the top left"]}
        during = prompts.compose(style, "video", row, span=(1, 3))
        self.assertIn("the caption \"Big catch!\" at the bottom", during)
        self.assertIn("flat 2D layer", during)
        self.assertIn("no flicker or morphing of letters", during)
        later = prompts.compose(style, "video", row, span=(5, 8))
        self.assertNotIn("Big catch", later)
        self.assertIn("12,580", later)
        self.assertNotIn("overlaid exactly by a program", during)
        self.assertNotIn("morphing of letters", prompts.compose(style, "anchor", row, span=(2, 2)))
        self.assertNotIn("flat 2D layer", prompts.compose(style, "keyframe", {"materials": "x"}, span=(2, 2)))
        for kind in ("anchor", "keyframe", "video"):
            self.assertNotIn("{", prompts.compose(style, kind, row, span=(2, 2)))
            self.assertIn("realistic materials", prompts.compose(style, kind, row, span=(2, 2)))


if __name__ == "__main__":
    unittest.main()
