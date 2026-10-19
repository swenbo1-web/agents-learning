"""Explicit historical reply recovery, with fake provider output and local SQLite."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import ResponseRejected
from agent_learning.admin_response_store import status
from agent_learning.admin_review_flow import review_activity
from agent_learning.candidate_review import (
    candidate_review_detail, commit_review, history_result_guard, prepare_review,
)
from agent_learning.runtime import ContractError, Runtime, digest, encode
from tests import test_history_candidates as fixtures


class HistoryResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "r76-history"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="test-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text(
            "RenderingPipeline ShaderCache project architecture.", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)

    def failed(self, *, transform=fixtures.multiple, auto_reference=False):
        class Reviewer(fixtures.history_reviewer(transform)):
            def __init__(child, args, **kwargs):
                super().__init__(args, **kwargs)
                child.log = kwargs["stdout"]

            def communicate(child, *args, **kwargs):
                result = super().communicate(*args, **kwargs)
                child.log.write((encode({"type": "turn.completed", "usage": {
                    "input_tokens": 600, "output_tokens": 120}}) + "\n").encode())
                return result

        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        with patch("agent_learning.admin.subprocess.Popen", Reviewer), patch(
                "agent_learning.admin._accept_response", side_effect=ResponseRejected("candidate_batch_ambiguous")):
            self.r.history_run(self.p, job, allow_review=True, max_calls=1, auto_reference=auto_reference)
        call = self.r.db.execute("SELECT id FROM admin_calls ORDER BY created DESC,id LIMIT 1").fetchone()[0]
        self.assertTrue(status(self.r, self.p, call)["available"])
        return job, call

    def recover(self, call, **changes):
        from agent_learning.history_response_recovery import recheck
        saved = status(self.r, self.p, call)
        history = saved["history_recovery"]
        body = {"request_id": "history-local-recheck", "expected_version": saved["version"],
                "expected_job_version": history["job_version"],
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch(),
                "acknowledge_boundary": True, "reason": "Explicitly inspect the saved frozen batch reply",
                "allow_publication": False, "confirmed": True}
        body.update(changes)
        return recheck(self.r, self.p, call, **body)

    def candidate_ids(self):
        return [row[0] for row in self.r.db.execute("SELECT id FROM candidates ORDER BY id")]

    def assert_rejected(self, call, **changes):
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            try:
                answer = self.recover(call, **changes)
            except ContractError:
                answer = None
        launch.assert_not_called()
        if answer is not None:
            self.assertEqual(answer["status"], "rejected", answer)
        self.assertFalse(self.candidate_ids())
        return answer

    def test_recovery_has_no_model_call_or_ledger_job_permission_changes(self):
        from agent_learning.admin_usage import admin_project_budget_status
        job, call = self.failed()
        paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
        batch = dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())
        observations = [tuple(row) for row in self.r.db.execute("SELECT * FROM admin_usage_observations")]
        budget = admin_project_budget_status(self.r, self.p)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(call)
        launch.assert_not_called()
        self.assertEqual(result["status"], "accepted", result)
        self.assertFalse(result["models_started"])
        self.assertEqual(len(self.candidate_ids()), 3)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone()), paid)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone()), batch)
        self.assertEqual([tuple(row) for row in self.r.db.execute("SELECT * FROM admin_usage_observations")], observations)
        after = admin_project_budget_status(self.r, self.p)
        for key in ("counted_attempts", "started_attempts", "reported_tokens", "token_reservations", "accounted_tokens"):
            self.assertEqual(budget[key], after[key], key)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_recovered_artifacts_can_be_inspected_and_explicitly_approved_without_general_case_access(self):
        _, call = self.failed()
        result = self.recover(call)
        self.assertEqual(result["status"], "accepted", result)
        identities = self.candidate_ids()
        candidate = self.r.db.execute("SELECT problem FROM candidates WHERE id=?", (identities[0],)).fetchone()
        self.assertEqual(candidate_review_detail(self.r, self.p, identities[0])["preflight"]["issues"], [])
        with self.assertRaises(ContractError):
            history_result_guard(self.r, self.p, candidate[0])
        with self.assertRaises(ContractError):
            self.r.review_packet(self.p, candidate[0])
        with self.assertRaises(ContractError):
            self.r.history_guard(self.p, candidate[0], require_review=True)
        plan = prepare_review(self.r, self.p, identities[:2], "approve", "Review exact saved reply")
        self.assertTrue(plan["ready"], plan)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            approved = commit_review(self.r, self.p, plan, "approve-recovered")
        launch.assert_not_called()
        self.assertEqual(len(approved["results"]), 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates WHERE status='candidate'").fetchone()[0], 1)

    def test_recent_activity_displays_only_exact_authorized_call(self):
        _, call = self.failed()
        self.assertEqual(self.recover(call)["status"], "accepted")
        recent = review_activity(self.r, self.p)["recent"][0]
        self.assertEqual(recent["call_id"], call)
        self.assertTrue(recent["local_recheck"])
        self.assertEqual(len(recent["candidates"]), 3)
        self.assertFalse(recent.get("history_access_required", False))
        self.r.history_pause(self.p, recent["history_job_id"])
        recent = review_activity(self.r, self.p)["recent"][0]
        self.assertTrue(recent["history_access_required"])
        self.assertEqual(recent["candidates"], [])

    def test_recovered_candidate_read_only_browser_inspection_has_no_runtime_or_write_dependency(self):
        from agent_learning.knowledge_browser import _ReadModel
        _, call = self.failed()
        self.assertEqual(self.recover(call)["status"], "accepted")
        uri = (self.r.home / "runtime.sqlite3").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            before = db.total_changes
            detail = candidate_review_detail(_ReadModel(db), self.p, self.candidate_ids()[0])
            self.assertTrue(detail["preflight"]["publishable"], detail)
            self.assertEqual(db.total_changes, before)

    def test_approving_one_recovered_candidate_does_not_revoke_other_saved_artifacts(self):
        _, call = self.failed()
        self.assertEqual(self.recover(call)["status"], "accepted")
        identities = self.candidate_ids()
        first = prepare_review(self.r, self.p, identities[:1], "approve")
        commit_review(self.r, self.p, first, "first-artifact")
        second = prepare_review(self.r, self.p, identities[1:], "approve")
        self.assertTrue(second["ready"], second)
        commit_review(self.r, self.p, second, "remaining-artifacts")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 3)

    def test_reference_access_still_requires_independent_batch_opt_in(self):
        from agent_learning.candidate_references import assess, history_grant
        job, call = self.failed()
        self.assertEqual(self.recover(call)["status"], "accepted")
        self.assertFalse(history_grant(self.r, self.p, job)["enabled"])
        for identity in self.candidate_ids():
            item = assess(self.r, self.p, identity, _respect_policy=False)
            self.assertFalse(item["eligible"])
            self.assertEqual(item["reason"], "history_reference_not_authorized")

    def test_original_history_auto_reference_authority_is_not_restored_by_default(self):
        self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
            "VALUES(?,?,0,1,?,1,'tiered')", (self.p, self.r._session_scope(), time.time()))
        job, call = self.failed(auto_reference=True)
        self.assertTrue(json.loads(self.r.db.execute("SELECT last_result FROM history_jobs WHERE id=?", (job,)).fetchone()[0])[
            "auto_reference_authorized"])
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            answer = self.recover(call)
        launch.assert_not_called()
        self.assertEqual(answer["status"], "accepted", answer)
        self.assertEqual(len(self.candidate_ids()), 3)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        row = self.r.db.execute("SELECT state,review_enabled,owner FROM history_jobs WHERE id=?", (job,)).fetchone()
        self.assertEqual(tuple(row), ("paused", 0, None))

    def test_explicit_publication_uses_original_batch_permission_and_current_tiered_policy(self):
        self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
            "VALUES(?,?,0,1,?,1,'tiered')", (self.p, self.r._session_scope(), time.time()))
        _, call = self.failed(auto_reference=True)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            answer = self.recover(call, allow_publication=True)
        launch.assert_not_called()
        self.assertEqual(answer["status"], "accepted", answer)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 3)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates WHERE status='published'").fetchone()[0], 3)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_explicit_publication_cannot_invent_original_batch_permission(self):
        self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
            "VALUES(?,?,0,1,?,1,'tiered')", (self.p, self.r._session_scope(), time.time()))
        _, call = self.failed(auto_reference=False)
        self.assert_rejected(call, allow_publication=True)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_repeat_request_replays_exact_receipt_without_second_artifacts(self):
        _, call = self.failed()
        saved = status(self.r, self.p, call)
        versions = {"expected_version": saved["version"],
                    "expected_job_version": saved["history_recovery"]["job_version"]}
        first = self.recover(call, **versions)
        count = self.r.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            second = self.recover(call, **versions)
        launch.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(self.r.db.total_changes, count)
        with self.assertRaises(ContractError):
            self.recover(call, reason="Different authorization", **versions)

    def test_explicit_pause_cancellation_and_account_fences_require_boundary_acknowledgement(self):
        for boundary in ("pause", "cancel", "account"):
            with self.subTest(boundary=boundary):
                job, call = self.failed()
                if boundary == "pause":
                    self.r.history_pause(self.p, job)
                elif boundary == "cancel":
                    self.r.history_cancel(self.p, job)
                else:
                    self.r.account_changed()
                state = status(self.r, self.p, call)["history_recovery"]
                self.assertTrue(state["ack_required"], state)
                self.assert_rejected(call, acknowledge_boundary=False)
                accepted = self.recover(call, request_id="recover-" + boundary)
                self.assertEqual(accepted["status"], "accepted", accepted)
                self.assertEqual(len(self.candidate_ids()), 3)
                self.r.close()
                next_home = self.root / (boundary + "-next-home")
                self.r = Runtime(next_home, config={})
                self.addCleanup(self.r.close)
                self.p = self.r.project(self.workspace)
                self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)

    def test_model_or_admin_policy_changes_do_not_reexecute_frozen_plan(self):
        _, call = self.failed()
        self.r.admin_configure(self.p, False, "codex", "fixed", "other-model", expected_policy_version=0)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            answer = self.recover(call)
        launch.assert_not_called()
        self.assertEqual(answer["status"], "accepted", answer)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_changed_sources_or_comparison_do_not_become_authorized_new_knowledge(self):
        _, call = self.failed()
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=(SELECT problem FROM admin_calls WHERE id=?)", (call,))
        self.assert_rejected(call)

    def test_changed_comparison_context_requires_analysis_instead_of_confirming_away_change(self):
        _, call = self.failed()
        problem = self.r.db.execute("SELECT problem FROM admin_calls WHERE id=?", (call,)).fetchone()[0]
        self.r.db.execute("INSERT INTO admin_case_epochs VALUES(?,?,1) ON CONFLICT(project,problem) "
                          "DO UPDATE SET revision=revision+1", (self.p, problem))
        self.assert_rejected(call)

    def test_comparison_knowledge_source_conflict_rejects_reply_with_unchanged_body_and_revision(self):
        from tests import test_candidate_references as candidate_fixtures
        task = self.r.task_update(self.p, "comparison-source", "RenderingPipeline ShaderCache architecture", new_task=True)
        self.r.db.execute("INSERT INTO operations(project,id,conversation,task_id,task_revision,agent,state,last_event,conflict,decision) "
            "VALUES(?,?,?,?,?,NULL,'completed','fixture-operation-start',0,'allowed')", (
                self.p, "comparison-operation", "comparison-source", task["id"], task["revision"]))
        identity, _, _, event, _ = candidate_fixtures.CandidateReferenceTests.candidate(
            self, title="RenderingPipeline ShaderCache project architecture",
            content="RenderingPipeline ShaderCache project architecture records the current rendering convention.",
            conversation="comparison-source", operation_id="comparison-operation")
        self.r.db.execute("UPDATE operations SET last_event=? WHERE project=? AND id=?",
                          (event["id"], self.p, "comparison-operation"))
        knowledge = self.r.publish(self.p, identity)
        # Fix the comparison selection; this regression verifies acceptance
        # freshness, independently of lexical ranking of the import summary.
        with patch.object(self.r, "_review_related", return_value=([
                {"knowledge_id": knowledge["knowledge_id"], "revision": knowledge["revision"],
                 "title": "RenderingPipeline ShaderCache", "content": "Existing rendering convention"}], [])):
            _, call = self.failed()
        reserved = json.loads(self.r.db.execute("SELECT reserved FROM admin_response_records WHERE call_id=?", (call,)).fetchone()[0])
        self.assertTrue(any(item.get("knowledge_id", item.get("id")) == knowledge["knowledge_id"]
                            for item in reserved["packet"]["related_knowledge"]))
        current = tuple(self.r.db.execute("SELECT k.body,a.revision FROM knowledge k JOIN active a "
            "ON a.project=k.project AND a.id=k.id AND a.revision=k.revision WHERE k.project=? AND k.id=?",
            (self.p, knowledge["knowledge_id"])).fetchone())
        self.r.db.execute("UPDATE operations SET state='conflict',conflict=1,decision='denied' WHERE project=? AND id=?",
                          (self.p, "comparison-operation"))
        count = len(self.candidate_ids())
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            try:
                answer = self.recover(call)
            except ContractError:
                answer = None
        launch.assert_not_called()
        if answer is not None:
            self.assertEqual(answer["status"], "rejected", answer)
        self.assertEqual(len(self.candidate_ids()), count)
        unchanged = tuple(self.r.db.execute("SELECT k.body,a.revision FROM knowledge k JOIN active a "
            "ON a.project=k.project AND a.id=k.id AND a.revision=k.revision WHERE k.project=? AND k.id=?",
            (self.p, knowledge["knowledge_id"])).fetchone())
        self.assertEqual(unchanged, current)

    def test_changed_related_frozen_source_rejects_primary_recovery(self):
        (self.workspace / "architecture.md").write_text(
            "RenderingPipeline ShaderCache project architecture uses a local memory cache.", encoding="utf-8")
        _, call = self.failed()
        reserved = json.loads(self.r.db.execute("SELECT reserved FROM admin_response_records WHERE call_id=?", (call,)).fetchone()[0])
        related = reserved["packet"].get("related_history")
        self.assertTrue(related, reserved["packet"])
        event = related[0]["events"][0]["id"]
        self.r.db.execute("UPDATE events SET outcome='failed' WHERE id=?", (event,))
        self.assert_rejected(call)

    def test_legacy_reservations_without_frozen_snapshot_cannot_gain_access(self):
        _, call = self.failed()
        self.r.db.execute("UPDATE admin_response_records SET reserved=json_remove(reserved,'$.history_snapshot') WHERE call_id=?", (call,))
        detail = status(self.r, self.p, call)["history_recovery"]
        self.assertFalse(detail["available"])
        self.assert_rejected(call)

    def test_optional_recovery_comparison_budget_does_not_block_normal_history_review(self):
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        with patch("agent_learning.history_response_recovery._comparison_digest",
                   side_effect=ContractError("history_comparison_budget_exceeded")), patch(
                "agent_learning.admin.subprocess.Popen", side_effect=fixtures.history_reviewer(fixtures.multiple)) as model:
            result = self.r.history_run(self.p, job, allow_review=True, max_calls=1)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["job"]["counts"]["candidates"], 3)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 1)
        call = self.r.db.execute("SELECT id FROM admin_calls ORDER BY created DESC LIMIT 1").fetchone()[0]
        self.assertFalse(status(self.r, self.p, call)["history_recovery"]["available"])

    def test_other_host_cannot_reauthorize_a_call_created_by_this_host(self):
        from agent_learning.history_response_recovery import recheck
        _, call = self.failed()
        saved = status(self.r, self.p, call)
        other = Runtime(self.r.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        with patch("agent_learning.admin.subprocess.Popen") as launch, self.assertRaises(ContractError):
            recheck(other, self.p, call, request_id="foreign-host", expected_version=saved["version"],
                    expected_job_version=saved["history_recovery"]["job_version"],
                    expected_session_epoch=other.session_epoch(), expected_governance_epoch=other.host_epoch(),
                    acknowledge_boundary=True, reason="Wrong host", confirmed=True)
        launch.assert_not_called()
        self.assertFalse(self.candidate_ids())

    def test_budget_complete_and_interrupted_batch_states_use_separate_local_authority(self):
        cases = (("materialized", "manual_review_available", False),
                 ("review_complete", "bounded_review_complete", False),
                 ("paused", "review_budget_exhausted", False),
                 ("paused", "batch_time_exhausted", False),
                 ("reviewing", "run_failed", True))
        for index, (state, reason, ack) in enumerate(cases):
            with self.subTest(state=state, reason=reason):
                runtime = Runtime(self.root / ("state-" + str(index)), config={})
                self.addCleanup(runtime.close)
                self.r, self.p = runtime, runtime.project(self.workspace)
                self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)
                job, call = self.failed()
                self.r.db.execute("UPDATE history_jobs SET state=?,last_result=?,owner=NULL,lease_until=0 WHERE id=?",
                                  (state, encode({"reason": reason}), job))
                self.assertEqual(status(self.r, self.p, call)["history_recovery"]["ack_required"], ack)
                answer = self.recover(call, acknowledge_boundary=ack)
                self.assertEqual(answer["status"], "accepted", answer)
                self.assertEqual(len(self.candidate_ids()), 3)
                actual = self.r.db.execute("SELECT state,review_enabled FROM history_jobs WHERE id=?", (job,)).fetchone()
                self.assertEqual(tuple(actual), (state, 0))

    def test_tampered_public_response_and_execution_plan_rejected(self):
        _, call = self.failed()
        self.r.db.execute("UPDATE admin_response_records SET response=json_set(response,'$.reason','Tampered') WHERE call_id=?", (call,))
        self.assert_rejected(call)

    def test_frozen_execution_plan_digest_is_required_even_without_model_routing(self):
        _, call = self.failed()
        body = json.loads(self.r.db.execute("SELECT body FROM admin_execution_plans WHERE call_id=?", (call,)).fetchone()[0])
        body["model"] = "tampered-original-model"
        self.r.db.execute("UPDATE admin_execution_plans SET body=? WHERE call_id=?", (encode(body), call))
        self.assert_rejected(call)

    def test_active_history_owner_and_other_local_recheck_cannot_be_bypassed_by_ack(self):
        job, call = self.failed()
        self.r.db.execute("UPDATE history_jobs SET state='reviewing',review_enabled=1,owner='live-worker',lease_until=? WHERE id=?",
                          (time.time() + 60, job))
        self.assert_rejected(call)
        self.r.db.execute("UPDATE history_jobs SET state='paused',review_enabled=0,owner=NULL,lease_until=NULL WHERE id=?", (job,))
        now = time.time()
        self.r.db.execute("INSERT INTO admin_response_rechecks VALUES(?,?,?,?,?,'validating','another-owner',?,NULL,?,?)",
                          (self.p, self.r._session_scope(), "other-request", "other-digest", call, now + 30, now, now))
        self.assert_rejected(call)

    def test_response_and_job_version_cas_prevents_stale_confirmation(self):
        job, call = self.failed()
        saved = status(self.r, self.p, call)
        old_version = saved["history_recovery"]["job_version"]
        self.r.history_pause(self.p, job)
        self.assert_rejected(call, expected_job_version=old_version)
        self.assert_rejected(call, expected_version=saved["version"] + 1)

    def test_active_history_request_prevents_local_acceptance(self):
        job, call = self.failed()
        now = time.time()
        self.r.db.execute("INSERT INTO history_review_requests VALUES(?,?,?,?,?,?,?,'running',?,?,?,?)", (
            self.p, self.r._session_scope(), self.r.session_epoch(), "live-request", "request-digest", job,
            encode({"expected_governance_epoch": self.r.host_epoch()}), now, now, now + 60, "{}"))
        self.assert_rejected(call)

    def test_expired_independent_recheck_lease_rolls_back_all_candidates(self):
        from agent_learning.admin import _accept_response
        _, call = self.failed()

        def expire(runtime, project, reserved, response):
            runtime.db.execute("UPDATE admin_response_rechecks SET lease_until=0 WHERE call_id=?", (call,))
            return _accept_response(runtime, project, reserved, response)

        with patch("agent_learning.admin._accept_response", side_effect=expire):
            self.assert_rejected(call)

    def test_candidate_acceptance_failure_rolls_back_every_artifact_and_source_link(self):
        _, call = self.failed()
        original = self.r.submit_candidate
        attempted = []

        def fail_second(*args, **kwargs):
            attempted.append(kwargs)
            if len(attempted) == 2:
                raise ContractError("Synthetic second candidate write failure")
            return original(*args, **kwargs)

        with patch.object(self.r, "submit_candidate", side_effect=fail_second):
            self.assert_rejected(call)
        self.assertEqual(len(attempted), 2)
        for table in ("candidates", "candidate_identity", "governance_proposals", "review_receipts"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)

    def test_recovered_candidate_authority_does_not_cover_a_sibling_candidate(self):
        _, call = self.failed()
        self.assertEqual(self.recover(call)["status"], "accepted")
        row = dict(self.r.db.execute("SELECT * FROM candidates ORDER BY id LIMIT 1").fetchone())
        body = json.loads(row["body"])
        body.update(title="Unrelated sibling", content="This is outside the saved response")
        self.r.db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)", (
            "sibling-not-authorized", self.p, row["problem"], row["problem_revision"], encode(body), "candidate", time.time()))
        detail = candidate_review_detail(self.r, self.p, "sibling-not-authorized")
        self.assertFalse(detail["preflight"]["publishable"])
        self.assertTrue(any(item["code"] == "history_blocked" for item in detail["preflight"]["issues"]))

    def test_failed_or_unknown_provider_response_cannot_local_recover(self):
        _, call = self.failed()
        reserved = json.loads(self.r.db.execute("SELECT reserved FROM admin_response_records WHERE call_id=?", (call,)).fetchone()[0])
        reserved.pop("terminal_observation", None)
        self.r.db.execute("UPDATE admin_response_records SET reserved=?,reserved_digest=? WHERE call_id=?",
                          (encode(reserved), digest(reserved), call))
        for state, observed, code in (("timeout_unknown", True, 0), ("failed", False, 0), ("failed", True, 1)):
            with self.subTest(state=state, observed=observed, code=code):
                self.r.db.execute("UPDATE admin_calls SET state=?,usage=? WHERE id=?", (state,
                    encode({"execution": {"terminal_observed": observed, "returncode": code}}), call))
                self.assert_rejected(call)

    def test_certified_unknown_calls_restore_reply_without_changing_unknown_usage_reservations(self):
        from agent_learning.admin_usage import admin_project_budget_status
        for state in ("orphaned_unknown", "account_changed_unknown"):
            with self.subTest(state=state):
                runtime = Runtime(self.root / ("cert-" + state), config={})
                self.addCleanup(runtime.close)
                self.r, self.p = runtime, runtime.project(self.workspace)
                self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)
                _, call = self.failed()
                if state == "account_changed_unknown":
                    self.r.account_changed()
                self.r.db.execute("UPDATE admin_calls SET state=? WHERE id=?", (state, call))
                paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
                before = admin_project_budget_status(self.r, self.p)
                with patch("agent_learning.admin.subprocess.Popen") as launch:
                    answer = self.recover(call)
                launch.assert_not_called()
                self.assertEqual(answer["status"], "accepted", answer)
                self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone()), paid)
                after = admin_project_budget_status(self.r, self.p)
                for key in ("counted_attempts", "reported_tokens", "token_reservations", "accounted_tokens"):
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(after["locally_recovered_reviews"], before["locally_recovered_reviews"] + 1)
                self.assertEqual(after["effective_completed_reviews"], before["effective_completed_reviews"] + 1)
                for key in ("result_unknown_attempts", "usage_unknown_attempts"):
                    self.assertEqual(after[key], before[key], key)
                recent = review_activity(self.r, self.p)["recent"][0]
                self.assertTrue(recent["local_recheck"])
                self.assertEqual(recent["execution_state"], state)
                self.assertIn("history_recovery", recent["saved_response"])

    def test_uncertified_unknown_call_does_not_infer_success_from_public_reply_alone(self):
        _, call = self.failed()
        reserved = json.loads(self.r.db.execute("SELECT reserved FROM admin_response_records WHERE call_id=?", (call,)).fetchone()[0])
        reserved.pop("terminal_observation", None)
        self.r.db.execute("UPDATE admin_response_records SET reserved=?,reserved_digest=? WHERE call_id=?",
                          (encode(reserved), digest(reserved), call))
        self.r.db.execute("UPDATE admin_calls SET state='orphaned_unknown' WHERE id=?", (call,))
        self.assert_rejected(call)

    def test_each_host_has_same_local_recovery_boundary(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime = Runtime(self.root / ("host-" + host), config={}, host=host)
                self.addCleanup(runtime.close)
                self.r, self.p = runtime, runtime.project(self.workspace)
                self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)
                # Exercise each host identity with the same offline transport.
                # Native CLI/SDK availability is verified by provider suites.
                self.r.admin_configure(self.p, False, provider="codex", model_mode="default",
                                       command=[sys.executable], expected_policy_version=0)
                _, call = self.failed()
                with patch("agent_learning.admin.subprocess.Popen") as launch:
                    answer = self.recover(call)
                launch.assert_not_called()
                self.assertEqual(answer["status"], "accepted", answer)
                plan = prepare_review(self.r, self.p, self.candidate_ids()[:1], "approve")
                self.assertTrue(plan["ready"], plan)

    def test_later_pause_cancel_account_or_job_change_invalidates_prepared_approval(self):
        for change in ("pause", "cancel", "account", "job"):
            with self.subTest(change=change):
                runtime = Runtime(self.root / ("changed-" + change), config={})
                self.addCleanup(runtime.close)
                self.r, self.p = runtime, runtime.project(self.workspace)
                self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)
                job, call = self.failed()
                self.assertEqual(self.recover(call)["status"], "accepted")
                identity = self.candidate_ids()[0]
                plan = prepare_review(self.r, self.p, [identity], "approve")
                self.assertTrue(plan["ready"], plan)
                if change == "pause":
                    self.r.history_pause(self.p, job)
                elif change == "cancel":
                    self.r.history_cancel(self.p, job)
                elif change == "account":
                    self.r.account_changed()
                else:
                    self.r.db.execute("UPDATE history_jobs SET updated=updated+1 WHERE id=?", (job,))
                detail = candidate_review_detail(self.r, self.p, identity)
                self.assertFalse(detail["preflight"]["publishable"], detail)
                with self.assertRaises(ContractError):
                    commit_review(self.r, self.p, plan, "stale-history-plan")
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_explicit_access_reconfirmation_after_pause_keeps_exact_artifacts_and_call_ledger(self):
        job, call = self.failed()
        self.assertEqual(self.recover(call)["status"], "accepted")
        identities = self.candidate_ids()
        old_plan = prepare_review(self.r, self.p, identities[:1], "approve")
        self.r.history_pause(self.p, job)
        self.assertFalse(prepare_review(self.r, self.p, identities[:1], "approve")["ready"])
        self.assertTrue(status(self.r, self.p, call)["history_recovery"]["access_reconfirmation"])
        paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
        batch = dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())
        with patch("agent_learning.admin.subprocess.Popen") as launch, patch(
                "agent_learning.admin._accept_response", side_effect=AssertionError("Must not accept response twice")):
            answer = self.recover(call, request_id="reconfirm-paused-artifacts")
        launch.assert_not_called()
        self.assertEqual(answer["status"], "access_reconfirmed", answer)
        self.assertEqual(self.candidate_ids(), identities)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone()), paid)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone()), batch)
        with self.assertRaises(ContractError):
            commit_review(self.r, self.p, old_plan, "old-plan-after-reconfirmation")
        plan = prepare_review(self.r, self.p, identities[:1], "approve")
        self.assertTrue(plan["ready"], plan)
        commit_review(self.r, self.p, plan, "new-plan-after-reconfirmation")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)

    def test_shared_candidate_uses_latest_explicit_accepted_call_grant_without_old_fallback(self):
        from agent_learning.history_response_recovery import result_access, result_snapshot
        job, first_call = self.failed()
        self.assertEqual(self.recover(first_call)["status"], "accepted")
        identity = self.candidate_ids()[0]
        candidate = self.r.db.execute("SELECT problem FROM candidates WHERE id=?", (identity,)).fetchone()
        problem = candidate[0]
        first_grant = dict(self.r.db.execute("SELECT * FROM history_response_accepts WHERE call_id=?",
                                            (first_call,)).fetchone())
        self.r.history_pause(self.p, job)
        with self.assertRaises(ContractError):
            result_access(self.r, self.p, problem, candidate_id=identity)

        def clone(table, row, **values):
            copied = dict(row)
            copied.update(values)
            columns = list(copied)
            self.r.db.execute("INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES(" +
                              ",".join("?" for _ in columns) + ")", [copied[name] for name in columns])

        # A separate paid attempt may yield the same deterministic candidate.
        # Freeze the second call and exact result receipt without executing a
        # model; all source, reply, plan and artifact digests remain consistent.
        second_call = "second-saved-call"
        clone("admin_calls", self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (first_call,)).fetchone(), id=second_call)
        clone("admin_execution_plans", self.r.db.execute("SELECT * FROM admin_execution_plans WHERE call_id=?",
              (first_call,)).fetchone(), call_id=second_call)
        record = dict(self.r.db.execute("SELECT * FROM admin_response_records WHERE call_id=?", (first_call,)).fetchone())
        reserved = json.loads(record["reserved"])
        reserved["call_id"] = second_call
        clone("admin_response_records", record, call_id=second_call, reserved=encode(reserved), reserved_digest=digest(reserved))
        receipt = self.r.db.execute("SELECT * FROM review_receipts WHERE call_id=?", (first_call,)).fetchone()
        clone("review_receipts", receipt, call_id=second_call, review_signature=digest(["second-result", second_call]))
        grant = json.loads(first_grant["body"])
        grant.update(call_id=second_call, request_id="second-explicit-authorization",
                     job_version=digest(dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())))
        clone("history_response_accepts", first_grant, call_id=second_call, request_id=grant["request_id"],
              body=encode(grant), updated=first_grant["updated"] + 10)
        for artifact in self.r.db.execute("SELECT * FROM history_response_artifacts WHERE call_id=?", (first_call,)).fetchall():
            clone("history_response_artifacts", artifact, call_id=second_call)
        marker = result_snapshot(self.r, self.p, candidate_id=identity)
        self.assertEqual(marker["call_id"], second_call)
        self.assertTrue(result_access(self.r, self.p, problem, candidate_id=identity)["local_response_recovery"])

        # Make the older grant valid and the latest one stale. Selection must
        # still honor the latest explicit grant and reject, without falling back.
        older = json.loads(first_grant["body"])
        older["job_version"] = grant["job_version"]
        self.r.db.execute("UPDATE history_response_accepts SET body=? WHERE call_id=?", (encode(older), first_call))
        grant["job_version"] = "0" * 64
        self.r.db.execute("UPDATE history_response_accepts SET body=? WHERE call_id=?", (encode(grant), second_call))
        self.assertEqual(result_snapshot(self.r, self.p, candidate_id=identity)["call_id"], second_call)
        with self.assertRaises(ContractError):
            result_access(self.r, self.p, problem, candidate_id=identity)


if __name__ == "__main__":
    unittest.main()
