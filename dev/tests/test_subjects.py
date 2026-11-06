"""Subject reference sheets and the two-layer checks: edits validation, which sheets go where, prompt texts, still
moments, the agent's verdicts on segments and seams, and a waiting chain taken up again once the agent passes it."""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from reskin import common, gate, lint, plan, prompts, render, review
from test_plan import catalog_row


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="vr-test-"))


HERO = {"id": "hero", "what": "the protagonist in a light blue coat", "view": "2.5d",
        "from": [{"at": 5.0, "box": [0.4, 0.3, 0.6, 0.7]}]}
TRUCK = {"id": "truck", "what": "a blue pickup truck", "from": [{"at": 50.0, "box": [0.5, 0.4, 0.9, 0.8]}],
         "on": [[45.0, 60.0]]}


class EditsTests(unittest.TestCase):
    def test_subjects_are_checked(self):
        self.assertIsNone(plan.subjects_problem([HERO, TRUCK], 60.0))
        bad = [
            [dict(HERO, id="Hero")], [dict(HERO, id="anchor")], [HERO, HERO], [dict(HERO, view="4d")],
            [dict(HERO, **{"from": []})], [dict(HERO, **{"from": [{"at": 70, "box": [0, 0, 1, 1]}]})],
            [dict(HERO, **{"from": [{"at": 5, "box": [0.5, 0, 0.4, 1]}]})], [dict(TRUCK, on=[[50, 40]])],
            [dict(HERO, colour="blue")], [dict(HERO, what="")],
        ]
        for items in bad:
            self.assertIsNotNone(plan.subjects_problem(items, 60.0), items)
        live = [{"start": 0.0, "end": 8.0, "what": "a real driver"}]
        self.assertIn("live-action", plan.subjects_problem([HERO], 60.0, live))

    def test_edits_take_subjects_and_dynamic_ui(self):
        catalog = {"sources": [catalog_row(60.0, 30.0)]}
        edits = {"sources": {"S01": {"subjects": [HERO], "subject_refs": False,
                                     "ui": [{"what": "the coin counter", "box": [0, 0, .3, .1], "dynamic": True}]}}}
        self.assertEqual(plan.check_edits(edits, {}, catalog)["sources"]["S01"]["subjects"], [HERO])
        broken = {"sources": {"S01": {"ui": [{"what": "x", "dynamic": "yes"}]}}}
        with self.assertRaises(ValueError):
            plan.check_edits(broken, {}, catalog)


class SheetsTests(unittest.TestCase):
    def row(self):
        cat = catalog_row(60.0, 30.0, cuts=(30.0,))
        segments = plan.segment(cat, [0, 60.0], 30.0, [1.0] * cat["info"]["frames"], [])
        row = {"keep": [0.0, 60.0], "fps": 30.0, "segments": segments, "subjects": [HERO, TRUCK],
               "materials": ["snow", {"what": "a red crane", "start": 40, "end": 60}]}
        plan.mark_subjects(row)
        return row

    def test_each_segment_names_its_subjects(self):
        row = self.row()
        first, second = row["segments"]
        self.assertEqual(first["subjects"], ["hero"])
        self.assertEqual(first["keyframe_subjects"], ["hero"])
        self.assertEqual(second["subjects"], ["hero", "truck"])        # in order of appearance
        self.assertEqual(second["keyframe_subjects"], ["hero"])        # the truck is not there at its first frame
        self.assertEqual(second["new_subjects"], ["truck"])            # it comes into view after the reference frame
        self.assertEqual(second["late_materials"], ["a red crane"])
        self.assertEqual(row["subject_times"]["hero"], [[0.0, 60.0]])

    def test_plan_report_lists_them(self):
        row = dict(self.row(), anchor=10.0, style_from=None, remove=[], extra="", live=[])
        text = plan.markdown({"sources": {"S01": row}}, {"sources": [{"id": "S01", "name": "ad"}]})
        self.assertIn("subject truck", text)
        self.assertIn("new after its reference frame: truck", text)
        self.assertIn("without a subject sheet: a red crane", text)

    def test_clause_numbers_the_sheets(self):
        text = prompts.subjects_clause("video", [("the hero", "on screen from the start"),
                                                  ("a truck", "comes into view at about 12.0 s")], first=2)
        self.assertIn("Images 2 to 3", text)
        self.assertIn("image 3: a truck (comes into view at about 12.0 s)", text)
        self.assertEqual(prompts.subjects_clause("keyframe", [], first=3), "")
        style = {"keyframe": "Do it. {materials} {look}{ui}{remove}{extra}", "default_materials": "real things"}
        made = prompts.compose(style, "keyframe", {"keep": [0, 60]}, span=(1, 1),
                               subjects=prompts.subjects_clause("keyframe", [("the hero", "")], first=3))
        self.assertIn("image 3: the hero", made)

    def test_a_sheet_change_renders_again(self):
        old = {"attempt": 1, "keyframe": "k", "subjects": ["a"]}
        self.assertEqual(render.cause_of({"digest": "x", "inputs": old}, {**old, "subjects": ["b"], "prompt": ""}, {}),
                         "subjects")


class LintTests(unittest.TestCase):
    def test_draft_subjects_and_animated_ui(self):
        row = catalog_row(60.0, 30.0)
        facts = {"end_card_start": 60.0, "objects": [], "camera": "2.5d",
                 "hud": [{"what": "the coin counter", "start": 0, "end": 60, "box": [0, .1, .3, .2], "animated": True}],
                 "subjects": [{"id": "Hero", "what": "the protagonist", "reason": "protagonist", "on": [[0, 60]],
                               "at": 5.0, "box": [400, 300, 600, 700]},
                              {"id": "truck", "what": "a truck", "reason": "appears_later", "on": [[40, 60]],
                               "at": 99.0, "box": [0, 0, 1, 1]}]}
        drafted = lint.draft(facts, row)
        self.assertTrue(drafted["ui"][0]["dynamic"])
        self.assertEqual(drafted["subjects"], [{"id": "hero", "what": "the protagonist", "view": "2.5d",
                                                "from": [{"at": 5.0, "box": [0.4, 0.3, 0.6, 0.7]}]}])


class MomentTests(unittest.TestCase):
    def test_every_two_seconds_to_the_last_frame(self):
        self.assertEqual(review.every(7.0, 2.0, 24), [0.0, 2.0, 4.0, 6.0, 6.958])
        self.assertEqual(review.every(6.2, 2.0, 24), [0.0, 2.0, 4.0, 6.0])

    def test_seam_stills(self):
        moments = review.seam_moments(10.0, None, 24, 30.0)
        times = [t for t, _ in moments]
        self.assertEqual(times, [8.0, 9.0, round(10 - 1 / 24, 4), 10.0, 11.0, 12.0])
        later = [t for t, _ in review.seam_moments(10.0, 10.9, 24, 30.0)]
        self.assertIn(10.9, later)
        self.assertIn(12.9, later)                       # 2 s after the later segment's own frames start
        self.assertTrue(all(0 <= t < 30.0 for t, _ in review.seam_moments(29.5, None, 24, 30.0)))


class VerdictTests(unittest.TestCase):
    def test_seams_are_passed_per_delivery(self):
        job = tmp()
        try:
            common.write(job / "state/assembled/S01.json", {"inputs": "d1"})
            common.write(job / "state/review/S01.json", {"rounds": [{"round": 1}]})
            report = {"assembled": "d1", "seam_checks": [{"segment": "K02", "flagged": False, "card": "c2"},
                                                         {"segment": "K03", "flagged": True, "card": "c3"}]}
            common.write(job / "reports/review/S01-r1.json", report)
            self.assertEqual([e["segment"] for e in review.pending_seams(job, "S01")], ["K02", "K03"])
            review.accept_seam(job, "S01", "K02")
            with self.assertRaises(ValueError):             # flagged: only with a reason
                review.accept_seam(job, "S01", "K03")
            review.accept_seam(job, "S01", "K03", "原片这里也闪白")
            self.assertEqual(review.pending_seams(job, "S01"), [])
            common.write(job / "reports/review/S01-r1.json", dict(report, assembled="d2"))
            self.assertEqual(len(review.pending_seams(job, "S01")), 2)    # a new delivery asks again
        finally:
            shutil.rmtree(job, ignore_errors=True)

    def test_a_waiting_chain_goes_on_once_passed(self):
        job = tmp()
        rendered, checked = [], []
        segs = [{"id": "K01", "seam": "start", "seconds": 20}, {"id": "K02", "seam": "continuous", "seconds": 20}]
        the_plan = {"sources": {"S01": {"segments": segs}}}

        def fake_render(job_, state, row, sid, seg, style, previous, prev_seg):
            path = gate.record_path(job, f"{sid}:{seg['id']}")
            if path.is_file():               # finished and unchanged: reused, its check kept (as render_segment does)
                return {**common.read(path), "key": f"{sid}:{seg['id']}", "reused": True}
            rendered.append(seg["id"])
            common.write(path, {"generation_id": "g", "status": "done"})
            return {"key": f"{sid}:{seg['id']}", "generation_id": "g"}

        def fake_check(job_, state, row, sid, index):
            key = f"{sid}:{row['segments'][index]['id']}"
            known = gate.stored(job, key)
            if known:
                return known
            checked.append(key)
            record = common.read(gate.record_path(job, key))
            record["gate"] = {"generation_id": "g", "held": False, "accepted": False}
            common.write(gate.record_path(job, key), record)
            return record["gate"]

        def agent():
            while "S01:K01" not in checked:
                time.sleep(.02)
            gate.accept(job, "S01:K01")
        try:
            helper = threading.Thread(target=agent)
            helper.start()
            with mock.patch("reskin.render.render_segment", fake_render), mock.patch("reskin.gate.check", fake_check), \
                    mock.patch("reskin.render.AGENT_POLL", .05):
                results = render.run_all(job, {}, the_plan, {}, workers=1, wait_checks=True)
            helper.join()
            self.assertEqual(rendered.count("K02"), 1)             # rendered once K01 was passed
            self.assertEqual(checked, ["S01:K01", "S01:K02"])
            self.assertFalse(any(r.get("waiting") for r in results))
            self.assertFalse(common.read(job / "state/run.json")["running"])
        finally:
            shutil.rmtree(job, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
