"""Recovery permissions, read-only visibility and host CLI selection; no paid calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import _usage
from agent_learning.admin_control import control_status
from agent_learning.admin_lifecycle import claim_call
from agent_learning.admin_providers import resolve_command
from agent_learning.admin_queue import executor_signature
from agent_learning.admin_recovery import apply_recovery, recovery_status
from agent_learning.runtime import Runtime, ContractError, encode
from tests import test_admin_control as control_fixtures


class AdminRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"command": [sys.executable]}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def failed(self):
        problem = self.r.feedback(self.p, "root", "A failed review of a project observation")
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["id"], problem["revision"],
                              "fingerprint", self.r.host_epoch(), 10)
            snapshot = self.r.resolve_admin_executor(self.p, require_enabled=False)
            self.r.record_admin_execution(self.p, call["call_id"], snapshot, require_enabled=False)
            self.r.db.execute("UPDATE admin_calls SET state='failed',started=?,finished=?,lease_until=NULL,usage=? WHERE id=?",
                              (time.time(), time.time(), encode({"reported_turns": 0, "token_totals": {}}), call["call_id"]))
        return call["call_id"]

    def request(self, action, item, **values):
        body = {"request_id": "explicit-recovery", "expected_policy_version": self.r.admin_policy(self.p)["policy_version"],
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch(),
                "action": action, "id": item["id"], "version": item["version"],
                "reason": "User explicitly confirmed this bounded recovery", "confirmed": True}
        body.update(values)
        return body

    def history(self):
        (self.workspace / "notes.md").write_text("A documented project fact.\n", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.r.history_run(self.p, job, allow_review=False, max_calls=0)
        self.r.db.execute("UPDATE history_jobs SET state='paused',last_result=? WHERE id=?",
                          (encode({"reason": "review_budget_exhausted"}), job))
        self.r.account_changed()
        return job

    def test_status_is_readonly_and_does_not_treat_coverage_as_current_hook_proof(self):
        identity = self.failed()
        before = self.r.db.total_changes
        with patch("subprocess.Popen") as launch:
            state = control_status(self.r, self.p)
        self.assertEqual(self.r.db.total_changes, before)
        launch.assert_not_called()
        self.assertEqual(state["recovery"]["capture"]["current_hook"], "unknown")
        item = state["recovery"]["blocked_reviews"][0]
        self.assertEqual(item["id"], identity)
        self.assertFalse(item["usage_known"])
        self.assertEqual(item["reason"], "historical_failure_details_unavailable")

    def test_retry_preserves_original_state_usage_and_attempt_count_and_never_wakes(self):
        identity = self.failed()
        item = recovery_status(self.r, self.p)["blocked_reviews"][0]
        request = self.request("retry_call", item)
        with patch.object(self.r, "_launch_admin") as wake, patch("subprocess.Popen") as launch:
            result = apply_recovery(self.r, self.p, request)
            before = self.r.db.total_changes
            self.assertEqual(apply_recovery(self.r, self.p, request), result)
            self.assertEqual(self.r.db.total_changes, before)
        wake.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(result["models_started"])
        row = self.r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?", (identity,)).fetchone()
        self.assertEqual((row["state"], row["retry_allowed"]), ("failed", 1))
        self.assertEqual(json.loads(row["usage"])["token_totals"], {})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        with self.assertRaises(ContractError):
            apply_recovery(self.r, self.p, dict(request, reason="Different request content"))
        with self.assertRaises(ContractError):
            apply_recovery(self.r, self.p, dict(request, request_id="stale-snapshot"))

    def test_cross_project_host_and_stale_epoch_recovery_are_rejected(self):
        self.failed()
        item = recovery_status(self.r, self.p)["blocked_reviews"][0]
        request = self.request("retry_call", item)
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(ContractError):
            apply_recovery(self.r, self.r.project(other), request)
        ag = Runtime(self.r.home, config={}, host="antigravity")
        self.addCleanup(ag.close)
        self.assertEqual(recovery_status(ag, self.p)["blocked_reviews"], [])
        with self.assertRaises(ContractError):
            apply_recovery(ag, self.p, request)
        self.r.account_changed()
        with self.assertRaises(ContractError):
            apply_recovery(self.r, self.p, request)

    def test_history_reconfirmation_only_changes_access_without_scanning_or_models(self):
        identity = self.history()
        item = next(v for v in recovery_status(self.r, self.p)["history_jobs"] if v["id"] == identity)
        self.assertTrue(item["can_reauthorize"])
        sources = list(self.r.db.execute("SELECT id,content_hash,body FROM history_materials"))
        with patch("agent_learning.admin.admin_once") as model, patch.object(self.r, "history_prepare") as scan:
            result = apply_recovery(self.r, self.p, self.request("reauthorize_history", item))
        model.assert_not_called()
        scan.assert_not_called()
        self.assertFalse(result["knowledge_published"])
        row = self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (identity,)).fetchone()
        self.assertEqual((row["state"], row["host_epoch"], row["review_enabled"]), ("materialized", self.r.host_epoch(), 0))
        self.assertEqual(sources, list(self.r.db.execute("SELECT id,content_hash,body FROM history_materials")))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_cancelled_and_user_paused_batches_cannot_be_reauthorized_by_recovery(self):
        identity = self.history()
        for state in ("cancelled", "paused"):
            self.r.db.execute("UPDATE history_jobs SET state=?,last_result=? WHERE id=?",
                              (state, encode({"reason": "user_paused"}), identity))
            item = next(v for v in recovery_status(self.r, self.p)["history_jobs"] if v["id"] == identity)
            self.assertFalse(item["can_reauthorize"])
            with self.assertRaises(ContractError):
                apply_recovery(self.r, self.p, self.request("reauthorize_history", item, request_id=state))

    def test_recovery_requires_explicit_confirmation_and_reason(self):
        self.failed()
        item = recovery_status(self.r, self.p)["blocked_reviews"][0]
        for values in ({"confirmed": False}, {"reason": " "}, {"unexpected": True}, {"version": "x"}, {"action": []}):
            with self.assertRaises(ContractError):
                apply_recovery(self.r, self.p, dict(self.request("retry_call", item), **values))


class HostCliAndFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="host-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.root / "new" / "codex.exe"
        self.path_cli = self.root / "old" / "codex.exe"
        for file in (self.host, self.path_cli):
            file.parent.mkdir()
            file.write_bytes(b"fixture")

    def test_host_backend_overrides_only_bare_default_and_changes_executor_signature(self):
        plan = {"command": ["codex"], "provider": "codex", "model": "model-a", "session_epoch": 0}
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.host)}), patch("shutil.which", return_value=str(self.path_cli)):
            self.assertEqual(resolve_command(["codex"])["args"], [str(self.host.resolve())])
            self.assertEqual(resolve_command([str(self.path_cli)])["args"], [str(self.path_cli)])
            first = executor_signature(plan)
        with patch.dict(os.environ, {"CODEX_CLI_PATH": ""}), patch("shutil.which", return_value=str(self.path_cli)):
            second = executor_signature(plan)
        self.assertNotEqual(first, second)

    def test_invalid_host_hint_fails_closed_instead_of_using_an_older_path_cli(self):
        for hint in ("relative/codex.exe", str(self.root / "missing" / "codex.exe"), str(self.root / "other.exe")):
            with patch.dict(os.environ, {"CODEX_CLI_PATH": hint}), patch("shutil.which", return_value=str(self.path_cli)):
                with self.assertRaises(FileNotFoundError):
                    resolve_command(["codex"])

    def test_host_backend_change_invalidates_an_existing_execution_snapshot(self):
        runtime = Runtime(self.root / "home", config={})
        self.addCleanup(runtime.close)
        project = runtime.project(self.root)
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.host)}):
            first = runtime.resolve_admin_executor(project, require_enabled=False)
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.path_cli)}):
            current = runtime.resolve_admin_executor(project, require_enabled=False)
            self.assertNotEqual(first["executor_signature"], current["executor_signature"])
            with self.assertRaises(ContractError):
                runtime.assert_admin_executor_snapshot(project, first, require_enabled=False)

    def test_codex_machine_failure_is_retained_without_provider_text_or_fabricated_zero_usage(self):
        file = self.root / "events.log"
        file.write_text("\n".join(json.dumps(v) for v in [
            {"type": "turn.started"}, {"type": "turn.failed", "error": {
                "code": "model_not_found", "status_code": 404, "message": "private-secret private-evidence"}}]), encoding="utf-8")
        usage = _usage(file, 1)
        self.assertEqual(usage["provider_diagnostic"]["reason_code"], "model_unavailable")
        self.assertEqual(usage["provider_diagnostic"]["http_status"], 404)
        self.assertEqual(usage["provider_diagnostic"]["retry_policy"], "no_automatic_retry")
        self.assertEqual(usage["token_totals"], {})
        self.assertEqual(usage["completeness"], "unknown")
        self.assertNotIn("private-", json.dumps(usage))


class RecoveryHttpTests(unittest.TestCase):
    setUp = control_fixtures.AdminControlHttpTests.setUp
    stop = control_fixtures.AdminControlHttpTests.stop
    request = control_fixtures.AdminControlHttpTests.request
    body = control_fixtures.AdminControlTests.body

    def recovery_request(self):
        AdminRecoveryTests.failed(self)
        item = recovery_status(self.r, self.p)["blocked_reviews"][0]
        return AdminRecoveryTests.request(self, "retry_call", item)

    def test_recovery_requires_admin_capability_and_same_origin(self):
        body = self.recovery_request()
        for headers in ({"X-Admin-Token": None}, {"Origin": "https://example.invalid"}):
            code, _ = self.request("/api/admin-control/recovery", body, **headers)
            self.assertEqual(code, 403)
        self.assertEqual(self.r.db.execute("SELECT retry_allowed FROM admin_calls").fetchone()[0], 0)

    def test_http_recovery_has_a_durable_receipt_and_does_not_launch(self):
        body = self.recovery_request()
        with patch("subprocess.Popen") as launch:
            code, result = self.request("/api/admin-control/recovery", body)
            self.assertEqual(code, 200)
            self.assertFalse(result["models_started"])
            code, repeated = self.request("/api/admin-control/recovery", body)
            self.assertEqual(code, 200)
            self.assertEqual(result, repeated)
            code, receipt = self.request("/api/admin-control/receipt?request_id=" + body["request_id"])
        self.assertEqual(code, 200)
        self.assertEqual(receipt["response"], result)
        launch.assert_not_called()

    def test_get_includes_recovery_without_initializing_or_writing_runtime(self):
        self.recovery_request()
        before = self.r.db.total_changes
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("GET initialized Runtime")):
            code, result = self.request("/api/admin-control")
        self.assertEqual(code, 200)
        self.assertEqual(len(result["recovery"]["blocked_reviews"]), 1)
        self.assertEqual(self.r.db.total_changes, before)


if __name__ == "__main__":
    unittest.main()
