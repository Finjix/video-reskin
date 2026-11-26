"""Seedance segment retries, provenance checks and real short upload clips."""
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from PIL import Image

from reskin import element_seedance as seedance, elements, lumos, net, pipeline
from reskin.common import FFMPEG, read, run, sha, video_info, write


class SeedanceSegmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        write(self.folder / "job.json", {})
        video = self.folder / "source.mp4"
        # Exactly one white picture among the black pictures distinguishes the selected index from either neighbour.
        pipeline.ff(["-f", "lavfi", "-i", "color=black:s=64x48:r=120:d=0.25", "-vf",
                     "drawbox=c=white:t=fill:enable='eq(n,10)'", "-c:v", "libx264"], video)
        self.source = {"path": str(video), "sha256": sha(video)}
        self.times = elements.packet_times(video)
        self.info = video_info(video)
        reference = self.folder / "reference.png"
        Image.new("RGB", (32, 32), "white").save(reference)
        self.segment = {"range": [10, 11], "references": [str(reference)], "prompt": "Change the crate to a cooler."}
        self.inputs = {}
        self.enterContext(patch.object(lumos, "upload", side_effect=lambda p, kind: {"path": str(p)}))
        self.submit = self.enterContext(patch.object(lumos, "submit_video_edit", side_effect=self.start))
        self.wait = self.enterContext(patch.object(lumos, "wait", side_effect=self.success))
        self.download = self.enterContext(patch.object(net, "download", side_effect=self.copy_result))

    def start(self, video, images, prompt, *, resolution, model):
        self.assertTrue(images)
        self.assertIn(lumos.CASH_GUIDANCE, prompt)
        self.assertEqual((resolution, model), (seedance.RESOLUTION, seedance.MODEL))
        generation_id = f"g{len(self.inputs) + 1}"
        self.inputs[generation_id] = Path(video["path"])
        return {"generation_id": generation_id}

    @staticmethod
    def success(generation_id):
        return {"status": "Succeeded", "generation_id": generation_id,
                "generation_config": {"model": seedance.MODEL, "generation_mode": "video_edit"},
                "generations": {"media_result": [{"media_url": "https://test.invalid/" + generation_id}]}}

    def copy_result(self, url, path):
        shutil.copyfile(self.inputs[url.rsplit("/", 1)[-1]], path)
        return path

    def obtain(self, *segments):
        return seedance.obtain(self.folder, self.source, {"seedance": {"segments": list(segments)}},
                               self.times, self.info, budget_job=self.folder)

    def request(self, sequence=1):
        return next((self.folder / "seedance").glob(f"{sequence:02}-*/request.json"))

    def test_new_and_legacy_orders_submit_and_reuse_without_a_job_limit(self):
        for index, order in enumerate(({}, {"generation_limit": 0}), 1):
            write(self.folder / "job.json", order)
            segment = {**self.segment, "prompt": f"design {index}"}
            self.assertEqual(self.obtain(segment)[0]["generation_id"], f"g{index}")
            self.assertEqual(self.obtain(segment)[0]["generation_id"], f"g{index}")
            self.assertEqual(self.submit.call_count, index)

    def test_reference_upload_failure_never_reserves_a_paid_attempt(self):
        with patch.object(lumos, "upload", side_effect=OSError("upload interrupted")):
            with self.assertRaises(OSError):
                self.obtain(self.segment)
        self.assertFalse((self.folder / "elements/submissions.json").exists())
        self.submit.assert_not_called()
        self.assertEqual(self.obtain(self.segment)[0]["generation_id"], "g1")

    def test_range_cap_counts_changed_prompts_references_ambiguous_posts_and_shifted_cuts(self):
        first = {**self.segment, "range": [3, 15]}
        self.obtain(first)
        second = {**first, "prompt": "Use a second reference."}
        image = self.folder / "second.png"
        Image.new("RGB", (32, 32), "blue").save(image)
        second["references"] = [str(image)]
        self.obtain(second)
        self.submit.side_effect = TimeoutError("reply lost after the paid POST")
        with self.assertRaises(TimeoutError):
            self.obtain({**second, "prompt": "Change the sidewalls."})
        self.submit.side_effect = self.start
        with patch.object(lumos, "upload", side_effect=AssertionError("exhausted interval should not be uploaded")):
            for changed in ([3, 15], [4, 16], [6, 9], [0, 19]):
                with self.subTest(span=changed), self.assertRaisesRegex(ValueError, "3-submission"):
                    self.obtain({**second, "range": changed, "prompt": "Replan this span."})
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(self.obtain(first)[0]["generation_id"], "g1")
        self.obtain({**first, "range": [15, 24]})
        self.assertEqual(self.submit.call_count, 4)

    def test_three_service_failures_are_the_maximum_including_the_initial_submission(self):
        def fail(gid):
            raise lumos.GenerationFailed({"generation_id": gid, "status": "Failed"})

        self.wait.side_effect = fail
        for _ in range(3):
            with self.assertRaises(lumos.GenerationFailed):
                self.obtain(self.segment)
        with self.assertRaisesRegex(ValueError, "3-submission"):
            self.obtain(self.segment)
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(read(self.request())["failed_generation_ids"], ["g1", "g2"])

    def test_reservations_serialize_and_follow_a_variant_through_plan_reordering(self):
        order = {"variants": [{"id": "V01", "name": "wood"}, {"id": "V02", "name": "ice"}]}
        write(self.folder / "job.json", order)
        barrier = Barrier(6)
        def reserve(i):
            barrier.wait()
            try:
                seedance.reserve_submission(self.folder, str(i), "source", "V01", [0, 30])
            except ValueError as error:
                self.assertIn("3-submission", str(error))
                return False
            return True

        with ThreadPoolExecutor(6) as pool:
            self.assertEqual(sum(pool.map(reserve, range(6))), 3)
        order["variants"] = [{"id": "V01", "name": "ice"}, {"id": "V02", "name": "WOOD"}]
        write(self.folder / "job.json", order)
        with self.assertRaisesRegex(ValueError, "3-submission"):
            seedance.reserve_submission(self.folder, "reordered", "source", "V02", [1, 30])
        seedance.reserve_submission(self.folder, "other-output", "source", "V01", [0, 30])
        seedance.reserve_submission(self.folder, "other-source", "other-source", "V02", [0, 30])
        seedance.reserve_submission(self.folder, "next", "source", "V02", [30, 60])

    def test_legacy_receipts_and_ambiguous_key_only_reservations_do_not_reset_limits(self):
        seedance_dir = self.folder / "elements" / "V01" / "seedance" / "old"
        original = {"key": "old-key", "source_sha256": "source", "range": [0, 30],
                    "generation_id": "rejected-2", "failed_generation_ids": ["rejected-1"]}
        write(seedance_dir / "request.json", original)
        ledger = self.folder / "elements" / "submissions.json"
        write(ledger, ["old-key"] * 3)  # The third POST timed out without an ID.
        with self.assertRaisesRegex(ValueError, "3-submission"):
            seedance.reserve_submission(self.folder, "new-key", "source", "V01", [0, 29])
        seedance.reserve_submission(self.folder, "adjacent", "source", "V01", [30, 60])
        history = read(ledger)
        self.assertEqual(len(history), 4)
        self.assertTrue(all(row["range"] == [0, 30] for row in history[:3]))

    def test_merging_adjacent_segments_counts_coverage_per_frame(self):
        def reserve(key, span):
            seedance.reserve_submission(self.folder, key, "source", "V01", span)
        for i in range(2):
            reserve(f"left-{i}", [0, 30])
            reserve(f"right-{i}", [30, 60])
        reserve("whole", [0, 60])
        with self.assertRaisesRegex(ValueError, "3-submission"):
            reserve("normalized-again", [1, 59])

    def test_one_high_rate_source_picture_survives_padding_and_recovers_bad_old_cache(self):
        path = self.folder / "one" / "original.mp4"
        path.parent.mkdir()
        # This is the video-less MP4 the old filter chain cached when the sole frame was shorter than 1/24 s.
        pipeline.ff(["-i", self.source["path"], "-an", "-vf",
                     "trim=start_frame=10:end_frame=11,setpts=PTS-STARTPTS,fps=24", "-c:v", "libx264"], path)
        with self.assertRaisesRegex(ValueError, "no video stream"):
            video_info(path)
        clip = seedance.original_clip(path.parent, self.source, [10, 11], 96)
        info = video_info(clip)
        self.assertEqual((info["fps"], info["frames"], info["duration"]), (24, 96, 4))
        pixels = run([FFMPEG, "-v", "error", "-i", clip, "-vf", "scale=1:1", "-map", "0:v:0", "-pix_fmt", "rgb24",
                      "-f", "rawvideo", "-"], binary=True)
        self.assertEqual(len(pixels), 96 * 3)
        self.assertGreater(min(pixels), 235)
        with patch.object(seedance, "run", side_effect=AssertionError("valid clip was encoded again")):
            self.assertEqual(seedance.original_clip(path.parent, self.source, [10, 11], 96), clip)

    def test_interruptions_retry_the_same_id_and_repair_download_without_resubmission(self):
        self.wait.side_effect = TimeoutError("still rendering")
        with self.assertRaises(TimeoutError):
            self.obtain(self.segment)
        self.assertEqual(read(self.request())["generation_id"], "g1")
        self.assertEqual(read(self.request())["status"], "submitted")
        self.wait.side_effect = self.success
        self.download.side_effect = OSError("connection reset")
        with self.assertRaises(OSError):
            self.obtain(self.segment)
        self.assertEqual(read(self.request())["status"], "submitted")
        self.download.side_effect = self.copy_result
        complete, = self.obtain(self.segment)
        self.assertEqual((complete["status"], complete["generation_id"]), ("succeeded", "g1"))
        self.assertEqual(complete["raw_sha256"], sha(Path(complete["raw"])))
        self.wait.reset_mock()
        self.download.reset_mock()
        self.assertEqual(self.obtain(self.segment), [complete])
        self.wait.assert_not_called()
        self.download.assert_not_called()
        Path(complete["raw"]).write_bytes(b"damaged cache")
        self.assertEqual(self.obtain(self.segment), [complete])
        self.wait.assert_called_once_with("g1")
        self.download.assert_called_once()
        self.assertEqual(self.submit.call_count, 1)

    def test_small_reference_is_scaled_for_upload_without_an_edit_to_the_original(self):
        image = Path(self.segment["references"][0])
        before = sha(image)
        with patch.object(lumos, "upload", side_effect=lambda p, kind: Image.open(p).size):
            size, evidence = seedance.upload_reference(image, self.folder, 1)
        self.assertEqual(size, (300, 300))
        self.assertEqual(before, sha(image))
        self.assertEqual(evidence["sha256"], before)
        self.assertNotEqual(evidence["uploaded_sha256"], before)

    def test_incorrect_success_or_foreign_failure_is_never_downloaded_or_marked_as_our_failure(self):
        valid = self.success("g1")
        config = valid["generation_config"]
        with patch.object(lumos, "submit_video_edit", return_value={"generation_id": ""}):
            with self.assertRaisesRegex(ValueError, "generation_id"):
                self.obtain(self.segment)
        self.wait.assert_not_called()
        for field, response in (
                ("generation_id", {**valid, "generation_id": "someone-else"}),
                ("generation_id", {key: value for key, value in valid.items() if key != "generation_id"}),
                ("model", {**valid, "generation_config": {**config, "model": "different-model"}}),
                ("generation_mode", {**valid, "generation_config": {**config, "generation_mode": "text_to_video"}}),
                ("model", {**valid, "generation_config": {"generation_mode": "video_edit"}}),
                ("generation_mode", {**valid, "generation_config": {"model": seedance.MODEL}})):
            with self.subTest(field=field, response=response):
                self.wait.side_effect = None
                self.wait.return_value = response
                with self.assertRaisesRegex(ValueError, field):
                    self.obtain(self.segment)
                self.assertEqual(read(self.request())["status"], "submitted")
                self.download.assert_not_called()
        self.wait.side_effect = lumos.GenerationFailed({"generation_id": "someone-else", "status": "Failed"})
        with self.assertRaisesRegex(ValueError, "generation_id"):
            self.obtain(self.segment)
        self.assertEqual(read(self.request())["status"], "submitted")
        self.download.assert_not_called()
        self.wait.side_effect = self.success
        self.assertEqual(self.obtain(self.segment)[0]["generation_id"], "g1")
        self.assertEqual(self.submit.call_count, 1)

    def test_inserting_and_removing_earlier_intervals_reuses_pending_and_completed_work(self):
        self.wait.side_effect = TimeoutError("pending")
        with self.assertRaises(TimeoutError):
            self.obtain(self.segment)
        original_path = self.request()
        early = {**self.segment, "range": [0, 4]}
        middle = {**self.segment, "range": [4, 9]}
        self.wait.side_effect = self.success
        first, second, original = self.obtain(early, middle, self.segment)
        self.assertEqual([r["generation_id"] for r in (first, second, original)], ["g2", "g3", "g1"])
        self.assertEqual(read(original_path), original)
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(len(list((self.folder / "seedance").glob("*/request.json"))), 3)
        self.wait.reset_mock()
        self.assertEqual(self.obtain(middle, self.segment), [second, original])
        self.wait.assert_not_called()
        self.assertEqual(self.submit.call_count, 3)

    def test_only_the_failed_interval_resubmits_and_retains_its_history(self):
        second = {**self.segment, "range": [16, 20]}
        def fail_first(generation_id):
            if generation_id == "g1":
                raise lumos.GenerationFailed({"generation_id": "g1", "status": "Failed", "error_message": "service"})
            return self.success(generation_id)

        self.wait.side_effect = fail_first
        with self.assertRaises(lumos.GenerationFailed):
            self.obtain(self.segment, second)
        self.assertEqual(read(self.request(1))["status"], "failed")
        original_second = read(self.request(2))
        self.assertEqual(original_second["status"], "succeeded")
        self.wait.side_effect = self.success
        self.wait.reset_mock()
        first, sibling = self.obtain(self.segment, second)
        self.assertEqual(self.submit.call_count, 3)
        self.wait.assert_called_once_with("g3")
        self.assertEqual(first["failed_generation_ids"], ["g1"])
        self.assertEqual(first["generation_id"], "g3")
        self.assertEqual(sibling, original_second)
