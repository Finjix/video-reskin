"""Small pure helpers: prompts, timelines, sizes, HTTP encoding, credentials, review mapping."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from reskin import assemble, images, net, prompts, review
from reskin.common import read, PROMPTS


class PromptTests(unittest.TestCase):
    def test_compose_fills_every_placeholder(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": "真实的冰块", "remove": ["游戏logo", "底部警示语"], "extra": "雪地洁白"}
        for kind in ("anchor", "keyframe", "video"):
            text = prompts.compose(style, kind, row, extra="鱼不要变形")
            self.assertNotIn("{", text)
            self.assertIn("真实的冰块", text)
            self.assertIn("游戏logo、底部警示语", text)
            self.assertIn("雪地洁白", text)
            self.assertIn("鱼不要变形", text)
        self.assertTrue(prompts.compose(style, "video", row).startswith("编辑视频"))

    def test_timed_removals_only_where_on_screen(self):
        items = ["游戏logo", {"what": "底部警示语", "start": 0, "end": 10}, {"what": "右上角水印", "start": 30}]
        self.assertEqual(prompts.removals(items, (2, 2)), ["游戏logo", "底部警示语"])
        self.assertEqual(prompts.removals(items, (12, 20)), ["游戏logo"])
        self.assertEqual(prompts.removals(items, (25, 31)), ["游戏logo", "右上角水印"])
        self.assertIn("底部警示语 (0-10 s)", prompts.describe(items))

    def test_timed_materials(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": ["积雪地面", {"what": "蓝色皮卡车", "start": 50, "end": 66}], "remove": []}
        self.assertNotIn("皮卡车", prompts.compose(style, "video", row, span=(0, 11)))
        self.assertIn("积雪地面、蓝色皮卡车", prompts.compose(style, "video", row, span=(48, 60)))
        self.assertIn("积雪地面", prompts.compose(style, "video", {"materials": "积雪地面"}, span=(0, 1)))

    def test_rebuild_and_style_reference(self):
        style = read(PROMPTS)["presets"]["realistic"]
        row = {"materials": "x", "remove": []}
        for kind in ("anchor", "keyframe", "video"):
            text = prompts.compose(style, kind, row)
            self.assertIn("大幅重构", text)
            self.assertNotIn("质感参考", text)
        self.assertIn("图2是写实质感参考", prompts.compose(style, "anchor", row, style_ref=True))

    def test_no_removal_clause_when_empty(self):
        style = read(PROMPTS)["presets"]["realistic"]
        text = prompts.compose(style, "anchor", {"materials": "x", "remove": []})
        self.assertNotIn("删除", text)


class SizeTests(unittest.TestCase):
    def test_target_sizes(self):
        self.assertEqual(assemble.target_size(1080, 1920, "1080p"), (1080, 1920))
        self.assertEqual(assemble.target_size(1920, 1080, "1080p"), (1920, 1080))
        self.assertEqual(assemble.target_size(3840, 2160, "1080p"), (1920, 1080))
        self.assertEqual(assemble.target_size(1080, 1350, "1080p"), (1080, 1350))

    def test_image_aspects(self):
        self.assertEqual(images.aspect_for(1920, 1080), "16:9")
        self.assertEqual(images.aspect_for(1080, 1920), "9:16")
        self.assertEqual(images.aspect_for(1080, 2400), "adaptive")
        self.assertEqual(images.aspect_for(1080, 1350), "4:5")


class TimelineTests(unittest.TestCase):
    def test_global_clock_has_no_drift(self):
        row = {"fps": 60.0, "keep": [0, 59.08], "segments": [
            {"id": "K01", "seam": "start", "frames": [0, 545], "render_frames": [0, 545]},
            {"id": "K02", "seam": "continuous", "frames": [545, 1124], "render_frames": [515, 1124]},
            {"id": "K03", "seam": "cut", "frames": [1124, 3545], "render_frames": [1124, 3545]}]}
        lines = assemble.timeline(row, 30.0)
        self.assertEqual(lines[0]["out"][0], 0)
        self.assertEqual(lines[-1]["out"][1], round(3545 / 2))
        for a, b in zip(lines, lines[1:]):
            self.assertEqual(a["body"][1], b["body"][0])
        self.assertIn(lines[1]["body"][0] - lines[1]["out"][0], (14, 15))


class NetTests(unittest.TestCase):
    def test_multipart(self):
        body, content_type = net.multipart([("model", "m"), ("image[]", ("a.png", b"\x89PNG", "image/png"))])
        boundary = content_type.split("boundary=")[1]
        self.assertIn(f"--{boundary}--".encode(), body)
        self.assertIn(b'name="image[]"; filename="a.png"', body)
        self.assertIn(b"\x89PNG", body)

    def test_double_encoded_error(self):
        inner = {"type": "error", "error": {"type": "winky_permission_error", "message": "no"}}
        self.assertEqual(net.decode_json(json.dumps(json.dumps(inner)).encode()), inner)


class CredentialTests(unittest.TestCase):
    def test_round_trip(self):
        from reskin import credentials
        with tempfile.TemporaryDirectory() as folder:
            old = os.environ.get("VIDEO_RESKIN_HOME")
            os.environ["VIDEO_RESKIN_HOME"] = folder
            saved_env = os.environ.pop(credentials.ENV_KEY, None)
            try:
                with self.assertRaises(credentials.MissingCredential):
                    credentials.winky_key()
                credentials.save_winky_key("  lumos-test-key  ")
                self.assertEqual(credentials.winky_key(), "lumos-test-key")
                self.assertNotIn("lumos-test-key", Path(folder, "credentials.json").read_text(encoding="utf-8"))
                credentials.save_lumos_login("token-1", {"user_id": "u", "name": "名字"})
                self.assertEqual(credentials.lumos_refresh_token(), "token-1")
                self.assertEqual(credentials.summary()["lumos_user"]["name"], "名字")
                credentials.forget_lumos_login()
                with self.assertRaises(credentials.MissingCredential):
                    credentials.lumos_refresh_token()
            finally:
                if old is None:
                    os.environ.pop("VIDEO_RESKIN_HOME", None)
                else:
                    os.environ["VIDEO_RESKIN_HOME"] = old
                if saved_env is not None:
                    os.environ[credentials.ENV_KEY] = saved_env


class ReviewTests(unittest.TestCase):
    def test_issue_maps_to_segments(self):
        assembled = {"segments": [{"id": "K01", "start": 0, "end": 10}, {"id": "K02", "start": 10, "end": 20}]}
        self.assertEqual(review.segment_for(3, 4, assembled), ["K01"])
        self.assertEqual(review.segment_for(9.9, 10.1, assembled), ["K01", "K02"])
        self.assertEqual(review.segment_for(25, 26, assembled), ["K02"])


if __name__ == "__main__":
    unittest.main()
