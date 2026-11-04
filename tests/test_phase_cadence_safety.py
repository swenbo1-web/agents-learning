"""Cadence tradeoffs use isolated SQLite and a deterministic reviewer, never a model.

These tests measure invocation/coverage contracts, not extraction quality or token
savings. In particular a plan can contain a durable new project constraint, so
its name or wording must not suppress the evidence before review.
"""
import subprocess
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_review import configure, policy, prepare, seal
from tests import test_phase_review as fixtures


class PhaseCadenceSafetyTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp

    def frequency(self, value):
        configure(self.r, self.p, {"every_phases": value}, policy(self.r, self.p)["version"])

    def stage(self, goal, reply, *, conversation="continuous-chat"):
        previous = self.r.task(self.p, conversation)
        expected = ({"expected_task_id": previous["id"],
                     "expected_write_version": previous["write_version"]} if previous else {})
        task = self.r.task_update(self.p, conversation, goal, new_task=True, **expected)
        self.r._event(self.p, conversation, "UserPromptSubmit", {"prompt": goal}, outcome="reported",
                      task_id=task["id"], task_revision=task["revision"])
        self.r.task_update(self.p, conversation, goal, status="delivered", expected_task_id=task["id"],
                           expected_write_version=task["write_version"])
        event, _ = self.r._event(self.p, conversation, "Stop", {"last_assistant_message": reply},
                                outcome="reported", task_id=task["id"], task_revision=task["revision"])
        with self.r.transaction():
            seal(self.r, self.p, conversation, event["id"])
        phase = self.r.db.execute("SELECT * FROM review_phases WHERE task_id=?", (task["id"],)).fetchone()
        self.assertIsNotNone(phase)
        return phase

    def child_receipts(self):
        return self.r.db.execute("SELECT r.* FROM review_receipts r JOIN phase_review_members m "
                                 "ON m.project=r.project AND m.problem_id=r.problem AND m.revision=r.revision "
                                 "WHERE r.project=? AND r.duplicate_of IS NULL", (self.p,)).fetchall()

    def test_six_short_stages_at_frequency_one_keep_six_distinct_receipts(self):
        self.assert_cadence(1, [1, 1, 1, 1, 1, 1])

    def test_same_six_stages_at_frequency_two_keep_coverage_with_three_calls(self):
        self.assert_cadence(2, [2, 2, 2])

    def test_same_six_stages_at_frequency_three_keep_coverage_with_two_calls(self):
        self.assert_cadence(3, [3, 3])

    def assert_cadence(self, frequency, expected_sizes):
        self.frequency(frequency)
        # Each is a distinct delivered task in the same chat. Plans and negative
        # findings remain material for the reviewer, rather than local verdicts.
        stages = (
            ("Record export format", "Reported fact: exports use UTF-8 with no BOM."),
            ("Prepare upcoming experiment", "Plan: freeze six scenes before running the experiment."),
            ("Plan with a new project constraint", "Plan: never upload private fixtures; this is now a project requirement."),
            ("Record counterexample evidence", "Counterexample report: old path matching fails for case-sensitive checkout names."),
            ("Record recovery limitation", "Reported limitation: recovery passes locally but remains unverified on network volumes."),
            ("Hand off the next phase", "Handoff plan: independent validation must precede installation."),
        )
        observed = []
        def collect(packet):
            observed.append([(item["problem"]["task_id"],
                              "".join(event["payload"].get("last_assistant_message", event["payload"].get("text", ""))
                                      for event in item["events"]))
                             for item in packet["items"]])
        phase_ids = []
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip", callback=collect)) as model:
            for goal, reply in stages:
                phase_ids.append(self.stage(goal, reply)["id"])
                admin_once(self.r, self.p)
        self.assertEqual(model.call_count, len(expected_sizes))
        self.assertEqual([len(items) for items in observed], expected_sizes)
        self.assertEqual(len({task for items in observed for task, _ in items}), len(stages))
        self.assertEqual({reply for items in observed for _, reply in items}, {reply for _, reply in stages})
        self.assertEqual(len(self.child_receipts()), len(stages))
        states = self.r.db.execute("SELECT id,state FROM review_phases").fetchall()
        self.assertEqual({row["id"] for row in states}, set(phase_ids))
        self.assertTrue(all(row["state"] == "completed" for row in states))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], len(expected_sizes))

    def test_frequency_alone_does_not_flush_a_tail_but_explicit_force_can(self):
        self.frequency(3)
        phase = self.stage("Freeze a new plan constraint", "Future runs must preserve the independent source receipts.")
        # No wall-clock sleep: count-only policy remains count-only after a day.
        future = time.time() + 86400
        with patch("agent_learning.phase_review.time.time", return_value=future), \
                patch("agent_learning.admin.subprocess.Popen") as model:
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], "sealed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches WHERE state='pending'").fetchone()[0], 0)
        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as model:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(self.child_receipts()), 1)

    def test_same_subject_new_conclusion_is_reviewed_and_exact_repeat_is_reused(self):
        self.frequency(1)
        goal = "Record export precision rule"
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as model:
            self.stage(goal, "Export precision is eight decimal places.")
            admin_once(self.r, self.p)
            self.stage(goal, "Export precision changed to two decimal places; eight is obsolete.")
            admin_once(self.r, self.p)
        self.assertEqual(model.call_count, 2, "same subject does not make changed business evidence equivalent")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            self.stage(goal, "Export precision changed to two decimal places; eight is obsolete.")
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE duplicate_of IS NOT NULL").fetchone()[0], 1)

    def test_different_executor_models_cannot_satisfy_each_others_frequency(self):
        self.frequency(2)
        self.r.admin_configure(self.p, True, "host", "current", "", expected_policy_version=0)
        for chat, model in (("a1", "model-a"), ("b1", "model-b"), ("a2", "model-a"), ("b2", "model-b")):
            self.r.admin_observe_model(self.p, chat, model)
        packets = []
        def collect(packet):
            packets.append({item["problem"]["conversation"] for item in packet["items"]})
        self.stage("A first source", "Independent A1 fact", conversation="a1")
        self.stage("B first source", "Independent B1 fact", conversation="b1")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip", callback=collect)) as model:
            self.stage("A second source", "Independent A2 fact", conversation="a2")
            admin_once(self.r, self.p)
            self.stage("B second source", "Independent B2 fact", conversation="b2")
            admin_once(self.r, self.p)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(packets, [{"a1", "a2"}, {"b1", "b2"}])
        self.assertEqual(len(self.child_receipts()), 4)

    def test_unknown_result_stays_locked_after_cadence_and_budget_changes(self):
        self.frequency(1)
        self.stage("Record a scoped project fact", "This fact is complete but its reviewer result will be unknown.")
        class Timeout(fixtures.reviewer()):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired("controlled-offline-review", 5)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=Timeout) as model:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "timeout_unknown", result)
        self.assertEqual(model.call_count, 1)
        before = dict(self.r.db.execute("SELECT * FROM admin_calls").fetchone())
        configure(self.r, self.p, {"every_phases": 2, "daily_tokens": 2000000}, policy(self.r, self.p)["version"])
        with patch("agent_learning.admin.subprocess.Popen") as model:
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        after = dict(self.r.db.execute("SELECT * FROM admin_calls").fetchone())
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(after["state"], "timeout_unknown")
        self.assertEqual(after["retry_allowed"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertGreater(self.r.db.execute("SELECT reserved_tokens FROM admin_call_budgets").fetchone()[0], 0)

    def test_due_tail_cannot_borrow_the_recent_other_executors_stage(self):
        configure(self.r, self.p, {"every_phases": 3, "max_wait_seconds": 60}, policy(self.r, self.p)["version"])
        self.r.admin_configure(self.p, True, "host", "current", "", expected_policy_version=0)
        self.r.admin_observe_model(self.p, "old-a", "model-a")
        self.r.admin_observe_model(self.p, "recent-b", "model-b")
        self.stage("Old independent A convention", "Old A fact", conversation="old-a")
        future = time.time() + 120
        seen = []
        with patch("agent_learning.phase_review.time.time", return_value=future):
            recent = self.stage("Recent independent B convention", "Recent B fact", conversation="recent-b")
            with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip", callback=lambda p: seen.append(p))):
                self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
            with patch("agent_learning.admin.subprocess.Popen") as model:
                admin_once(self.r, self.p)
            model.assert_not_called()
        self.assertEqual(len(seen), 1)
        self.assertEqual({item["problem"]["conversation"] for item in seen[0]["items"]}, {"old-a"})
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE id=?", (recent["id"],)).fetchone()[0], "sealed")

    def test_routine_only_due_tail_still_does_not_launch(self):
        configure(self.r, self.p, {"every_phases": 3, "max_wait_seconds": 60}, policy(self.r, self.p)["version"])
        phase = self.stage("检查Agent Learning连接", "Agent Learning连接正常。管理员未开启。")
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], "routine_deferred")
        future = time.time() + 120
        with patch("agent_learning.phase_review.time.time", return_value=future), \
                patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_due_new_tail_can_run_without_unlocking_an_older_unknown_result(self):
        self.frequency(1)
        self.stage("Old independent delivery", "Old evidence with unknown reviewer outcome.")
        class Timeout(fixtures.reviewer()):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired("controlled-offline-review", 5)
        with patch("agent_learning.admin.subprocess.Popen", Timeout):
            self.assertEqual(admin_once(self.r, self.p)["status"], "timeout_unknown")
        old = dict(self.r.db.execute("SELECT * FROM admin_calls").fetchone())
        configure(self.r, self.p, {"every_phases": 3, "max_wait_seconds": 60}, policy(self.r, self.p)["version"])
        new = self.stage("New independent delivery", "New independent public fact with its own source.")
        future = time.time() + 120
        seen = []
        with patch("agent_learning.phase_review.time.time", return_value=future), \
                patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip", callback=lambda p: seen.append(p))) as model:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(model.call_count, 1)
        self.assertEqual([item["problem"]["task_id"] for item in seen[0]["items"]], [new["task_id"]])
        protected = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (old["id"],)).fetchone()
        self.assertEqual(tuple(protected), ("timeout_unknown", 0))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2)

    def test_due_tail_remains_visible_beside_a_real_evidence_continuation(self):
        self.frequency(1)
        old = self.stage("An evidence-dependent old constraint", "The old constraint needs independent verification.")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("needs_evidence")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "needs_evidence")
        old_receipt = self.child_receipts()[0]["review_signature"]
        source = self.r.db.execute("SELECT * FROM problems WHERE task_id=?", (old["task_id"],)).fetchone()
        configure(self.r, self.p, {"every_phases": 3, "max_wait_seconds": 60}, policy(self.r, self.p)["version"])
        with patch("agent_learning.phase_review.prepare"):
            event, _ = self.r._event(self.p, old["conversation"], "PostToolUse",
                                    {"tool_name": "offline-check", "result": "New complete evidence for this old constraint."},
                                    outcome="succeeded", task_id=source["task_id"], task_revision=source["task_revision"])
            self.r._attach_case_evidence(source["id"], [(event["id"], "potential_recovery")])
            fresh = self.stage("An independent tail fact", "The tail contains a new independently sourced convention.")
        future = time.time() + 120
        seen = []
        with patch("agent_learning.phase_review.time.time", return_value=future), \
                patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip", callback=lambda p: seen.append(p))) as model:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(model.call_count, 1)
        self.assertEqual({item["problem"]["task_id"] for item in seen[0]["items"]}, {old["task_id"], fresh["task_id"]})
        self.assertTrue(self.r.db.execute("SELECT 1 FROM review_receipts WHERE review_signature=?", (old_receipt,)).fetchone())

    def test_one_new_source_reopens_only_its_receipt_without_repeating_siblings(self):
        self.frequency(2)
        first = self.stage("First independently scoped rule", "First rule based on the initial source.")
        second = self.stage("Second independently scoped rule", "Second rule based on an independent source.")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        old = {(row["problem"], row["revision"], row["review_signature"]) for row in self.child_receipts()}
        source = self.r.db.execute("SELECT p.* FROM problems p JOIN phase_review_members m ON m.problem_id=p.id "
                                   "JOIN phase_review_batches b ON b.problem_id=m.batch_id "
                                   "WHERE p.task_id=?", (first["task_id"],)).fetchone()
        event, _ = self.r._event(self.p, first["conversation"], "PostToolUse",
                                {"tool_name": "offline-check", "result": "The first rule has a new conflicting observation."},
                                outcome="failed", task_id=source["task_id"], task_revision=source["task_revision"])
        self.r._attach_case_evidence(source["id"], [(event["id"], "observation")])
        seen = []
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip", callback=lambda p: seen.append(p))):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(len(seen), 1)
        self.assertEqual([item["problem"]["id"] for item in seen[0]["items"]], [source["id"]])
        self.assertEqual(seen[0]["items"][0]["problem"]["revision"], source["revision"] + 1)
        now = {(row["problem"], row["revision"], row["review_signature"]) for row in self.child_receipts()}
        self.assertTrue(old <= now)
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE id=?", (second["id"],)).fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
