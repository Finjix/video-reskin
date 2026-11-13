"""Small pure helpers: video-edit requests, sizes, HTTP encoding and credentials."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reskin import net, pipeline


class VideoEditRequestTests(unittest.TestCase):
    def test_edit_uses_input_video_length(self):
        from reskin import lumos
        with patch.object(lumos, 'call', return_value={}) as submit:
            lumos.submit_video_edit({'path': 'clip.mp4'}, [], 'Edit materials')
        body = submit.call_args.args[2]
        self.assertEqual(body['generation_mode'], 'video_edit')
        self.assertEqual(body['frame_count'], -1)
        self.assertNotIn('duration', body)


class SizeTests(unittest.TestCase):
    def test_target_sizes(self):
        self.assertEqual(pipeline.target_size(1080, 1920), (1080, 1920))
        self.assertEqual(pipeline.target_size(1920, 1080), (1920, 1080))
        self.assertEqual(pipeline.target_size(3840, 2160), (1920, 1080))
        self.assertEqual(pipeline.target_size(1080, 1350), (1080, 1350))

    def test_image_aspects(self):
        self.assertEqual(pipeline.aspect_for(1920, 1080), "16:9")
        self.assertEqual(pipeline.aspect_for(1080, 1920), "9:16")
        self.assertEqual(pipeline.aspect_for(1080, 3000), "9:16")          # the nearest preset (4K needs one)


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


if __name__ == "__main__":
    unittest.main()
