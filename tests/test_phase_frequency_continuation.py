"""Only actual model work in a continuation may carry stages below frequency."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_review import configure, policy, prepare, seal
from tests import test_phase_review as fixtures


class PhaseFrequencyContinuationTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def waiting_stages(self):
        self.r.config["admin"]["max_input_chars"] = 64000
        configure(self.r, self.p, {"every_phases": 20}, policy(self.r, self.p)["version"])
        for number in range(20):
            self.phase("old-" + str(number), "Independent sourced contract " + str(number))
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("needs_evidence")):
            for _ in range(6):
                if admin_once(self.r, self.p)["status"] == "idle":
                    break
        self.assertEqual(dict(self.r.db.execute("SELECT state,COUNT(*) FROM review_phases GROUP BY state")), {"partial": 20})

    def unrelated_candidate(self):
        source = self.r.feedback(self.p, "astronomy", "Quartz telescope geometry", category="project")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (source["id"],))]
        self.r.submit_candidate(self.p, problem_id=source["id"], problem_revision=source["revision"],
            title="Quartz telescope", content="Nebula calibration", category="project", knowledge_kind="fact",
            applicability="Astronomy", limitations="Reported", validation_level="reported", evidence_refs=refs)

    def pending_sources(self):
        return {item["problem"]["conversation"] for row in self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'")
            for item in json.loads(row[0])["items"]}

    def test_local_comparison_reuse_cannot_reduce_twenty_stage_frequency_to_one(self):
        self.waiting_stages()
        self.unrelated_candidate()
        before = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            self.phase("fresh-0", "New independently sourced interface 0")
            for _ in range(6):
                admin_once(self.r, self.p)
        launch.assert_not_called()
        self.assertFalse(self.pending_sources())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], before)
        for number in range(1, 20):
            self.phase("fresh-" + str(number), "New independently sourced interface " + str(number))
        self.assertTrue(self.pending_sources())

    def test_new_evidence_requiring_real_continuation_can_carry_one_new_stage(self):
        self.waiting_stages()
        source = self.r.db.execute("SELECT * FROM problems WHERE conversation='old-0'").fetchone()
        event, _ = self.r._event(self.p, "old-0", "PostToolUse", {"tool_name": "offline-check", "result": "New verified recovery"},
            outcome="succeeded", task_id=source["task_id"], task_revision=source["task_revision"])
        self.r._attach_case_evidence(source["id"], [(event["id"], "potential_recovery")])
        self.phase("fresh-one", "New independent additional convention")
        sources = self.pending_sources()
        self.assertIn("old-0", sources)
        self.assertIn("fresh-one", sources)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as launch:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(launch.call_count, 1)

    def test_explicit_force_can_review_a_single_new_stage(self):
        configure(self.r, self.p, {"every_phases": 20}, policy(self.r, self.p)["version"])
        self.phase("one", "An independently sourced project convention")
        self.assertFalse(self.pending_sources())
        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        self.assertEqual(self.pending_sources(), {"one"})

    def many_case_stage(self, extra):
        self.r.config["admin"]["max_input_chars"] = 64000
        configure(self.r, self.p, {"every_phases": 20}, policy(self.r, self.p)["version"])
        task = self.r.task_update(self.p, "old", "Preserve independent old contracts", new_task=True)
        self.r._event(self.p, "old", "UserPromptSubmit", {"prompt": task["goal"]}, outcome="reported", task_id=task["id"], task_revision=1)
        for number in range(extra):
            self.r.feedback(self.p, "old", "Independent old constraint " + str(number), category="project")
        self.r.task_update(self.p, "old", task["goal"], status="delivered", expected_task_id=task["id"],
                           expected_write_version=task["write_version"])
        stop, _ = self.r._event(self.p, "old", "Stop", {"last_assistant_message": "Delivered independent old contracts"},
                                outcome="reported", task_id=task["id"], task_revision=1)
        with self.r.transaction():
            seal(self.r, self.p, "old", stop["id"])
            prepare(self.r, self.p, force=True)

    def test_full_continuation_does_not_queue_a_fresh_stage_without_visible_contribution(self):
        self.many_case_stage(15)
        self.phase("fresh", "New independent project convention")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as launch:
            self.assertEqual([admin_once(self.r, self.p)["status"] for _ in range(2)], ["skip", "skip"])
        self.assertEqual(launch.call_count, 2)
        self.assertFalse(self.pending_sources())
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE conversation='fresh'").fetchone()[0], "sealed")
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            admin_once(self.r, self.p)
        launch.assert_not_called()

    def test_final_wrapper_removal_keeps_zero_contribution_fresh_stage_outside_batch(self):
        from tests.test_phase_final_admission import PhaseFinalAdmissionTests
        self.many_case_stage(8)
        self.phase("fresh", "New independent project convention")
        capacity = PhaseFinalAdmissionTests.final_capacity(self, 1)
        def final_capacity_after_response(packet):
            capacity.start()
        try:
            with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip", callback=final_capacity_after_response)):
                self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        finally:
            capacity.stop()
        self.assertEqual(self.pending_sources(), {"old"})
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE conversation='fresh'").fetchone()[0], "sealed")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertFalse(self.pending_sources())
