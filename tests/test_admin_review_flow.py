"""Review visibility, local deferral, scoped publication and one-shot history work."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_control import apply_control, control_status
from agent_learning.admin_publication import publication_preview
from agent_learning.admin_review_flow import evidence_readiness, publication_policy
from agent_learning.history_review_control import request_history_review, run_history_review
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer
from tests import test_admin_control as control_fixtures


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-flow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.config = {"admin": {"enabled": True, "command": [sys.executable], "max_calls_per_day": 100,
                                 "timeout_seconds": 10}}
        self.r = Runtime(self.root / "home", config=copy.deepcopy(self.config))
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        from agent_learning.phase_review import configure
        configure(self.r, self.p, {"mode":"legacy"}, 0)
        self.r.admin_observe_model(self.p, "root", "observed-model")
        self.task = self.r.task_update(self.p, "root", "A bounded project task")

    def control(self, **values):
        state = control_status(self.r, self.p)
        result = {"request_id": "policy", "expected_policy_version": state["policy"]["policy_version"],
            "expected_settings_version": state["settings_version"], "expected_session_epoch": state["session_epoch"],
            "expected_governance_epoch": state["governance_epoch"],
            "expected_project_budget_version": state["project_budget_policy"]["version"],
            "project_limits": {}, "enabled": True, "model_mode": "default", "model": "", "start": False}
        result.update(values)
        return result

    def event(self, outcome="failed"):
        return self.r.record_event(self.p, "root", "PostToolUse", {"tool_name": "shell", "tool_response": {"exit_code": 1 if outcome == "failed" else 0}},
            outcome=outcome, task_id=self.task["id"], task_revision=self.task["revision"])

    def bare_case(self):
        event = self.event()
        return self.r._problem(self.p, "root", "shell failed: raw failure only", "operation", [event["id"]])

    def history(self):
        (self.workspace / "README.md").write_text("# Project notes\nThe memory store is SQLite.\n", encoding="utf-8")
        identity = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.r.history_run(self.p, identity, allow_review=False, max_calls=0)
        state = control_status(self.r, self.p)
        item = next(x for x in state["recovery"]["history_jobs"] if x["id"] == identity)
        return {"request_id": "history-review", "job_id": identity, "version": item["version"], "confirmed": True,
            "max_calls": 1, "max_seconds": 60, "expected_policy_version": state["policy"]["policy_version"],
            "expected_settings_version": state["settings_version"], "expected_session_epoch": state["session_epoch"],
            "expected_governance_epoch": state["governance_epoch"],
            "expected_project_budget_version": state["project_budget_policy"]["version"]}

    def test_bare_automatic_failure_waits_locally_without_a_model_or_call_charge(self):
        problem = self.bare_case()
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(result["status"], "needs_evidence")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        state = control_status(self.r, self.p)
        item = state["review_activity"]["deferred"][0]
        self.assertEqual(item["problem_id"], problem["id"])
        self.assertEqual(item["reason"], "awaiting_recovery_evidence")
        self.assertFalse(item["model_called"])

    def test_explicit_case_review_remains_possible_after_local_deferral(self):
        problem = self.bare_case()
        admin_once(self.r, self.p)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")) as model:
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "skip")
        item = control_status(self.r, self.p)["review_activity"]["recent"][0]
        self.assertEqual(item["purpose"], "daily_review")
        self.assertEqual(item["evidence"]["failures"], 1)

    def test_recovery_evidence_releases_locally_deferred_case(self):
        self.bare_case()
        admin_once(self.r, self.p)
        success = self.event("succeeded")
        problem = self.r._problem(self.p, "root", "shell failed: raw failure only", "operation", [success["id"]])
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["problem_id"], problem["id"])

    def test_feedback_and_incomplete_packets_are_not_silently_filtered(self):
        packet = {"problem": {"category": "operation"}, "events": [{"type": "Feedback", "outcome": "reported"}]}
        self.assertIsNone(evidence_readiness(packet))
        packet.update(events=[{"type": "PostToolUse", "outcome": "failed"}], evidence_truncated=True)
        self.assertIsNone(evidence_readiness(packet))

    def test_publication_switch_is_explicit_scoped_persistent_and_never_launches(self):
        request = self.control(auto_publish=True)
        with patch("subprocess.Popen") as model:
            result = apply_control(self.r, self.p, request)
            self.assertEqual(apply_control(self.r, self.p, request), result)
        model.assert_not_called()
        self.assertTrue(self.r.admin_effective_config(self.p)["auto_publish"])
        other = Runtime(self.r.home, config=copy.deepcopy(self.config), host="antigravity")
        self.addCleanup(other.close)
        self.assertFalse(publication_policy(other, self.p)["enabled"])
        second = self.root / "other-project"
        second.mkdir()
        self.assertFalse(publication_policy(self.r, self.r.project(second))["enabled"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        with self.assertRaises(ContractError):
            apply_control(self.r, self.p, self.control(request_id="invalid", auto_publish="true"))

    def test_switch_enabled_during_review_keeps_candidate_without_retroactive_publication(self):
        problem = self.r.feedback(self.p, "root", "A reusable project observation", category="operation")
        def enable(_packet, _reviewer):
            apply_control(self.r, self.p, self.control(auto_publish=True))
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate", callback=enable)):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["publication"]["reason"], "publication_not_enabled_at_reservation")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_publication_preview_is_readonly_and_rejects_other_project(self):
        problem = self.r.feedback(self.p, "root", "A project observation", category="operation")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p, problem["id"])
        before = self.r.db.total_changes
        with patch("subprocess.Popen") as model:
            preview = publication_preview(self.r, self.p, result["candidate_id"])
        self.assertFalse(preview["evidence_eligible"])
        self.assertEqual(preview["reason"], "requires trusted tool-success evidence")
        self.assertEqual(self.r.db.total_changes, before)
        model.assert_not_called()
        with self.assertRaises(ContractError):
            publication_preview(self.r, "other-project", result["candidate_id"])

    def test_switch_disabled_during_review_preserves_result_without_publishing(self):
        apply_control(self.r, self.p, self.control(auto_publish=True))
        problem = self.r.feedback(self.p, "root", "Keep a paid review result", category="operation")
        def disable(_packet, _reviewer):
            apply_control(self.r, self.p, self.control(request_id="disable-publication", auto_publish=False))
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate", callback=disable)):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["publication"]["reason"], "automatic_publication_disabled")

    def test_real_schema18_control_row_gets_nullable_policy_without_enabling_publication(self):
        self.r.db.execute("DROP TABLE admin_control_state")
        self.r.db.execute("CREATE TABLE admin_control_state(project TEXT,host_scope TEXT,enabled INTEGER,version INTEGER,updated REAL,PRIMARY KEY(project,host_scope))")
        self.r.db.execute("INSERT INTO admin_control_state VALUES(?,?,1,7,0)", (self.p, self.r._session_scope()))
        self.r.db.execute("PRAGMA user_version=18")
        upgraded = Runtime(self.r.home, config=copy.deepcopy(self.config))
        self.addCleanup(upgraded.close)
        state = control_status(upgraded, self.p)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["settings_version"], 7)
        self.assertFalse(state["publication_policy"]["configured"])
        self.assertFalse(state["publication_policy"]["enabled"])

    def test_history_request_is_durable_and_duplicate_posts_never_launch_twice(self):
        body = self.history()
        with patch.object(self.r, "_launch_background") as launch:
            first = request_history_review(self.r, self.p, body)
            self.assertEqual(request_history_review(self.r, self.p, body), first)
        launch.assert_called_once()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertFalse(first["history_review"]["publication_automatic"])
        with self.assertRaises(ContractError):
            request_history_review(self.r, self.p, dict(body, max_calls=2))
        with self.assertRaises(ContractError):
            request_history_review(self.r, self.p, dict(body, request_id="another"))

    def test_history_worker_runs_once_uses_only_frozen_archive_and_never_publishes(self):
        body = self.history()
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, body)
        self.r.config["admin"]["auto_publish"] = True
        with patch("agent_learning.history_sources.scan_sources", side_effect=AssertionError("worker expanded sources")), \
                patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = run_history_review(self.r, self.p, body["request_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["calls"], 1)
        self.assertEqual(result["counts"]["candidates"], 1)
        with patch("subprocess.Popen") as model:
            self.assertEqual(run_history_review(self.r, self.p, body["request_id"])["status"], "not_started")
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_history_confirmation_versions_host_and_limits_are_enforced(self):
        body = self.history()
        for values in ({"confirmed": False}, {"max_calls": 0}, {"max_seconds": 601}, {"version": "old"},
                       {"expected_settings_version": body["expected_settings_version"] + 1}, {"unexpected": True}):
            with self.assertRaises(ContractError):
                request_history_review(self.r, self.p, dict(body, **values))
        other = Runtime(self.r.home, config=copy.deepcopy(self.config), host="antigravity")
        self.addCleanup(other.close)
        self.assertEqual(run_history_review(other, self.p, body["request_id"])["status"], "not_started")

    def test_history_launch_failure_is_not_automatically_retried(self):
        body = self.history()
        with patch.object(self.r, "_launch_background", side_effect=OSError("fixture")) as launch:
            response = request_history_review(self.r, self.p, body)
            again = request_history_review(self.r, self.p, body)
        self.assertEqual(response, again)
        self.assertEqual(response["status"], "launch_failed")
        launch.assert_called_once()

    def test_explicit_history_review_can_resume_pause_but_cannot_resume_cancellation(self):
        body = self.history()
        self.r.db.execute("UPDATE history_jobs SET state='paused',last_result=? WHERE id=?",
                          (json.dumps({"reason": "user_paused"}), body["job_id"]))
        state = control_status(self.r, self.p)
        item = next(x for x in state["recovery"]["history_jobs"] if x["id"] == body["job_id"])
        self.assertTrue(item["can_review"])
        body["version"] = item["version"]
        with patch.object(self.r, "_launch_background") as launch:
            request_history_review(self.r, self.p, body)
        launch.assert_called_once()
        self.r.db.execute("UPDATE history_jobs SET state='cancelled' WHERE id=?", (body["job_id"],))
        result = run_history_review(self.r, self.p, body["request_id"])
        self.assertEqual(result["status"], "not_started")

    def test_queued_history_worker_cancels_after_model_or_batch_changed(self):
        body = self.history()
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, body)
        self.r.db.execute("UPDATE history_jobs SET state='cancelled',updated=updated+1 WHERE id=?", (body["job_id"],))
        with patch("subprocess.Popen") as model:
            result = run_history_review(self.r, self.p, body["request_id"])
        self.assertEqual(result["status"], "not_started")
        model.assert_not_called()

    def test_schema18_upgrade_preserves_settings_and_fences_old_handles_once(self):
        apply_control(self.r, self.p, self.control(auto_publish=True))
        old_epoch = self.r.session_epoch()
        handle = self.r.register_context(self.p, "root")["context_handle"]
        self.r.db.execute("PRAGMA user_version=18")
        with patch("subprocess.Popen") as model:
            upgraded = Runtime(self.r.home, config=copy.deepcopy(self.config))
        self.addCleanup(upgraded.close)
        model.assert_not_called()
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 24)
        self.assertGreater(upgraded.session_epoch(), old_epoch)
        self.assertTrue(publication_policy(upgraded, self.p)["enabled"])
        with self.assertRaises(ContractError):
            self.r.access_context(self.p, handle, expected_epoch=old_epoch)


class ReviewFlowHttpTests(control_fixtures.AdminControlHttpTests):
    def batch_request(self):
        (self.workspace / "notes.md").write_text("A project convention.", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.r.history_run(self.p, job, max_calls=0)
        state = control_status(self.r, self.p)
        item = next(x for x in state["recovery"]["history_jobs"] if x["id"] == job)
        return {"request_id": "http-history", "job_id": job, "version": item["version"], "confirmed": True,
            "max_calls": 2, "max_seconds": 60, "expected_policy_version": state["policy"]["policy_version"],
            "expected_settings_version": state["settings_version"], "expected_session_epoch": state["session_epoch"],
            "expected_governance_epoch": state["governance_epoch"],
            "expected_project_budget_version": state["project_budget_policy"]["version"]}

    def test_history_post_receipt_is_idempotent_and_requires_admin_and_same_origin(self):
        body = self.batch_request()
        for headers in ({"X-Admin-Token": None}, {"Origin": "https://outside.example"}):
            code, _ = self.request("/api/admin-control/history-review", body, **headers)
            self.assertEqual(code, 403)
        with patch("agent_learning.runtime.Runtime._launch_background") as launch:
            code, result = self.request("/api/admin-control/history-review", body)
            self.assertEqual(code, 200, result)
            code, duplicate = self.request("/api/admin-control/history-review", body)
        self.assertEqual(result, duplicate)
        launch.assert_called_once()
        code, receipt = self.request("/api/admin-control/receipt?request_id=http-history")
        self.assertEqual(code, 200)
        self.assertEqual(receipt["response"], result)

    def test_publication_preview_does_not_construct_runtime_or_call_model(self):
        problem = self.problem
        refs = [r[0] for r in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="A project fact", content="Observed project text", category="project", applicability="Same project",
            limitations="Reported", evidence_refs=refs, validation_level="reported")
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("read constructed runtime")), \
                patch("subprocess.Popen") as model:
            code, result = self.request("/api/admin-control/publication-preview?candidate_id=" + candidate["candidate_id"])
        self.assertEqual(code, 200, result)
        self.assertFalse(result["published"])
        model.assert_not_called()
