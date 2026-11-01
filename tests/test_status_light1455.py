"""Routine MCP diagnostics read bounded facts, without rebuilding the review queue."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin_lifecycle import claim_call, finish_call
from agent_learning.runtime import ContractError, Runtime
from agent_learning.status_output import MAX_MODEL_CHARS, status_summary
from tests.test_status_output import fixture, wire_size


class LightStatusTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "status-light1455"
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.home / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        environment = {"ALP_HOME": str(self.home), "ALP_CONFIG": str(self.config),
                       "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
                       "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("TMP", "TEMP", "TMPDIR"):
            environment[name] = str(self.root / "tmp")
        (self.root / "tmp").mkdir()
        env = patch.dict(os.environ, environment)
        env.start()
        self.addCleanup(env.stop)
        self.r = Runtime(self.home, config={"admin": {"enabled": True, "command": [sys.executable]}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def claim(self, project=None):
        project = project or self.p
        problem = self.r.feedback(project, "synthetic-chat", "Synthetic pending review")
        with self.r.transaction():
            call = claim_call(self.r, project, problem["id"], problem["revision"], "synthetic-fingerprint",
                              self.r.host_epoch(), 30)
        self.assertEqual(call["status"], "reserved")
        return call

    def test_light_read_skips_full_history_event_workload_and_queue_paths(self):
        statements = []
        self.r.db.set_trace_callback(statements.append)
        self.addCleanup(self.r.db.set_trace_callback, None)
        before = self.r.db.total_changes
        with contextlib.ExitStack() as stack:
            for method in ("status", "history_status", "work_memory_status", "learning_effects", "scheduler_status"):
                stack.enter_context(patch.object(self.r, method, side_effect=AssertionError(method)))
            for target in ("agent_learning.admin_workload.workload_summary", "agent_learning.phase_review.status",
                           "agent_learning.knowledge_health.report", "agent_learning.admin_queue.eligible_query",
                           "subprocess.Popen", "subprocess.run"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            source = self.r.status_light(self.p)
            result = status_summary(source)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertFalse(self.r.db.in_transaction)
        self.assertFalse(any("FROM EVENTS" in sql.upper() for sql in statements), statements)
        for key in ("counts", "observed_events", "outbox_states", "history_import", "workload", "learning_effects"):
            self.assertNotIn(key, source)
        self.assertNotIn("workload", result)
        self.assertNotIn("items", result["scheduler"])
        self.assertNotIn("total_jobs", result["scheduler"])
        self.assertNotIn("index_rebuild_pending", result["work_memory"])
        self.assertTrue(result["details_omitted"])
        self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)

    def test_identity_effective_gate_route_and_separate_ledgers_match_full(self):
        full = self.r.status(self.p)
        light = self.r.status_light(self.p)
        for key in ("project_id", "home", "host_scope", "host_epoch", "storage_schema_version", "knowledge_scope",
                    "admin_enabled", "admin_admission_enabled", "admin_routing", "project_usage", "global_usage",
                    "admission", "work_summary_policy", "hindsight_enabled"):
            self.assertEqual(light[key], full[key], key)
        self.assertIn("observed_events", full)
        self.assertIn("workload", full)
        self.assertIn("history_import", full)

    def test_unchecked_queue_does_not_reuse_historical_ready_diagnostic(self):
        self.r.feedback(self.p, "synthetic-chat", "A pending case does not start a model")
        self.r.db.execute("INSERT INTO admin_dispatch_diagnostics VALUES(?,?,?,?,?)",
                          (self.p, self.r._session_scope(), "ready", "{}", 1))
        before = self.r.db.total_changes
        with patch("subprocess.Popen", side_effect=AssertionError("model launch")):
            result = status_summary(self.r.status_light(self.p))
        diagnostic = result["admin_dispatch_diagnostic"]
        self.assertIs(diagnostic["queue_checked"], False)
        self.assertEqual((diagnostic["phase"], diagnostic["reason"]), ("idle", "queue_not_checked"))
        self.assertNotIn("last_observation", diagnostic)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_disabled_policy_and_active_lease_are_current_observations(self):
        self.r.admin_configure(self.p, enabled=False, command=[sys.executable])
        disabled = status_summary(self.r.status_light(self.p))
        self.assertFalse(disabled["admin_admission_enabled"])
        self.assertIs(disabled["admin_dispatch_diagnostic"]["queue_checked"], False)
        self.assertEqual(disabled["admin_dispatch_diagnostic"]["reason"], "disabled")
        self.r.admin_configure(self.p, enabled=True, command=[sys.executable])
        call = self.claim()
        before = self.r.db.total_changes
        active = status_summary(self.r.status_light(self.p))
        self.assertEqual(active["admin_dispatch_diagnostic"]["phase"], "model_reserved")
        self.assertIs(active["admin_dispatch_diagnostic"]["queue_checked"], False)
        self.assertEqual(active["admin_dispatch_diagnostic"]["next_due"], call["lease_until"])
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()[0], "reserved")

    def test_unknown_call_remains_reserved_in_global_ledger_without_other_project_attribution(self):
        call = self.claim()
        finish_call(self.r, call["call_id"], "timeout_unknown")
        other = self.r.project(self.root / "other-workspace")
        before = tuple(self.r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?",
                                        (call["call_id"],)).fetchone())
        result = status_summary(self.r.status_light(other))
        self.assertEqual(result["project_usage"]["scope"], "project")
        self.assertEqual(result["project_usage"]["counted_attempts"], 0)
        self.assertEqual(result["global_usage"]["scope"], "ALP_HOME")
        self.assertEqual(result["global_usage"]["result_unknown_attempts"], 1)
        self.assertEqual(result["global_usage"]["usage_unknown_attempts"], 1)
        self.assertEqual(tuple(self.r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?",
                                               (call["call_id"],)).fetchone()), before)
        own = self.r.status_light(self.p)
        self.assertGreater(own["project_usage"]["token_reservations"], 0)
        self.assertEqual(own["project_usage"]["result_unknown_attempts"], 1)

    def test_invalid_budget_configuration_remains_explicit_without_zero_metrics(self):
        with patch("agent_learning.admin_usage.admin_admission_status", side_effect=ContractError("invalid budget")):
            result = status_summary(self.r.status_light(self.p))
        self.assertEqual(result["admission"]["admission_status"], "invalid_configuration")
        self.assertEqual(result["admin_dispatch_diagnostic"]["reason"], "invalid_configuration")
        self.assertIs(result["admin_dispatch_diagnostic"]["queue_checked"], False)
        for name in ("project_usage", "global_usage"):
            self.assertEqual(result[name]["admission_status"], "invalid_configuration")
            self.assertNotIn("reported_tokens", result[name])

    def test_outer_snapshot_is_preserved(self):
        self.r.db.execute("BEGIN")
        try:
            before = self.r.db.total_changes
            self.r.status_light(self.p)
            self.assertTrue(self.r.db.in_transaction)
            self.assertEqual(self.r.db.total_changes, before)
        finally:
            self.r.db.execute("ROLLBACK")


class LightStatusProjectionTests(unittest.TestCase):
    def test_queue_observation_survives_response_budget_fallback(self):
        for length in (8, 16, 512):
            value = fixture()

            def flood(data):
                for key, item in data.items():
                    if isinstance(item, dict):
                        flood(item)
                    elif isinstance(item, str):
                        data[key] = ('\\"' * length)[:length]
                    elif type(item) is int:
                        data[key] = 2**63 - 1

            flood(value)
            value["home"] = '\\"' * 5000
            value["admin_dispatch_diagnostic"].update(queue_checked=False, reason="queue_not_checked")
            result = status_summary(value)
            with self.subTest(length=length):
                self.assertIs(result["admin_dispatch_diagnostic"]["queue_checked"], False)
                self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)

    def test_invalid_queue_observation_is_unknown(self):
        for checked in (None, "false", 0):
            with self.subTest(checked=checked):
                result = status_summary({"admin_dispatch_diagnostic": {"queue_checked": checked}})
                self.assertIsNone(result["admin_dispatch_diagnostic"]["queue_checked"])


if __name__ == "__main__":
    unittest.main()
