"""Failed review processes cool down across cases; no model or real host calls."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent_learning.admin import admin_once, ReviewStateChanged
from agent_learning.admin_queue import (claim_dispatch, dispatch_diagnostic, executor_backoff,
                                       record_executor_failure, executor_signature)
from agent_learning.runtime import ContractError, Runtime
from agent_learning.scheduler import SCHEDULER_DEFAULTS


class AdminExecutionBackoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-execution-backoff-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {
            "enabled": True, "command": [sys.executable], "timeout_seconds": 10}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        for index in range(3):
            self.r.feedback(self.p, "chat", f"Independent observation {index}")

    def process(self, code=7):
        process = Mock(pid=4242, returncode=code, stdin=io.BytesIO(), stdout=io.BytesIO())
        process.poll.return_value = code
        process.wait.return_value = code
        return process

    def expire(self):
        self.r.db.execute("UPDATE admin_executor_failures SET next_try=0")

    def test_nonzero_exit_cools_other_cases_without_refunding_or_retrying(self):
        process = self.process()
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=process) as launch:
            failed = admin_once(self.r, self.p)
            blocked = admin_once(self.r, self.p)
            self.assertIsNone(claim_dispatch(self.r, self.p))
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(blocked["status"], "executor_not_ready")
        self.assertEqual(blocked["reason"], "executor_backoff")
        self.assertEqual(failed["usage"]["execution"]["reason_code"], "executor_nonzero_exit")
        self.assertEqual(failed["usage"]["execution"]["returncode"], 7)
        self.assertTrue(failed["usage"]["execution"]["input_submission_attempted"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        row = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls").fetchone()
        self.assertEqual(tuple(row), ("failed", 0))
        self.assertEqual(self.r.db.execute("SELECT completeness FROM admin_call_budgets").fetchone()[0], "unknown")

    def test_dsh_profile_change_uses_a_separate_executor_cooldown_identity(self):
        plan = {"provider": "dsh", "command": [sys.executable], "model": "same-model", "session_epoch": 0,
                "dsh_profile": {"source": "operator_file", "profile_directory": "profile-a", "sha256": "a" * 64}}
        other = dict(plan, dsh_profile=dict(plan["dsh_profile"], sha256="b" * 64))
        with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value=None):
            record_executor_failure(self.r, self.p, plan, "executor_initialization_failed")
            self.assertNotEqual(executor_signature(plan), executor_signature(other))
            self.assertIsNotNone(executor_backoff(self.r, self.p, plan))
            self.assertIsNone(executor_backoff(self.r, self.p, other))

    def test_started_process_does_not_reset_failures_and_valid_commit_does(self):
        plan = self.r.resolve_admin_executor(self.p, "chat")
        record_executor_failure(self.r, self.p, plan, "executor_nonzero_exit")
        self.expire()
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=self.process()):
            failed = admin_once(self.r, self.p)
        backoff = executor_backoff(self.r, self.p, plan)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(backoff["failures"], 2)
        self.assertGreater(backoff["next_try"], time.time() + 110)
        self.expire()
        response = {"decision": "skip", "reason": "No reusable value", "candidate": None,
                    "missing_evidence": [], "proposal": None}
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=self.process(0)), \
                patch("agent_learning.admin_providers.read_response", return_value=response):
            succeeded = admin_once(self.r, self.p)
        self.assertEqual(succeeded["status"], "skip")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_executor_failures").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE call_id=?",
                                          (succeeded["call_id"],)).fetchone()[0], 1)

    def test_invalid_response_is_durable_but_does_not_store_exception_text(self):
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=self.process(0)), \
                patch("agent_learning.admin_providers.read_response", side_effect=ContractError("PRIVATE_TOKEN and evidence")):
            result = admin_once(self.r, self.p)
        execution = result["usage"]["execution"]
        self.assertEqual(execution["phase"], "response_validation")
        self.assertEqual(execution["reason_code"], "executor_response_invalid")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(dispatch_diagnostic(self.r, self.p)))
        stored = json.loads(self.r.db.execute("SELECT usage FROM admin_calls WHERE id=?", (result["call_id"],)).fetchone()[0])
        self.assertEqual(stored["execution"], execution)

    def test_changed_local_context_is_not_an_executor_failure(self):
        response = {"decision": "skip", "reason": "No reusable value", "candidate": None,
                    "missing_evidence": [], "proposal": None}
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=self.process(0)), \
                patch("agent_learning.admin_providers.read_response", return_value=response), \
                patch("agent_learning.admin._accept_response", side_effect=ReviewStateChanged("changed local revision")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["usage"]["execution"]["reason_code"], "review_context_changed")
        self.assertEqual(self.r.db.execute("SELECT count(*) FROM admin_executor_failures").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT count(*) FROM admin_calls").fetchone()[0], 1)

    def test_timeout_unknown_cools_different_cases_and_keeps_unknown_usage(self):
        process = self.process()
        process.communicate.side_effect = subprocess.TimeoutExpired("PRIVATE_ARGV", 10)
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=process):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "timeout_unknown")
        self.assertEqual(result["usage"]["execution"]["reason_code"], "executor_timeout")
        self.assertIsNone(claim_dispatch(self.r, self.p))
        self.assertIsNotNone(executor_backoff(self.r, self.p, self.r.resolve_admin_executor(self.p, "chat")))
        self.assertTrue(result["usage"]["call_outcome_unknown"])
        self.assertNotIn("PRIVATE_ARGV", json.dumps(result))

    def test_unsafe_stream_initialization_never_submits_evidence_and_cools_executor(self):
        self.r.config["admin"].update(provider="antigravity", antigravity_command=[sys.executable])
        initialized = threading.Event()
        initialized.set()
        state = {"initialized": initialized, "init_safe": False, "error": True, "terminal": False,
                 "mark_input_sent": Mock()}
        thread = Mock()
        thread.is_alive.return_value = False
        process = self.process()
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", return_value=process), \
                patch("agent_learning.admin_providers.collect_antigravity_stream", return_value=(thread, state)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["usage"]["execution"]["reason_code"], "executor_initialization_failed")
        self.assertFalse(result["usage"]["execution"]["input_submission_attempted"])
        process.communicate.assert_not_called()
        state["mark_input_sent"].assert_not_called()
        self.assertIsNone(claim_dispatch(self.r, self.p))


class SchedulerExecutionFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-scheduler-review-failure-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": True, "command": [sys.executable]},
            "scheduler": {**SCHEDULER_DEFAULTS, "enabled": True, "poll_seconds": 1, "retry_seconds": 30}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.feedback(self.p, "chat", "First observation")

    def job(self):
        return dict(self.r.db.execute("SELECT * FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone())

    def test_failed_or_unknown_batch_is_cooled_even_after_new_material(self):
        for reason in ("failed", "timeout_unknown", "account_changed_unknown", "orphaned_unknown", "not_started"):
            with self.subTest(reason=reason):
                self.r.db.execute("DELETE FROM scheduler_jobs")
                with patch("agent_learning.admin.admin_batch", return_value={
                        "status": "batch_complete", "stop_reason": reason, "invocations": 1, "scanned_count": 1}) as batch:
                    self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
                    first = self.job()
                    self.assertEqual(first["wait_reason"], "retry_backoff")
                    self.assertGreater(first["next_due"], time.time() + 25)
                    self.r.feedback(self.p, "chat", "Changed material " + reason)
                    self.r.scheduler_wake(self.p)
                    self.r.scheduler_status(self.p)
                    self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
                    self.assertEqual(batch.call_count, 1)
                    self.assertGreaterEqual(self.job()["next_due"], first["next_due"])

    def test_scheduler_retry_cannot_shorten_executor_circuit(self):
        plan = self.r.resolve_admin_executor(self.p, "chat")
        def failed(runtime, project, **kwargs):
            for _ in range(3):
                record_executor_failure(runtime, project, plan, "executor_nonzero_exit")
            return {"status": "batch_complete", "stop_reason": "failed", "invocations": 1, "scanned_count": 1}
        with patch("agent_learning.admin.admin_batch", side_effect=failed):
            self.r.scheduler_tick(self.p)
        circuit = executor_backoff(self.r, self.p, plan)
        self.assertGreaterEqual(self.job()["next_due"], circuit["next_try"])
        self.r.feedback(self.p, "chat", "More material must not bypass a circuit")
        with patch("agent_learning.admin.admin_batch") as batch:
            self.r.scheduler_wake(self.p)
            self.r.scheduler_status(self.p)
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        batch.assert_not_called()
        self.assertGreaterEqual(self.job()["next_due"], circuit["next_try"])


if __name__ == "__main__":
    unittest.main()
