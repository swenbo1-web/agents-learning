"""Persistent ALP_HOME limits and finite administrator draining; no model calls."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import (_fingerprint, _review_configuration, admin_batch, admin_drain)
from agent_learning.admin_decisions import review_signature
from agent_learning.admin_lifecycle import claim_call, finish_call
from agent_learning.admin_usage import (admin_budget_config, admin_budget_policy, admin_budget_status,
                                        configure_admin_budget, configure_project_budget, project_budget_policy)
from agent_learning.runtime import ContractError, Runtime


class AdminBudgetSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-budget-settings-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def configure(self, **values):
        return configure_admin_budget(self.r, values, expected_version=admin_budget_policy(self.r)["version"],
                                      expected_epoch=self.r.session_epoch())

    def configure_project(self, **values):
        return configure_project_budget(self.r, self.p, values,
                                        expected_version=project_budget_policy(self.r, self.p)["version"],
                                        expected_epoch=self.r.session_epoch())

    def enable(self):
        self.r.config["admin"]["enabled"] = True

    def dispatch(self):
        token = "isolated-dispatch"
        self.r.db.execute("INSERT INTO admin_dispatch(project,token,host_epoch,lease_until,created) VALUES(?,?,?,?,?)",
                          (self.p, token, self.r.host_epoch(), time.time() + 30, time.time()))
        return token

    def test_generous_limits_preserve_disabled_defaults(self):
        cfg = admin_budget_config(self.r)
        self.assertEqual((cfg["max_calls_per_day"], cfg["max_calls_per_batch"], cfg["max_cases_per_batch"],
                          cfg["max_batch_seconds"], cfg["max_run_seconds"], cfg["max_concurrent_calls"]),
                         (100, 5, 40, 180, 600, 1))
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["auto_publish"])
        self.assertFalse(self.r.config["work_memory"]["summary_enabled"])
        self.assertEqual(admin_budget_policy(self.r)["version"], 0)

    def test_policy_persists_across_projects_hosts_and_runtime_connections(self):
        self.configure(max_calls_per_day=77, max_calls_per_batch=4, max_run_seconds=900)
        other = Runtime(self.home, config={"admin": {"max_calls_per_day": 2}}, host="antigravity")
        self.addCleanup(other.close)
        other.project(Path(self.temp.name) / "other")
        self.assertEqual(admin_budget_policy(other)["values"]["max_calls_per_day"], 77)
        self.assertEqual(admin_budget_config(other)["max_calls_per_batch"], 4)
        self.assertEqual(admin_budget_policy(other)["scope"], "ALP_HOME")
        self.assertEqual(other.config["admin"]["max_calls_per_day"], 2, "persistent overrides do not rewrite supplied config")

    def test_cas_and_same_policy_replay(self):
        first = self.configure(max_calls_per_day=80)
        self.assertEqual(first["version"], 1)
        self.assertTrue(first["changed"])
        repeat = self.configure(max_calls_per_day=80)
        self.assertFalse(repeat["changed"])
        self.assertEqual(repeat["version"], 1)
        with self.assertRaisesRegex(ContractError, "budget changed"):
            configure_admin_budget(self.r, {"max_calls_per_day": 99}, expected_version=0)
        self.assertEqual(admin_budget_config(self.r)["max_calls_per_day"], 80)

    def test_invalid_values_and_unrelated_configuration_never_persist(self):
        invalid = ({}, {"max_calls_per_day": True}, {"max_calls_per_day": -1}, {"max_calls_per_day": 1.0},
                   {"max_calls_per_batch": 0}, {"max_calls_per_batch": 21}, {"max_cases_per_batch": 101},
                   {"max_batch_seconds": 601}, {"max_run_seconds": 3601}, {"max_concurrent_calls": 0},
                   {"timeout_seconds": "120"}, {"max_reported_tokens_per_day": False},
                   {"token_reservation_per_call": 0}, {"max_input_chars": 1000}, {"enabled": True},
                   {"auto_publish": True}, {"model": "unknown"})
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ContractError):
                configure_admin_budget(self.r, values, expected_version=0)
        self.assertEqual(admin_budget_policy(self.r)["version"], 0)

    def test_old_session_epoch_cannot_update_global_policy(self):
        with self.assertRaises(ContractError):
            configure_admin_budget(self.r, {"max_calls_per_day": 3}, expected_version=0,
                                   expected_epoch=self.r.session_epoch() + 1)
        self.assertEqual(admin_budget_policy(self.r)["version"], 0)

    def test_raising_shared_limit_changes_real_claim_admission_without_resetting_usage(self):
        self.configure(max_calls_per_day=1)
        with self.r.transaction():
            first = claim_call(self.r, self.p, "case-one", 1, "evidence-one", self.r.host_epoch(), 10)
        finish_call(self.r, first["call_id"], "failed")
        other = self.r.project(Path(self.temp.name) / "other")
        with self.r.transaction():
            blocked = claim_call(self.r, other, "case-two", 1, "evidence-two", self.r.host_epoch(), 10)
        self.assertEqual(blocked["status"], "budget_exhausted")
        self.configure(max_calls_per_day=2)
        with self.r.transaction():
            admitted = claim_call(self.r, other, "case-two", 1, "evidence-two", self.r.host_epoch(), 10)
        self.assertEqual(admitted["status"], "reserved")
        self.assertEqual(admin_budget_status(self.r)["used_calls"], 2)
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls WHERE id=?", (first["call_id"],)).fetchone()[0], "failed")

    def test_lowering_budget_preserves_active_call_and_usage(self):
        self.configure(max_calls_per_day=3)
        with self.r.transaction():
            call = claim_call(self.r, self.p, "active", 1, "active", self.r.host_epoch(), 10)
        self.configure(max_calls_per_day=0)
        self.assertEqual(admin_budget_status(self.r)["admission_status"], "budget_exhausted")
        self.assertEqual(admin_budget_status(self.r)["used_calls"], 1)
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()[0], "reserved")

    def test_reported_token_limit_and_reservation_apply_at_claim(self):
        self.configure(max_reported_tokens_per_day=1000, token_reservation_per_call=1500)
        with self.r.transaction():
            call = claim_call(self.r, self.p, "tokens", 1, "tokens", self.r.host_epoch(), 10)
        self.assertEqual(call["status"], "token_budget_exhausted")
        self.configure(max_reported_tokens_per_day=None)
        with self.r.transaction():
            call = claim_call(self.r, self.p, "tokens", 1, "tokens", self.r.host_epoch(), 10)
        self.assertEqual(call["status"], "reserved")
        self.assertEqual(admin_budget_status(self.r)["token_reservations"], 1500)

    def test_budget_changes_do_not_change_paid_review_signature(self):
        packet = {"problem": {"id": "case", "revision": 1, "summary": "review", "category": "project"},
                  "events": [], "task": None}
        before = review_signature(packet, _review_configuration(self.r.admin_effective_config(self.p), {}), _fingerprint(packet))
        self.configure(max_calls_per_day=200, max_calls_per_batch=2, max_cases_per_batch=10,
                       max_batch_seconds=20, max_run_seconds=30, timeout_seconds=10,
                       token_reservation_per_call=50, max_concurrent_calls=2, max_reported_tokens_per_day=10000)
        after = review_signature(packet, _review_configuration(self.r.admin_effective_config(self.p), {}), _fingerprint(packet))
        self.assertEqual(before, after)

    def test_batch_stops_at_updated_persistent_call_limit(self):
        self.enable()
        self.configure_project(max_calls_per_batch=5)

        def reviewed(*args, **kwargs):
            self.configure_project(max_calls_per_batch=1)
            return {"status": "skip", "call_id": "fake-only", "usage": {}, "scanned_count": 1}

        with patch("agent_learning.admin.admin_once", side_effect=reviewed) as review:
            result = admin_batch(self.r, self.p)
        self.assertEqual(review.call_count, 1)
        self.assertEqual(result["stop_reason"], "call_limit")
        self.assertEqual(result["invocations"], 1)

    def test_unknown_at_call_limit_retains_unknown_stop_reason(self):
        self.enable()
        self.configure_project(max_calls_per_batch=1)
        with patch("agent_learning.admin.admin_once", return_value={
                "status": "timeout_unknown", "call_id": "fake-only", "usage": {}, "scanned_count": 1}):
            result = admin_batch(self.r, self.p)
        self.assertEqual(result["stop_reason"], "timeout_unknown")

    def test_drain_keeps_one_lease_across_batches_and_releases_it(self):
        self.enable()
        token = self.dispatch()
        seen = []

        def batch(*args, **kwargs):
            row = self.r.db.execute("SELECT lease_until FROM admin_dispatch WHERE token=?", (token,)).fetchone()
            self.assertIsNotNone(row)
            self.assertGreater(row[0], time.time() + 500)
            self.assertFalse(kwargs["_release_dispatch"])
            seen.append(kwargs["dispatch_token"])
            return {"status": "batch_complete", "stop_reason": "call_limit" if len(seen) == 1 else "idle",
                    "invocations": 5 if len(seen) == 1 else 0, "scanned_count": 5 if len(seen) == 1 else 0}

        with patch("agent_learning.admin.admin_batch", side_effect=batch):
            result = admin_drain(self.r, self.p, dispatch_token=token)
        self.assertEqual(seen, [token, token])
        self.assertEqual((result["batches"], result["invocations"], result["stop_reason"]), (2, 5, "idle"))
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch").fetchone())

    def test_drain_rereads_enabled_state_between_batches(self):
        self.enable()
        token = self.dispatch()

        def batch(*args, **kwargs):
            self.r.config["admin"]["enabled"] = False
            return {"status": "batch_complete", "stop_reason": "call_limit", "invocations": 5, "scanned_count": 5}

        with patch("agent_learning.admin.admin_batch", side_effect=batch) as invoke:
            result = admin_drain(self.r, self.p, dispatch_token=token)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result["stop_reason"], "disabled")
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch").fetchone())

    def test_drain_does_not_continue_failed_or_unknown_calls(self):
        self.enable()
        for status in ("failed", "timeout_unknown", "review_required", "needs_evidence", "budget_exhausted"):
            with self.subTest(status=status):
                token = self.dispatch()
                with patch("agent_learning.admin.admin_batch", return_value={"status": "batch_complete",
                           "stop_reason": status, "invocations": 1, "scanned_count": 1}) as invoke:
                    result = admin_drain(self.r, self.p, dispatch_token=token)
                self.assertEqual(invoke.call_count, 1)
                self.assertEqual(result["stop_reason"], status)
                self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch").fetchone())

    def test_drain_invalid_allowance_does_not_release_an_unclaimed_dispatch(self):
        self.enable()
        token = self.dispatch()
        with self.assertRaises(ContractError):
            admin_drain(self.r, self.p, dispatch_token=token, max_seconds=float("inf"))
        self.assertEqual(self.r.db.execute("SELECT token FROM admin_dispatch").fetchone()[0], token)


if __name__ == "__main__":
    unittest.main()
