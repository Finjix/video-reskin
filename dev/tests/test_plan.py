"""Segmentation: lengths stay inside Seedance's limits, seams land on cuts, overlaps only inside shots."""
import unittest

from reskin import plan


def catalog_row(duration=60.0, fps=30.0, cuts=()):
    bounds = [0, *[round(c * fps) for c in cuts], round(duration * fps)]
    shots = [{"n": i + 1, "start": a / fps, "end": b / fps, "frames": [a, b]} for i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:]))]
    return {"id": "S01", "duration": duration, "info": {"fps": fps, "frames": bounds[-1], "width": 1080, "height": 1920},
            "shots": shots, "end_card": None}


class SegmentTests(unittest.TestCase):
    def check(self, segments, fps, keep, maximum):
        self.assertEqual(segments[0]["frames"][0], round(keep[0] * fps))
        self.assertEqual(segments[-1]["frames"][1], round(keep[1] * fps))
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a["frames"][1], b["frames"][0])
        for seg in segments:
            length = (seg["render_frames"][1] - seg["render_frames"][0]) / fps
            self.assertLessEqual(length, maximum + 1e-6, seg)
            self.assertGreaterEqual(length, plan.MIN_SECONDS - 1e-6, seg)
            if seg["seam"] == "continuous":
                self.assertEqual(seg["frames"][0] - seg["render_frames"][0], round(plan.OVERLAP_SECONDS * fps))
            else:
                self.assertEqual(seg["frames"][0], seg["render_frames"][0])

    def test_single_long_shot(self):
        row = catalog_row(63.6, 60.0)
        motion = [1.0] * row["info"]["frames"]
        segments = plan.segment(row, [0, 59.08], 12.0, motion, [])
        self.check(segments, 60.0, [0, 59.08], 12.0)
        self.assertEqual(segments[0]["seam"], "start")
        self.assertTrue(all(s["seam"] == "continuous" for s in segments[1:]))

    def test_cuts_become_seams(self):
        row = catalog_row(40.0, 30.0, cuts=(8.0, 19.0, 30.0))
        segments = plan.segment(row, [0, 40.0], 12.0, [], [])
        self.check(segments, 30.0, [0, 40.0], 12.0)
        self.assertEqual([s["seam"] for s in segments], ["start", "cut", "cut", "cut"])

    def test_short_shots_are_packed(self):
        row = catalog_row(20.0, 30.0, cuts=(2.0, 4.0, 6.0, 8.0, 10.0, 12.0))
        segments = plan.segment(row, [0, 20.0], 12.0, [], [])
        self.check(segments, 30.0, [0, 20.0], 12.0)
        self.assertLess(len(segments), 4)
        self.assertTrue(any(s["inner_cuts"] for s in segments))

    def test_split_prefers_still_moment(self):
        fps = 30.0
        row = catalog_row(20.0, fps)
        motion = [10.0] * row["info"]["frames"]
        for i in range(round(9.5 * fps), round(10.5 * fps)):
            motion[i] = 0.0
        segments = plan.segment(row, [0, 20.0], 12.0, motion, [])
        self.check(segments, fps, [0, 20.0], 12.0)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[1]["frames"][0] / fps, 10.0, delta=.6)

    def test_split_inside_a_shot_is_a_continuous_seam(self):
        row = catalog_row(65.7, 30.0)
        segments = plan.segment(row, [0.0, 65.7], 30.0, [], [47.5])
        self.assertEqual(segments[-1]["start"], 47.5)
        self.assertEqual(segments[-1]["seam"], "continuous")
        self.assertEqual(segments[-1]["render_frames"][0], round(47.5 * 30) - round(plan.OVERLAP_SECONDS * 30))
        self.assertTrue(all(not s["inner_cuts"] for s in segments))
        self.assertTrue(all(s["seconds"] <= 30.0 for s in segments))

    def test_keep_range_respected(self):
        row = catalog_row(65.0, 30.0, cuts=(10.33,))
        segments = plan.segment(row, [2.0, 61.0], 12.0, [], [])
        self.check(segments, 30.0, [2.0, 61.0], 12.0)


class EditsTests(unittest.TestCase):
    def test_rejects_bad_keep(self):
        catalog = {"sources": [catalog_row()]}
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {"keep": [10, 5]}}}, {}, catalog)
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S09": {}}}, {}, catalog)

    def test_timed_removal_items(self):
        catalog = {"sources": [catalog_row()]}
        plan.check_edits({"sources": {"S01": {"remove": [{"what": "logo", "start": 1, "end": 5}]}}}, {}, catalog)
        for bad in ([{"what": "logo", "start": 5, "end": 1}], [{"text": "logo"}], ["  "], "logo"):
            with self.assertRaises(ValueError):
                plan.check_edits({"sources": {"S01": {"remove": bad}}}, {}, catalog)

    def test_defaults_merge(self):
        catalog = {"sources": [catalog_row()]}
        edits = plan.check_edits({"defaults": {"remove": ["底部警示语"]},
                                  "sources": {"S01": {"remove": ["游戏logo"], "keep": [0, 50]}}}, {}, catalog)
        row = plan.source_edits(edits, "S01", catalog["sources"][0], {"default_materials": "x"})
        self.assertEqual(row["remove"], ["底部警示语", "游戏logo"])
        self.assertEqual(row["keep"], [0.0, 50.0])
        self.assertEqual(row["materials"], "x")

    def test_style_ref(self):
        import tempfile
        from pathlib import Path
        catalog = {"sources": [catalog_row()]}
        with tempfile.TemporaryDirectory() as folder:
            video, image = Path(folder) / "ref.mp4", Path(folder) / "ref.jpg"
            video.write_bytes(b"x")
            image.write_bytes(b"x")
            edits = plan.check_edits({"defaults": {"style_ref": {"path": str(video), "at": 3, "box": [0, .1, 1, .8]}},
                                      "sources": {"S01": {}}}, {}, catalog)
            row = plan.source_edits(edits, "S01", catalog["sources"][0], {"default_materials": "x"})
            self.assertEqual(row["style_ref"]["at"], 3)
            edits = plan.check_edits({"sources": {"S01": {"style_ref": str(image)}}}, {}, catalog)
            row = plan.source_edits(edits, "S01", catalog["sources"][0], {"default_materials": "x"})
            self.assertEqual(row["style_ref"], {"path": str(image)})
            for bad in ({"path": str(video)}, {"path": str(Path(folder) / "missing.jpg")},
                        {"path": str(image), "box": [.5, 0, .4, 1]}, {"path": str(image), "zoom": 2}):
                with self.assertRaises(ValueError):
                    plan.check_edits({"sources": {"S01": {"style_ref": bad}}}, {}, catalog)


def twin_catalog(duration_b=60.0):
    a = catalog_row(60.0, 30.0, cuts=(20.0,))
    b = {**catalog_row(duration_b, 30.0, cuts=(20.0,)), "id": "S02"}
    return {"sources": [a, b]}


class LikeTests(unittest.TestCase):
    TWIN = {"keep": [0, 59.9], "materials": ["whole tuna"], "extra": "The snow stays white.", "anchor": 30,
            "split_at": [40], "remove": [{"what": "the logo top right", "box": [.7, 0, 1, .1]}],
            "ui": [{"what": "the coin counter at the top left", "box": [0, 0, .2, .05]}]}

    def edits(self, own, catalog=None):
        catalog = catalog or twin_catalog()
        edits = plan.check_edits({"sources": {"S01": dict(self.TWIN), "S02": own}}, {}, catalog)
        return plan.source_edits(edits, "S02", catalog["sources"][1], {"default_materials": "x"})

    def test_inherits_times_and_texts_not_boxes(self):
        row = self.edits({"like": "S01", "remove": [{"what": "the logo bottom", "box": [0, .9, .4, 1]}]})
        self.assertEqual(row["keep"], [0.0, 59.9])
        self.assertEqual(row["materials"], ["whole tuna"])
        self.assertEqual(row["extra"], "The snow stays white.")
        self.assertEqual((row["anchor"], row["split_at"]), (30, [40]))
        self.assertEqual(row["style_from"], "S01")
        self.assertEqual(row["remove"], [{"what": "the logo bottom", "box": [0, .9, .4, 1]}])
        self.assertEqual(row["ui"], [])
        self.assertEqual(row["like"], "S01")

    def test_own_fields_win(self):
        row = self.edits({"like": "S01", "extra": "Own.", "anchor": 12, "style_from": None})
        self.assertEqual((row["extra"], row["anchor"], row["style_from"]), ("Own.", 12, "S01"))

    def test_inherited_keep_ends_with_the_shorter_twin(self):
        row = self.edits({"like": "S01"}, twin_catalog(59.8))
        self.assertEqual(row["keep"], [0.0, 59.8])

    def test_rejected(self):
        for own, catalog in (({"like": "S02"}, None), ({"like": "S09"}, None), ({"like": "S01"}, twin_catalog(55.0))):
            with self.assertRaises(ValueError):
                self.edits(own, catalog)
        self.edits({"like": "S01", "keep": [0, 54]}, twin_catalog(55.0))
        catalog = {"sources": [*twin_catalog()["sources"], {**catalog_row(), "id": "S03"}]}
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {}, "S02": {"like": "S01"}, "S03": {"like": "S02"}}}, {}, catalog)
        with self.assertRaises(ValueError):
            plan.check_edits({"sources": {"S01": {"skip": True}, "S02": {"like": "S01"}}}, {}, catalog)

    def test_segments_with_equal_spans_are_linked(self):
        a = {"like": None, "segments": plan.segment(catalog_row(60.0, 30.0, cuts=(20.0,)), [0, 60], 30, [], [40])}
        b = {"like": "S01", "segments": plan.segment(catalog_row(60.0, 30.0, cuts=(20.0,)), [0, 60], 30, [], [40])}
        b["segments"][-1]["end"] += 1      # a span the twin does not have
        value = {"sources": {"S01": a, "S02": b}}
        plan.link_twins(value)
        self.assertEqual([s.get("like") for s in b["segments"]], ["S01:K01", "S01:K02", None])
        self.assertTrue(all("like" not in s for s in a["segments"]))

    def test_corrections_carry_over_until_replaced(self):
        import tempfile
        from pathlib import Path
        from reskin import render
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            render.bump(job, "S01:K02", "tree turned cartoon", "Keep the trees real.")
            render.bump(job, "S02:K02", "logo", "Remove the logo.")
            seg = {"id": "K02", "like": "S01:K02"}
            self.assertEqual(render.corrections(job, "S02:K02", seg), "Keep the trees real.\nRemove the logo.")
            self.assertEqual(render.corrections(job, "S02:K03", {"id": "K03"}), "")
            render.bump(job, "S02:K02", "too much", "Only the logo.", replace=True)
            self.assertEqual(render.corrections(job, "S02:K02", seg), "Only the logo.")

    def test_twin_waits(self):
        import tempfile
        from pathlib import Path
        try:
            from reskin import render
            render.twin_waits     # needs assemble/review (NumPy, Pillow)
            import numpy, PIL     # noqa: F401
        except ImportError:
            self.skipTest("media packages missing")
        from reskin.common import write
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            value = {"sources": {"S01": {"like": None, "segments": [{"id": "K01", "seam": "start", "seconds": 5}]},
                                 "S02": {"like": "S01", "segments": [{"id": "K01", "seam": "start", "seconds": 5}]}}}
            self.assertIn("delivered and reviewed", render.twin_waits(job, {}, value)["S02"])
            self.assertEqual(render.twin_waits(job, {}, value, {"S02"}), {})
            write(render.state_path(job, "S01:K01"), {"status": "done", "attempt": 1})
            render.bump(job, "S01:K01", "seam", "Fix it.")
            self.assertIn("redos of S01 (K01)", render.twin_waits(job, {}, value)["S02"])
            self.assertEqual(render.plan_order(value, hold={"S02"})[0][0], "S01")
            self.assertEqual(len(render.plan_order(value, hold={"S02"})), 1)


if __name__ == "__main__":
    unittest.main()
