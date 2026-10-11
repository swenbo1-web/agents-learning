"""第六模块真实隔离 SQLite：调用租约、启动边界和显式再审，不启动模型。"""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest

from agent_learning.admin_lifecycle import (
    ACTIVE_STATES, claim_call, finish_call, install_lifecycle, mark_launch_intent,
    mark_started, recover_calls, retry_call, validate_call,
)
from agent_learning.runtime import ContractError, Runtime


class Module6LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module6-lifecycle-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(max_calls_per_day=5)
        with self.r.transaction():
            install_lifecycle(self.r)

    def claim(self, project=None, problem="problem-a", revision=1):
        with self.r.transaction():
            return claim_call(self.r, project or self.p, problem, revision, "fingerprint:" + problem,
                              self.r.host_epoch(), 120)

    def row(self, call):
        return dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone())

    def expire(self, call):
        self.r.db.execute("UPDATE admin_calls SET lease_until=? WHERE id=?", (time.time() - 1, call["call_id"]))

    def launch(self, call):
        return mark_launch_intent(self.r, call["call_id"], call["owner"], call["host_epoch"])

    def start(self, call):
        self.launch(call)
        return mark_started(self.r, call["call_id"], call["owner"], 12345)

    def test_claim_requires_existing_write_transaction(self):
        with self.assertRaisesRegex(ContractError, "existing write transaction"):
            claim_call(self.r, self.p, "case", 1, "sig", self.r.host_epoch(), 120)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_migration_idempotent_preserves_terminal_history(self):
        call = self.claim()
        finish_call(self.r, call["call_id"], "not_started")
        with self.r.transaction():
            install_lifecycle(self.r)
            install_lifecycle(self.r)
        self.assertEqual(self.row(call)["state"], "not_started")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_claim_stores_owner_epoch_and_timeout_margin(self):
        before = time.time()
        call = self.claim()
        row = self.row(call)
        self.assertEqual(ACTIVE_STATES, ("reserved", "launch_intent", "started"))
        self.assertEqual(call["status"], "reserved")
        self.assertTrue(row["owner"])
        self.assertIsNone(row["launch_intent"])
        self.assertGreaterEqual(row["lease_until"], before + 150)

    def test_same_project_has_one_active_slot(self):
        first = self.claim()
        second = self.claim(problem="problem-b")
        self.assertEqual(second["status"], "waiting_for_review")
        self.assertEqual(second["call_id"], first["call_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_started_call_holds_project_slot(self):
        first = self.claim()
        self.start(first)
        self.assertEqual(self.claim(problem="problem-b")["active_state"], "started")

    def test_projects_share_daily_budget(self):
        self.r.config["admin"]["max_calls_per_day"] = 1
        self.claim()
        other = self.r.project(Path(self.temp.name) / "other")
        self.assertEqual(self.claim(other, "other-case")["status"], "budget_exhausted")

    def test_global_budget_is_atomic_across_connections(self):
        other = self.r.project(Path(self.temp.name) / "other")
        barrier = threading.Barrier(2)
        outcomes, failures = [], []

        def worker(project):
            runtime = Runtime(self.home)
            try:
                runtime.config["admin"]["max_calls_per_day"] = 1
                barrier.wait(timeout=5)
                with runtime.transaction():
                    outcomes.append(claim_call(runtime, project, project + "-case", 1, project,
                                               runtime.host_epoch(), 120)["status"])
            except Exception as exc:
                failures.append(repr(exc))
            finally:
                runtime.close()

        threads = [threading.Thread(target=worker, args=(project,)) for project in (self.p, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertCountEqual(outcomes, ["reserved", "budget_exhausted"])

    def test_expired_new_reserved_has_safe_not_started_recovery(self):
        call = self.claim()
        self.expire(call)
        recovered = recover_calls(self.r, self.p)
        self.assertEqual(recovered["recovered"][0]["state"], "not_started")
        self.assertIsNone(self.row(call)["lease_until"])
        self.assertEqual(self.claim()["status"], "reserved")

    def test_expired_launch_intent_is_unknown_and_keeps_budget(self):
        self.r.config["admin"]["max_calls_per_day"] = 1
        call = self.claim()
        self.launch(call)
        self.expire(call)
        self.assertEqual(recover_calls(self.r)["recovered"][0]["state"], "orphaned_unknown")
        self.assertEqual(self.claim(problem="another")["status"], "budget_exhausted")

    def test_expired_started_is_unknown_not_free(self):
        call = self.claim()
        self.start(call)
        self.expire(call)
        self.assertEqual(recover_calls(self.r)["count"], 1)
        self.assertEqual(self.row(call)["state"], "orphaned_unknown")
        self.assertEqual(self.row(call)["pid"], 12345)

    def test_recovery_project_filter_and_now(self):
        first = self.claim()
        other = self.r.project(Path(self.temp.name) / "other")
        second = self.claim(other, "other-case")
        recover_calls(self.r, self.p, now=first["lease_until"] + 1)
        self.assertEqual(self.row(first)["state"], "not_started")
        self.assertEqual(self.row(second)["state"], "reserved")

    def test_legacy_active_record_never_assumed_not_started(self):
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created) "
                          "VALUES('legacy',?,'old',1,'2000-01-01','reserved',1)", (self.p,))
        with self.r.transaction():
            install_lifecycle(self.r)
        legacy = self.r.db.execute("SELECT * FROM admin_calls WHERE id='legacy'").fetchone()
        self.assertEqual(legacy["lease_until"], 0)
        self.assertEqual(legacy["launch_intent"], 1)
        recover_calls(self.r)
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls WHERE id='legacy'").fetchone()[0],
                         "orphaned_unknown")

    def test_launch_intent_is_durable_before_started(self):
        call = self.claim()
        self.launch(call)
        other = Runtime(self.home)
        try:
            row = other.db.execute("SELECT state,launch_intent,started FROM admin_calls WHERE id=?",
                                   (call["call_id"],)).fetchone()
            self.assertEqual(row["state"], "launch_intent")
            self.assertIsNotNone(row["launch_intent"])
            self.assertIsNone(row["started"])
        finally:
            other.close()

    def test_launch_owner_cas_and_double_launch(self):
        call = self.claim()
        with self.assertRaisesRegex(ContractError, "ownership"):
            mark_launch_intent(self.r, call["call_id"], "another-owner", call["host_epoch"])
        self.assertEqual(self.row(call)["state"], "reserved")
        self.launch(call)
        with self.assertRaisesRegex(ContractError, "already launched"):
            self.launch(call)

    def test_started_requires_launch_intent_and_valid_pid(self):
        call = self.claim()
        with self.assertRaisesRegex(ContractError, "intent required"):
            mark_started(self.r, call["call_id"], call["owner"])
        self.launch(call)
        with self.assertRaisesRegex(ContractError, "process identity"):
            mark_started(self.r, call["call_id"], call["owner"], True)
        row = mark_started(self.r, call["call_id"], call["owner"], 321)
        self.assertEqual(row["state"], "started")
        self.assertEqual(row["pid"], 321)

    def test_expired_owner_cannot_accept_response(self):
        call = self.claim()
        self.start(call)
        self.expire(call)
        with self.assertRaisesRegex(ContractError, "lease changed"):
            validate_call(self.r, call["call_id"], call["owner"], call["host_epoch"])
        recover_calls(self.r)
        self.assertEqual(finish_call(self.r, call["call_id"], "candidate")["state"], "orphaned_unknown")

    def test_host_change_rejects_launch_before_process(self):
        call = self.claim()
        self.r.account_changed("switch-before-launch")
        with self.assertRaises(ContractError):
            self.launch(call)
        row = finish_call(self.r, call["call_id"], "not_started")
        self.assertEqual(row["state"], "not_started")

    def test_host_change_preserves_unknown_but_adds_true_usage(self):
        call = self.claim()
        self.start(call)
        self.r.account_changed("switch-after-launch")
        row = finish_call(self.r, call["call_id"], "skip", usage={"token_usage": [{"input_tokens": 123}]})
        self.assertEqual(row["state"], "account_changed_unknown")
        self.assertEqual(json.loads(row["usage"])["token_usage"][0]["input_tokens"], 123)
        self.assertIsNone(row["lease_until"])

    def test_finish_releases_slot_and_preserves_terminal_result(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "skip", usage={"elapsed_seconds": 1.2})
        self.assertEqual(finish_call(self.r, call["call_id"], "failed")["state"], "skip")
        self.assertEqual(self.claim(problem="next")["status"], "reserved")

    def test_retry_preserves_original_unknown_and_daily_count(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "timeout_unknown")
        receipt = retry_call(self.r, self.p, call["call_id"], "User requested another review after timeout")
        self.assertEqual(receipt["status"], "retry_allowed")
        self.assertEqual(self.row(call)["state"], "timeout_unknown")
        self.assertEqual(self.row(call)["retry_allowed"], 1)
        self.assertTrue(self.row(call)["retry_reason"])
        self.r.config["admin"]["max_calls_per_day"] = 1
        self.assertEqual(self.claim()["status"], "budget_exhausted")
        self.assertEqual(self.row(call)["retry_allowed"], 1)

    def test_retry_authorization_is_consumed_once(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "failed")
        retry_call(self.r, self.p, call["call_id"], "Explicit retry")
        second = self.claim()
        self.assertEqual(second["status"], "reserved")
        self.assertEqual(self.row(call)["retry_allowed"], 2)
        self.assertEqual(retry_call(self.r, self.p, call["call_id"], "Repeat old request")["status"], "retry_consumed")
        self.assertEqual(self.row(call)["retry_allowed"], 2)

    def test_retry_rejects_wrong_project_empty_reason_and_success(self):
        call = self.claim()
        with self.assertRaises(ContractError):
            retry_call(self.r, self.p, call["call_id"], " ")
        with self.assertRaises(ContractError):
            retry_call(self.r, self.p, call["call_id"], "Active calls cannot retry")
        finish_call(self.r, call["call_id"], "failed")
        with self.assertRaises(ContractError):
            retry_call(self.r, "other-project", call["call_id"], "Retry wrong project")
        second = self.claim(problem="other")
        self.start(second)
        finish_call(self.r, second["call_id"], "skip")
        with self.assertRaises(ContractError):
            retry_call(self.r, self.p, second["call_id"], "No retry for a successful decision")

    def test_claim_host_epoch_and_timeout_validation(self):
        with self.r.transaction():
            with self.assertRaises(ContractError):
                claim_call(self.r, self.p, "case", 1, "fp", self.r.host_epoch() + 1, 120)
            with self.assertRaises(ContractError):
                claim_call(self.r, self.p, "case", 1, "fp", self.r.host_epoch(), True)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
