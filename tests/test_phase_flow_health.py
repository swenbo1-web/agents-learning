"""Event-driven phase wakeups and executor fairness, without real model work."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_queue import (claim_dispatch, clear_executor_failure,
                                       record_executor_failure, release_dispatch)
from agent_learning.phase_review import configure, policy, prepare, status
from tests import test_phase_review as fixtures
from tests import test_phase_scan_progress as scan_fixtures


class PhaseFlowHealthTests(unittest.TestCase):
    setUp = scan_fixtures.PhaseScanProgressTests.setUp
    phase = scan_fixtures.PhaseScanProgressTests.phase
    pending_members = scan_fixtures.PhaseScanProgressTests.pending_members
    ask_for_evidence = scan_fixtures.PhaseScanProgressTests.ask_for_evidence
    recover_member = scan_fixtures.PhaseScanProgressTests.recover_member
    def complete(self):
        members = self.pending_members()
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(status(self.r, self.p)["phases"], {"completed": 2})
        return members

    def pending(self):
        row = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
        return json.loads(row[0]) if row else None

    def test_new_evidence_wakes_completed_phase_and_keeps_old_receipts(self):
        members = self.complete()
        old = {tuple(r) for r in self.r.db.execute("SELECT problem,revision,review_signature FROM review_receipts")}
        source = self.recover_member(sorted(members)[0])
        with self.r.transaction():
            prepare(self.r, self.p)
        packet = self.pending()
        self.assertEqual([i["problem"]["id"] for i in packet["items"]], [source["id"]])
        self.assertEqual(packet["items"][0]["problem"]["revision"], source["revision"])
        self.assertTrue(old <= {tuple(r) for r in self.r.db.execute("SELECT problem,revision,review_signature FROM review_receipts")})
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2)

    def test_replanned_current_revision_settles_dirty_without_an_extra_batch(self):
        members = self.pending_members()
        source = self.recover_member(sorted(members)[0])
        self.assertTrue(self.r.db.execute("SELECT 1 FROM phase_source_dirty WHERE problem_id=?", (source["id"],)).fetchone())
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(status(self.r, self.p)["phases"], {"completed": 2})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_source_dirty").fetchone()[0], 0)
        batches = self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0]
        self.assertEqual(batches, 2, "the old stale batch and its replacement do not require a third empty batch")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], batches)

    def test_replanned_needs_evidence_revision_keeps_dormant_wait_after_dirty_delivery(self):
        members = self.pending_members()
        self.recover_member(sorted(members)[0])
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("needs_evidence")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "needs_evidence")
        self.assertEqual(status(self.r, self.p)["phases"], {"partial": 2})
        batches = self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0]
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], batches)

    def test_new_independent_case_in_original_window_wakes_only_its_stage(self):
        self.complete()
        phase = self.r.db.execute("SELECT * FROM review_phases ORDER BY created LIMIT 1").fetchone()
        task = self.r.task_snapshot(self.p, phase["task_id"], phase["task_revision"], phase["delivery_version"])
        source = self.r._problem(self.p, phase["conversation"], "Independent late project constraint",
                                 "project", [phase["event_id"]], task_binding=task)
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertEqual(self.pending()["phase_ids"], [phase["id"]])
        self.assertIn(source["id"], {i["problem"]["id"] for i in self.pending()["items"]})
        self.assertEqual(status(self.r, self.p)["phases"].get("completed"), 1)

    def test_independent_case_added_while_model_runs_is_reviewed_after_frozen_batch(self):
        self.pending_members()
        phase = self.r.db.execute("SELECT * FROM review_phases ORDER BY created LIMIT 1").fetchone()
        task = self.r.task_snapshot(self.p, phase["task_id"], phase["task_revision"], phase["delivery_version"])
        added = []
        def add_case(_packet):
            source = self.r._problem(self.p, phase["conversation"], "Independent constraint discovered during review",
                                     "project", [phase["event_id"]], task_binding=task)
            added.append(source["id"])
            with self.r.transaction():
                prepare(self.r, self.p)
            self.assertTrue(self.r.db.execute("SELECT 1 FROM phase_source_dirty WHERE phase_id=?", (phase["id"],)).fetchone(),
                            "queued sources retain their notification until the frozen batch settles")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip", callback=add_case)):
            first = admin_once(self.r, self.p)
        self.assertEqual(first["status"], "skip", first)
        packet = self.pending()
        self.assertEqual([i["problem"]["id"] for i in packet["items"]], added,
                         "only the new case requires another review; old receipts stay reusable")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(status(self.r, self.p)["phases"], {"completed": 2})

    def test_new_case_outside_original_window_does_not_reopen_completed_stage(self):
        self.complete()
        phase = self.r.db.execute("SELECT * FROM review_phases LIMIT 1").fetchone()
        task = self.r.task(self.p, phase["conversation"])
        later, _ = self.r._event(self.p, phase["conversation"], "Stop", {"last_assistant_message": "Later work"},
            task_id=task["id"], task_revision=task["revision"], outcome="reported")
        self.r._problem(self.p, phase["conversation"], "Independent later work", "project", [later["id"]], task_binding=task)
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertIsNone(self.pending())
        self.assertEqual(status(self.r, self.p)["phases"], {"completed": 2})

    def test_delivery_projection_is_not_a_new_independent_stage_case(self):
        self.complete()
        phase = self.r.db.execute("SELECT * FROM review_phases LIMIT 1").fetchone()
        task = self.r.task(self.p, phase["conversation"])
        source = self.r._problem(self.p, phase["conversation"], "Delivery projection", "mixed",
                                 [phase["event_id"]], task_binding=task)
        self.r.db.execute("INSERT INTO deliveries(project,task_id,revision,problem_id) VALUES(?,?,?,?)",
                          (self.p, task["id"], task["revision"], source["id"]))
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertIsNone(self.pending())
        self.assertEqual(status(self.r, self.p)["phases"], {"completed": 2})

    def test_source_dirty_never_reopens_an_old_account_phase(self):
        members = self.complete()
        self.recover_member(sorted(members)[0])
        self.r.scope_account_changed()
        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        self.assertIsNone(self.pending())
        self.assertEqual(status(self.r, self.p)["historical_boundary_phases"], 2)

    def test_other_host_cannot_consume_or_reopen_the_dirty_source(self):
        from agent_learning.runtime import Runtime
        members = self.complete()
        self.recover_member(sorted(members)[0])
        dirty = self.r.db.execute("SELECT COUNT(*) FROM phase_source_dirty").fetchone()[0]
        self.assertGreater(dirty, 0)
        other = Runtime(self.r.home, host="dsh", config=self.r.config)
        self.addCleanup(other.close)
        configure(other, self.p, {"mode": "phase", "every_phases": 1}, 0)
        with other.transaction():
            prepare(other, self.p, force=True)
        self.assertEqual(other.db.execute("SELECT COUNT(*) FROM phase_source_dirty").fetchone()[0], dirty)
        self.assertEqual(status(other, self.p)["phases"], {})
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertIsNotNone(self.pending())

    def test_scheduler_prepares_new_evidence_without_a_new_stage_or_model_call(self):
        members = self.ask_for_evidence()
        self.recover_member(sorted(members)[0])
        self.r.config["scheduler"]["enabled"] = True
        self.assertIsNone(self.pending())
        with patch("agent_learning.admin.subprocess.Popen") as model:
            self.assertTrue(self.r.scheduler_wake(self.p))
        model.assert_not_called()
        self.assertIsNotNone(self.pending())
        job = self.r.db.execute("SELECT state FROM scheduler_jobs WHERE kind='admin'").fetchone()
        self.assertEqual(job[0], "ready")
        before = self.r.db.total_changes
        status(self.r, self.p)
        self.r.scheduler_status(self.p)
        self.assertEqual(self.r.db.total_changes, before, "status views cannot prepare or dispatch")

    def models(self):
        configure(self.r, self.p, {"every_phases": 1}, policy(self.r, self.p)["version"])
        self.r.admin_configure(self.p, True, "host", "current", "", expected_policy_version=0)
        self.r.admin_observe_model(self.p, "one", "model-a")
        self.r.admin_observe_model(self.p, "two", "model-b")
        return self.r.resolve_admin_executor(self.p, "one")

    def test_unavailable_first_executor_cannot_starve_a_ready_group(self):
        plan = self.models()
        with patch("agent_learning.phase_review.prepare"):
            self.phase("one", "Independent model A constraint")
            self.phase("two", "Independent model B convention")
        record_executor_failure(self.r, self.p, plan, "configuration_change_required")
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertEqual(self.pending()["executor_conversation"], "two")
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(token)
        release_dispatch(self.r, self.p, token)
        self.assertEqual(self.r.db.execute("SELECT state FROM review_phases WHERE conversation='one'").fetchone()[0], "sealed")

    def test_pending_executor_backoff_sleeps_without_blocking_or_changing_models(self):
        plan = self.models()
        self.phase("one", "Independent model A constraint")
        record_executor_failure(self.r, self.p, plan, "configuration_change_required")
        self.phase("two", "Independent model B convention")
        packets = [(row[0], json.loads(row[1])) for row in self.r.db.execute("SELECT state,packet FROM phase_review_batches")]
        self.assertEqual({p["executor_conversation"]: state for state, p in packets},
                         {"one": "executor_waiting", "two": "pending"})
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()
        clear_executor_failure(self.r, self.p, plan)
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertEqual(self.pending()["executor_conversation"], "one")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2)

    def test_unknown_first_group_remains_locked_while_other_model_can_review(self):
        import subprocess
        self.models()
        self.phase("one", "Independent model A constraint")
        class Timeout(fixtures.reviewer()):
            def communicate(inner, *args, **kwargs):
                raise subprocess.TimeoutExpired("controlled", 5)
        with patch("agent_learning.admin.subprocess.Popen", Timeout):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "timeout_unknown", result)
        original = self.r.db.execute("SELECT id,state,retry_allowed FROM admin_calls").fetchone()
        self.phase("two", "Independent model B convention")
        self.assertEqual(self.pending()["executor_conversation"], "two")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        current = self.r.db.execute("SELECT id,state,retry_allowed FROM admin_calls WHERE id=?", (original["id"],)).fetchone()
        self.assertEqual(tuple(current), tuple(original))
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()

    def test_policy_change_cancels_unpaid_executor_waiting_batch(self):
        plan = self.models()
        self.phase("one", "Independent model A constraint")
        record_executor_failure(self.r, self.p, plan, "configuration_change_required")
        with self.r.transaction():
            prepare(self.r, self.p)
        old = self.r.db.execute("SELECT problem_id FROM phase_review_batches WHERE state='executor_waiting'").fetchone()[0]
        configure(self.r, self.p, {"max_input_tokens": 41000}, policy(self.r, self.p)["version"])
        self.assertEqual(self.r.db.execute("SELECT state FROM phase_review_batches WHERE problem_id=?", (old,)).fetchone()[0],
                         "cancelled")
        clear_executor_failure(self.r, self.p, plan)
        with self.r.transaction():
            prepare(self.r, self.p)
        self.assertIsNotNone(self.pending())
        self.assertNotEqual(self.pending()["problem"]["id"], old)

    def test_upgrade_discovers_old_unreviewed_revision_without_changing_authority_or_spending(self):
        from agent_learning.runtime import Runtime
        members = self.complete()
        source = self.recover_member(sorted(members)[0])
        self.r.db.execute("DROP TRIGGER phase_completed_source_change")
        self.r.db.execute("DROP TABLE phase_source_dirty")
        self.r.db.execute("DROP TABLE phase_source_projection")
        self.r.db.execute("PRAGMA user_version=24")
        tables = ("problems", "problem_events", "events", "review_phases", "review_receipts", "admin_calls", "admin_call_budgets")
        before = {name: [tuple(row) for row in self.r.db.execute("SELECT * FROM " + name + " ORDER BY rowid")] for name in tables}
        with patch("subprocess.Popen", side_effect=AssertionError("Upgrade cannot run a model")):
            upgraded = Runtime(self.r.home, config=self.r.config)
        self.addCleanup(upgraded.close)
        self.assertEqual(before, {name: [tuple(row) for row in upgraded.db.execute("SELECT * FROM " + name + " ORDER BY rowid")]
                                  for name in tables})
        dirty = upgraded.db.execute("SELECT problem_id,revision FROM phase_source_dirty").fetchall()
        self.assertEqual([tuple(row) for row in dirty], [(source["id"], source["revision"])])
        with upgraded.transaction():
            prepare(upgraded, self.p)
        self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM phase_source_dirty").fetchone()[0], 1,
                         "migration discovery cannot borrow authorization across the protocol fence")
        upgraded.db.execute("DELETE FROM phase_source_dirty")
        upgraded.db.execute("DELETE FROM runtime_storage_receipt")
        with patch("subprocess.Popen", side_effect=AssertionError("Reopen cannot run a model")):
            reopened = Runtime(self.r.home, config=self.r.config)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM phase_source_dirty").fetchone()[0], 0,
                         "the projection receipt prevents repeated migration seeding")

    def test_status_counts_single_candidate_results(self):
        self.pending_members()
        base = fixtures.reviewer("candidate")
        class SingleCandidate(base):
            def communicate(inner, data=None, timeout=None):
                result = super().communicate(data, timeout)
                response = json.loads(inner.output.read_text(encoding="utf-8"))
                for item in response["items"]:
                    item["candidate"] = item["candidates"][0]
                    item["candidates"] = []
                inner.output.write_text(json.dumps(response), encoding="utf-8")
                return result
        with patch("agent_learning.admin.subprocess.Popen", SingleCandidate):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "candidate", result)
        view = status(self.r, self.p)["recent_batches"][0]
        self.assertEqual(sum(len(i["candidate_ids"]) for i in view["items"]), view["metrics"]["candidate_count"])


if __name__ == "__main__":
    import unittest
    unittest.main()
