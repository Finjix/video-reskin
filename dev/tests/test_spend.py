"""What protects the money: single submissions, the job lock, causes and the ledger, the segment check holding a
chain, longest chains first, local patches, lint drafts and previews."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reskin import cli, common, gate, ledger, lint, lumos, net, patch, preview, render, review
from test_plan import catalog_row
from reskin import plan as PLAN


def tmp() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="vr-test-"))
    return folder


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch("reskin.lumos.time.sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_lost_reply_is_found_in_the_history(self):
        calls = []

        def call(method, path, value=None, **kw):
            calls.append((method, path))
            if path == "/generations/video-to-video":
                raise net.NetworkError("timed out")
            return {"data": [{"generation_id": "g-1", "generation_config": {
                "reference_videos": [{"media_id": "m-1"}]}}]}
        with mock.patch("reskin.lumos.call", call):
            result = lumos.submit("/generations/video-to-video", {}, media_id="m-1", generation_type="video-to-video")
        self.assertEqual(result["generation_id"], "g-1")
        self.assertEqual(sum(1 for c in calls if c[1] == "/generations/video-to-video"), 1)

    def test_submission_is_sent_once_per_try(self):
        seen = {}

        def call(method, path, value=None, **kw):
            seen["attempts"] = kw.get("attempts")
            return {"generation_id": "g-2"}
        with mock.patch("reskin.lumos.call", call):
            lumos.submit("/generations/video-to-video", {}, media_id="m", generation_type="video-to-video")
        self.assertEqual(seen["attempts"], 1)

    def test_bad_request_is_final(self):
        calls = []

        def call(method, path, value=None, **kw):
            calls.append(path)
            raise net.HttpError(400, "bad prompt", path)
        with mock.patch("reskin.lumos.call", call), self.assertRaises(net.HttpError):
            lumos.submit("/generations/video-to-video", {}, media_id="m", generation_type="video-to-video")
        self.assertEqual(calls, ["/generations/video-to-video"])

    def test_refusal_is_retried_without_lookup(self):
        calls = []

        def call(method, path, value=None, **kw):
            calls.append(path)
            if len(calls) == 1:
                raise lumos.Refused("queue busy")
            return {"generation_id": "g-3"}
        with mock.patch("reskin.lumos.call", call):
            result = lumos.submit("/generations/video-to-video", {}, media_id="m", generation_type="video-to-video")
        self.assertEqual(result["generation_id"], "g-3")
        self.assertEqual(calls, ["/generations/video-to-video"] * 2)

    def test_network_error_type(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("slow")), \
                mock.patch("reskin.net.time.sleep", lambda s: None):
            with self.assertRaises(net.NetworkError):
                net.request("POST", "https://example.invalid/x", body=b"{}", attempts=1)


class LockTests(unittest.TestCase):
    def test_second_render_is_refused(self):
        job = tmp()
        try:
            with common.job_lock(job, "render"):
                with self.assertRaises(common.Busy):
                    with common.job_lock(job, "render"):
                        pass
            with common.job_lock(job, "render"):
                pass
        finally:
            shutil.rmtree(job, ignore_errors=True)


class CauseTests(unittest.TestCase):
    base = {"source": "s", "frames": [0, 300], "fps": 30, "keyframe": "k", "model": "m", "resolution": "480p",
            "attempt": 1, "prompt": "p"}

    def record(self, **changes):
        inputs = {**render.core_inputs(self.base), **changes}
        return {"digest": render.core({**inputs, "prompt": "p"}), "inputs": inputs, "attempt": inputs["attempt"]}

    def test_causes(self):
        self.assertEqual(render.cause_of({}, self.base, {}), "new")
        self.assertEqual(render.cause_of(self.record(), self.base, {}), "unusable")
        self.assertEqual(render.cause_of(self.record(attempt=0), self.base, {"reasons": ["fish"]}), "redo")
        self.assertEqual(render.cause_of(self.record(attempt=0), self.base, {"reasons": ["automatic retry after: x"]}),
                         "auto-retry")
        chained = {**{k: v for k, v in self.base.items() if k != "keyframe"}, "chain": "g-new"}
        old = {**{k: v for k, v in render.core_inputs(self.base).items() if k != "keyframe"}, "chain": "g-old"}
        self.assertEqual(render.cause_of({"digest": "x", "inputs": old}, chained, {}), "upstream")
        self.assertEqual(render.cause_of(self.record(keyframe="k0"), self.base, {}), "keyframe")
        self.assertEqual(render.cause_of(self.record(frames=[0, 290]), self.base, {}), "plan")
        self.assertEqual(render.cause_of(self.record(resolution="1080p"), self.base, {}), "resolution")


class LedgerTests(unittest.TestCase):
    def test_summary(self):
        job = tmp()
        try:
            ledger.add(job, kind="video", event="submit", key="S01:K01", seconds=20, resolution="480p", cause="new",
                       generation_id="a")
            ledger.add(job, kind="video", event="submit", key="S01:K02", seconds=10, resolution="480p", cause="redo",
                       generation_id="b")
            ledger.add(job, kind="video", event="failed", key="S01:K02", generation_id="b")
            ledger.add(job, kind="image", event="submit", purpose="preview", key="S01@5")
            ledger.add(job, kind="gemini", purpose="gate", key="S01:K01", seconds=20)
            value = ledger.summary(job)
            self.assertEqual(value["video_seconds"], 30)
            self.assertEqual(value["by_cause"], {"new": 20, "redo": 10})
            self.assertEqual(value["failed_or_unusable_seconds"], 10)
            self.assertEqual(value["images"], {"preview": 1})
            self.assertEqual(value["gemini"], {"gate": 1})
            self.assertTrue(any("redo 10" in line for line in ledger.markdown(value)))
        finally:
            shutil.rmtree(job, ignore_errors=True)


def three_segment_plan():
    segs = [{"id": "K01", "seam": "start", "seconds": 20}, {"id": "K02", "seam": "continuous", "seconds": 20},
            {"id": "K03", "seam": "cut", "seconds": 5}]
    return {"sources": {"S01": {"segments": segs}, "S02": {"segments": [{"id": "K01", "seam": "start", "seconds": 8}]}}}


class GateTests(unittest.TestCase):
    def test_longest_chain_first(self):
        order = [(sid, [s["id"] for s in members]) for sid, _, members in render.plan_order(three_segment_plan())]
        self.assertEqual(order, [("S01", ["K01", "K02"]), ("S02", ["K01"]), ("S01", ["K03"])])

    def test_held_segment_stops_its_chain_only(self):
        rendered, checked, finished = [], [], []

        def fake_render(job, state, row, sid, seg, style, previous, prev_seg):
            rendered.append(f"{sid}:{seg['id']}")
            return {"key": f"{sid}:{seg['id']}", "generation_id": "g"}

        def fake_check(job, state, row, sid, index):
            checked.append(f"{sid}:{row['segments'][index]['id']}")
            return {"held": True, "accepted": False, "reasons": ["Gemini: [high] continuity"]}
        with mock.patch("reskin.render.render_segment", fake_render), mock.patch("reskin.gate.check", fake_check), \
                mock.patch("reskin.patch.render_all", lambda *a: []):
            results = render.run_all(Path(tempfile.gettempdir()), {}, three_segment_plan(), {}, workers=1,
                                     on_source_done=lambda sid, rows: finished.append(sid))
        self.assertEqual(sorted(rendered), ["S01:K01", "S01:K03", "S02:K01"])
        self.assertEqual(checked, ["S01:K01"])          # only segments with a successor in their shot
        waiting = next(r for r in results if r["key"] == "S01:K02")
        self.assertTrue(waiting["waiting"])
        self.assertNotIn("error", waiting)
        self.assertEqual(sorted(finished), ["S01", "S02"])

    def test_accept(self):
        job = tmp()
        try:
            path = gate.record_path(job, "S01:K01")
            common.write(path, {"generation_id": "g", "gate": {"generation_id": "g", "held": True}})
            self.assertTrue(gate.holds(gate.stored(job, "S01:K01")))
            gate.accept(job, "S01:K01", "原片也这样")
            self.assertFalse(gate.holds(gate.stored(job, "S01:K01")))
            common.write(path, {**common.read(path), "generation_id": "g2"})
            self.assertIsNone(gate.stored(job, "S01:K01"))     # a new render is checked anew
        finally:
            shutil.rmtree(job, ignore_errors=True)

    def test_judge(self):
        self.assertEqual(review.judge([{"severity": "low"}] * 5), "pass")
        self.assertEqual(review.judge([{"severity": "medium"}] * 2), "fail")
        self.assertEqual(review.judge([{"severity": "high"}]), "fail")
        self.assertEqual(review.auto_fps(60), 4.0)
        self.assertEqual(review.auto_fps(120), 2.0)


class PatchTests(unittest.TestCase):
    row = {"fps": 30, "keep": [0.0, 60.0]}
    seg = {"id": "K01", "frames": [0, 900], "render_frames": [0, 900]}

    def test_window_is_at_least_four_seconds(self):
        a, b = patch.window(self.row, self.seg, 10.0, 10.4, following=True)
        self.assertGreaterEqual(b - a, 120)
        self.assertLessEqual(a, round(9.5 * 30))
        self.assertGreaterEqual(b, round(10.9 * 30))

    def test_window_slides_away_from_the_edges(self):
        a, b = patch.window(self.row, self.seg, 29.5, 29.8, following=True)
        self.assertLessEqual(b, 900 - round(.75 * 30))      # clear of the next segment's overlap
        a, b = patch.window(self.row, self.seg, 0.1, 0.2, following=False)
        self.assertGreaterEqual(a, round(.25 * 30))

    def test_window_too_long(self):
        with self.assertRaises(ValueError):
            patch.window(self.row, self.seg, 5, 15, following=False)

    def test_add_and_drop(self):
        job = tmp()
        try:
            plan = {"sources": {"S01": {**self.row, "segments": [dict(self.seg, seam="start")]}}}
            common.write(job / "state/render/S01-K01.json", {"status": "done", "generation_id": "g1"})
            entry = patch.add(job, plan, "S01:K01", 12.0, 12.5, "鱼变形", "鱼保持原样")
            self.assertEqual(entry["id"], "P1")
            with self.assertRaises(ValueError):
                patch.add(job, plan, "S01:K01", 13.0, 13.2, "overlap", None)
            self.assertEqual(len(patch.active(job, "S01:K01")), 1)
            common.write(job / "state/render/S01-K01.json", {"status": "done", "generation_id": "g2"})
            self.assertEqual(patch.active(job, "S01:K01"), [])      # made for the earlier render
            self.assertTrue(patch.drop(job, "S01:K01:P1"))
        finally:
            shutil.rmtree(job, ignore_errors=True)


class LintTests(unittest.TestCase):
    def test_draft(self):
        row = catalog_row(70.2, 30.0)
        row["end_card"] = {"start": 65.7}
        facts = {"end_card_start": 65.7,
                 "objects": [{"what": "积雪地面", "start": 0, "end": 70, "always": True},
                             {"what": "蓝色皮卡车", "start": 56, "end": 65.7, "always": False}],
                 "logos": [{"what": "右上角的游戏logo「WHITEOUT SURVIVAL」", "start": 0, "end": 36, "box": [800, 20, 980, 90]}],
                 "screen_text": [{"what": "底部字幕", "text": "钓到了！", "start": 3, "end": 6, "box": [.1, .8, .9, .9], "fixed": True},
                                 {"what": "地面价格牌", "text": "$300", "start": 0, "end": 70, "box": [.4, .5, .5, .55], "fixed": False}],
                 "hud": [{"what": "金币计数", "start": 0, "end": 70, "box": [0, .1, .28, .15]}]}
        drafted = lint.draft(facts, row)
        self.assertEqual(drafted["keep"], [0.0, 65.7])
        self.assertEqual(drafted["materials"][0], "积雪地面")
        self.assertEqual(drafted["materials"][1], {"what": "蓝色皮卡车", "start": 56, "end": 65.7})
        self.assertEqual(drafted["remove"][0]["start"], 0)
        self.assertEqual(len(drafted["overlays"]), 1)
        self.assertEqual(drafted["overlays"][0]["box"], [.1, .8, .9, .9])
        self.assertIsNone(PLAN.overlay_problem(drafted["overlays"]))
        self.assertIsNone(PLAN.hud_problem(drafted["hud"]))
        self.assertIsNone(PLAN.items_problem(drafted["materials"], "materials"))
        self.assertIn("edits_check", lint.prompt(row, {"materials": ["三文鱼"]}))


class PreviewTests(unittest.TestCase):
    def test_moments(self):
        row = {"fps": 30, "keep": [0.0, 65.0],
               "segments": [{"frames": [0, 990], "render_frames": [0, 990]},
                            {"frames": [990, 1950], "render_frames": [975, 1950]}],
               "materials": ["积雪地面", {"what": "蓝色皮卡车", "start": 56, "end": 65}]}
        times = preview.moments(row)
        self.assertIn(56.5, times)
        self.assertTrue(all(abs(t - 32.5) > 1.5 and t > 1.5 for t in times))     # not at segment starts
        self.assertLessEqual(len(times), preview.LIMIT)


class RedoTests(unittest.TestCase):
    def test_keyframe_redo_of_a_chained_segment_is_refused(self):
        job = tmp()
        try:
            source = job / "a.mp4"
            source.write_bytes(b"x")
            common.write(job / "job.json", {"sources": [{"id": "S01", "path": str(source), "size": 1}], "options": {}})
            common.write(job / "config/plan.json", {"sources": {"S01": {"segments": [
                {"id": "K01", "seam": "start"}, {"id": "K02", "seam": "continuous"}]}}})
            args = mock.Mock(job_dir=job, segment=["S01:K02"], reason="r", prompt=None, keyframe=True)
            with self.assertRaises(ValueError) as caught:
                cli.cmd_redo(args)
            self.assertIn("S01:K01", str(caught.exception))
            self.assertFalse((job / "config/redo.json").exists())
        finally:
            shutil.rmtree(job, ignore_errors=True)


    def test_corrective_sentences_accumulate_unless_replaced(self):
        job = tmp()
        try:
            render.bump(job, "S01:K02", "r1", "吊臂变为红色。")
            render.bump(job, "S01:K02", "r2", "鱼保持金黄色。")
            row = common.read(job / "config/redo.json")["S01:K02"]
            self.assertEqual(row["prompt"].splitlines(), ["吊臂变为红色。", "鱼保持金黄色。"])
            row = render.bump(job, "S01:K02", "r3", "只有吊臂的金属结构变红，鱼不变色。", replace=True)
            self.assertEqual((row["attempt"], row["prompt"]), (4, "只有吊臂的金属结构变红，鱼不变色。"))
            row = render.bump(job, "S01:K02", "r4", None, replace=True)
            self.assertNotIn("prompt", row)
        finally:
            shutil.rmtree(job, ignore_errors=True)


class DurableTests(unittest.TestCase):
    def test_unreadable_json_names_the_file(self):
        job = tmp()
        try:
            broken = job / "work/S01-K01.json"
            broken.parent.mkdir(parents=True)
            broken.write_bytes(bytes(117))      # what a power cut left behind: zero bytes
            with self.assertRaises(ValueError) as caught:
                common.read(broken)
            self.assertIn("S01-K01.json", str(caught.exception))
            common.write(broken, {"ok": 1})
            self.assertEqual(common.read(broken), {"ok": 1})
        finally:
            shutil.rmtree(job, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
