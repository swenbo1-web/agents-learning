"""第八模块准入、计量和决定提交边界；临时 SQLite 与内存执行器。"""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import _usage, admin_batch, admin_once
from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent, mark_started, recover_calls
from agent_learning.admin_queue import claim_dispatch
from agent_learning.admin_usage import admin_budget_status, install_budget
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class Module8BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module8-budget-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=10,
                                       max_concurrent_calls=1, token_reservation_per_call=4000,
                                       max_reported_tokens_per_day=None)
        with self.r.transaction():
            install_budget(self.r)

    def claim(self, project=None, problem="budgetcase"):
        with self.r.transaction():
            return claim_call(self.r, project or self.p, problem, 1, problem, self.r.host_epoch(), 10)

    def start(self, call):
        mark_launch_intent(self.r, call["call_id"], call["owner"], call["host_epoch"])
        mark_started(self.r, call["call_id"], call["owner"])

    def expire(self, call):
        self.r.db.execute("UPDATE admin_calls SET lease_until=0 WHERE id=?", (call["call_id"],))

    def complete_usage(self, input_tokens=100, output_tokens=20):
        return {"token_usage": [{"input_tokens": input_tokens, "output_tokens": output_tokens}]}

    def status(self):
        return admin_budget_status(self.r)

    def problem(self, summary="budgetfailure"):
        self.r.task_update(self.p, "chat", "budgetinvariant")
        return self.r.feedback(self.p, "chat", summary, category="operation")

    def test_cross_project_safe_recovery_before_global_daily_admission(self):
        self.r.config["admin"]["max_calls_per_day"] = 1
        call = self.claim()
        self.expire(call)
        other = self.r.project(Path(self.temp.name) / "other")
        next_call = self.claim(other, "other")
        self.assertEqual(next_call["status"], "reserved")
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()[0], "not_started")
        self.assertEqual(self.status()["used_calls"], 1)

    def test_cross_project_unknown_recovery_keeps_daily_charge(self):
        self.r.config["admin"]["max_calls_per_day"] = 1
        call = self.claim()
        self.start(call)
        self.expire(call)
        other = self.r.project(Path(self.temp.name) / "other")
        self.assertEqual(self.claim(other)["status"], "budget_exhausted")
        self.assertEqual(self.status()["token_reservations"], 4000)

    def test_global_concurrent_slot_across_projects(self):
        self.claim()
        other = self.r.project(Path(self.temp.name) / "other")
        self.assertEqual(self.claim(other)["status"], "concurrency_limited")

    def test_global_slot_is_atomic_across_connections(self):
        other = self.r.project(Path(self.temp.name) / "other")
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker(project):
            r = Runtime(self.home)
            try:
                r.config["admin"].update(max_calls_per_day=10, max_concurrent_calls=1)
                barrier.wait(5)
                with r.transaction():
                    results.append(claim_call(r, project, "case", 1, "sig", r.host_epoch(), 10)["status"])
            except Exception as exc:
                errors.append(str(exc))
            finally:
                r.close()

        threads = [threading.Thread(target=worker, args=(p,)) for p in (self.p, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ["reserved", "concurrency_limited"])

    def test_reported_token_budget_requires_room_for_reservation(self):
        self.r.config["admin"]["max_reported_tokens_per_day"] = 3999
        self.assertEqual(self.claim()["status"], "token_budget_exhausted")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_complete_usage_settles_actual_total(self):
        self.r.config["admin"]["max_reported_tokens_per_day"] = 4200
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "skip", self.complete_usage())
        status = self.status()
        self.assertEqual(status["reported_tokens"], 120)
        self.assertEqual(status["token_reservations"], 0)
        self.assertEqual(status["accounted_tokens"], 120)
        self.assertEqual(status["usage_states"]["complete"], 1)
        self.assertEqual(self.claim(problem="next")["status"], "reserved")

    def test_unknown_usage_holds_reservation_after_timeout(self):
        self.r.config["admin"]["max_reported_tokens_per_day"] = 4000
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "timeout_unknown", {"token_usage": []})
        self.assertEqual(self.status()["accounted_tokens"], 4000)
        self.assertEqual(self.status()["usage_states"]["unknown"], 1)
        self.assertEqual(self.claim(problem="next")["status"], "token_budget_exhausted")

    def test_partial_overage_is_visible_and_not_clamped_to_reservation(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "failed", {"token_usage": [{"input_tokens": 5000}]})
        status = self.status()
        self.assertEqual(status["reported_tokens"], 5000)
        self.assertEqual(status["accounted_tokens"], 5000)
        self.assertEqual(status["token_reservations"], 4000)
        self.assertEqual(status["usage_states"]["partial"], 1)

    def test_not_started_releases_calls_and_token_reservation(self):
        self.r.config["admin"]["max_calls_per_day"] = 1
        call = self.claim()
        finish_call(self.r, call["call_id"], "not_started")
        self.assertEqual(self.status()["used_calls"], 0)
        self.assertEqual(self.status()["accounted_tokens"], 0)
        self.assertEqual(self.claim()["status"], "reserved")

    def test_usage_observation_replay_does_not_double_charge(self):
        call = self.claim()
        self.start(call)
        usage = self.complete_usage()
        finish_call(self.r, call["call_id"], "skip", usage)
        finish_call(self.r, call["call_id"], "skip", usage)
        self.assertEqual(self.status()["reported_tokens"], 120)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 1)

    def test_empty_observation_cannot_erase_existing_numbers(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "skip", self.complete_usage())
        row = finish_call(self.r, call["call_id"], "failed", {"token_usage": []})
        usage = json.loads(row["usage"])
        self.assertEqual(usage["token_totals"], {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(self.status()["reported_tokens"], 120)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 2)

    def test_conflicting_complete_observation_preserves_both_and_reservation(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "skip", self.complete_usage())
        finish_call(self.r, call["call_id"], "skip", self.complete_usage(30, 2))
        status = self.status()
        self.assertEqual(status["reported_tokens"], 120)
        self.assertEqual(status["accounted_tokens"], 4000)
        self.assertEqual(status["usage_states"]["conflict"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 2)

        replay = finish_call(self.r, call["call_id"], "skip", self.complete_usage(30, 2))
        self.assertEqual(json.loads(replay["usage"])["token_totals"], {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 2)
        self.assertEqual(self.status()["reported_tokens"], 120)

    def test_complete_zero_usage_can_settle_zero(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "skip", self.complete_usage(0, 0))
        self.assertEqual(self.status()["accounted_tokens"], 0)
        self.assertEqual(self.status()["usage_states"]["complete"], 1)

    def test_merged_missing_total_conflict_keeps_reservation_through_later_valid_observation(self):
        self.r.config["admin"]["max_reported_tokens_per_day"] = 5000
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "failed", {"token_totals": {"total_tokens": 1000}, "completeness": "partial"})
        row = finish_call(self.r, call["call_id"], "failed", {"token_totals": {"input_tokens": 100, "output_tokens": 50},
            "completeness": "complete"})
        merged = json.loads(row["usage"])
        self.assertEqual(merged["completeness"], "partial")
        self.assertTrue(merged["conflict"])
        self.assertEqual(self.status()["accounted_tokens"], 4000)
        self.assertEqual(self.claim(problem="blocked")["status"], "token_budget_exhausted")
        valid = {"token_totals": {"input_tokens": 950, "output_tokens": 50, "total_tokens": 1000}, "completeness": "complete"}
        for _ in range(2):
            row = finish_call(self.r, call["call_id"], "failed", valid)
        self.assertTrue(json.loads(row["usage"])["conflict"])
        self.assertEqual(self.status()["accounted_tokens"], 4000)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 3)

    def test_consistent_completion_of_partial_total_can_settle_original_reservation(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "failed", {"token_totals": {"total_tokens": 1000}, "completeness": "partial"})
        row = finish_call(self.r, call["call_id"], "failed", {"token_totals": {
            "input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}, "completeness": "complete"})
        self.assertFalse(json.loads(row["usage"])["conflict"])
        self.assertEqual(self.status()["accounted_tokens"], 1000)
        self.assertEqual(self.status()["token_reservations"], 0)

    def test_conflicting_disjoint_overage_preserves_both_visible_fields(self):
        call = self.claim()
        self.start(call)
        finish_call(self.r, call["call_id"], "failed", {"token_usage": [{"input_tokens": 5000}]})
        row = finish_call(self.r, call["call_id"], "failed", {"token_usage": [{"output_tokens": 6000}]})
        self.assertEqual(json.loads(row["usage"])["token_totals"], {"input_tokens": 5000, "output_tokens": 6000})
        self.assertEqual(self.status()["accounted_tokens"], 11000)
        self.assertEqual(self.status()["usage_states"]["partial"], 1)

    def test_utc_day_and_next_reset(self):
        status = admin_budget_status(self.r, now=172799)
        self.assertEqual(status["utc_day"], "1970-01-02")
        self.assertEqual(status["next_reset_at"], 172800)

    def test_budget_install_idempotent_and_migrates_old_usage(self):
        call = self.claim()
        finish_call(self.r, call["call_id"], "skip", self.complete_usage())
        self.r.db.execute("DELETE FROM admin_call_budgets")
        self.r.db.execute("DELETE FROM admin_usage_observations")
        with self.r.transaction():
            install_budget(self.r)
            install_budget(self.r)
        self.assertEqual(self.status()["reported_tokens"], 120)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_call_budgets").fetchone()[0], 1)

    def test_invalid_budget_contract_rejected(self):
        for key, value in (("max_concurrent_calls", 0), ("max_concurrent_calls", True),
                           ("token_reservation_per_call", -1), ("max_reported_tokens_per_day", True)):
            with self.subTest(key=key, value=value):
                old = self.r.config["admin"][key]
                self.r.config["admin"][key] = value
                with self.assertRaises(ContractError):
                    self.claim()
                self.r.config["admin"][key] = old

    def test_full_log_accumulation_survives_preview_limit(self):
        log = Path(self.temp.name) / "usage.log"
        # Distinct turns test preview limits; anonymous repeats remain ambiguous.
        log.write_text("\n".join(json.dumps({"type": "turn.completed", "turn_id": f"turn-{index}", "usage": {
            "input_tokens": 100, "output_tokens": 10}}) for index in range(20)), encoding="utf-8")
        usage = _usage(log, 1.0)
        self.assertEqual(usage["token_totals"], {"input_tokens": 2000, "output_tokens": 200})
        self.assertEqual(len(usage["token_usage"]), 16)
        self.assertTrue(usage["preview_truncated"])
        self.assertFalse(usage["log_tail_truncated"])
        self.assertEqual(usage["completeness"], "complete")

    def test_usage_scans_prefix_instead_of_dropping_first_two_megabytes(self):
        log = Path(self.temp.name) / "prefix.log"
        report = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 2}})
        log.write_text(report + "\n" + "unrelated diagnostic\n" * 110000, encoding="utf-8")
        self.assertEqual(_usage(log, 0)["token_totals"]["input_tokens"], 7)

    def test_same_turn_duplicate_not_charged_twice(self):
        log = Path(self.temp.name) / "duplicates.log"
        report = json.dumps({"type": "turn.completed", "turn_id": "turn-a", "usage": {
            "input_tokens": 7, "output_tokens": 2}})
        log.write_text(report + "\n" + report + "\n", encoding="utf-8")
        self.assertEqual(_usage(log, 0)["token_totals"], {"input_tokens": 7, "output_tokens": 2})

    def test_same_turn_conflicting_report_is_partial(self):
        log = Path(self.temp.name) / "conflicts.log"
        log.write_text("\n".join(json.dumps({"type": "turn.completed", "turn_id": "a", "usage": {
            "input_tokens": n, "output_tokens": 2}}) for n in (7, 9)), encoding="utf-8")
        usage = _usage(log, 0)
        self.assertEqual(usage["token_totals"], {"input_tokens": 9, "output_tokens": 2})
        self.assertEqual(usage["completeness"], "partial")

    def test_bad_and_missing_usage_are_not_complete_zero(self):
        log = Path(self.temp.name) / "partial.log"
        log.write_text(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": True, "output_tokens": 3}}), encoding="utf-8")
        self.assertEqual(_usage(log, 0)["completeness"], "partial")
        log.write_text(json.dumps({"type": "turn.completed"}), encoding="utf-8")
        self.assertEqual(_usage(log, 0)["completeness"], "unknown")

    def test_response_expiring_after_candidate_insert_rolls_back(self):
        self.problem()
        original = self.r.submit_candidate

        def expire_after_insert(*args, **kwargs):
            result = original(*args, **kwargs)
            self.r.db.execute("UPDATE admin_calls SET lease_until=0 WHERE state='started'")
            return result

        with patch.object(self.r, "submit_candidate", side_effect=expire_after_insert), patch(
                "agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p)
        # 数据库修改也随事务回滚，因此异常路径保存failed；不得暴露candidate成功。
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE decision='candidate'").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0], 0)

    def test_response_crossing_clock_lease_returns_real_unknown(self):
        self.problem()
        clock = {"value": time.time()}
        original = self.r.submit_candidate

        def advance_after_insert(*args, **kwargs):
            result = original(*args, **kwargs)
            lease = self.r.db.execute("SELECT lease_until FROM admin_calls WHERE state='started'").fetchone()[0]
            clock["value"] = lease + 1
            return result

        with patch("agent_learning.admin_lifecycle.time.time", side_effect=lambda: clock["value"]), patch.object(
                self.r, "submit_candidate", side_effect=advance_after_insert), patch(
                "agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "orphaned_unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE decision='candidate'").fetchone()[0], 0)

    def test_scheduler_deadline_shortens_batch(self):
        self.problem()
        with patch("agent_learning.admin.time.monotonic", return_value=100.0), patch(
                "agent_learning.admin.subprocess.Popen") as model:
            result = admin_batch(self.r, self.p, _deadline=100.5)
        model.assert_not_called()
        self.assertEqual(result["stop_reason"], "batch_time_exhausted")

    def test_lease_crossing_between_final_validation_and_finish_rolls_back(self):
        self.problem()
        clock = {"value": time.time()}
        from agent_learning.admin_lifecycle import finish_call as original_finish

        def expire_before_finish(runtime, call_id, state, **kwargs):
            if state == "candidate":
                clock["value"] = runtime.db.execute("SELECT lease_until FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0] + 1
            return original_finish(runtime, call_id, state, **kwargs)

        with patch("agent_learning.admin_lifecycle.time.time", side_effect=lambda: clock["value"]), patch(
                "agent_learning.admin_lifecycle.finish_call", side_effect=expire_before_finish), patch(
                "agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "orphaned_unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE decision='candidate'").fetchone()[0], 0)

    def test_batch_stops_with_explicit_token_reason(self):
        self.problem()
        self.r.config["admin"]["max_reported_tokens_per_day"] = 100
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_batch(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(result["stop_reason"], "token_budget_exhausted")
        self.assertEqual(result["invocations"], 0)

    def test_dispatch_uses_global_concurrency_before_spawn(self):
        self.problem()
        other = self.r.project(Path(self.temp.name) / "other")
        self.claim(other)
        self.assertIsNone(claim_dispatch(self.r, self.p))

    def test_dispatch_tokens_also_bound_background_slots(self):
        self.problem()
        other_path = Path(self.temp.name) / "other"
        other = self.r.project(other_path)
        self.r.task_update(other, "other-chat", "otherbudget")
        self.r.feedback(other, "other-chat", "otherfailure", category="operation")
        self.assertTrue(claim_dispatch(self.r, self.p))
        self.assertIsNone(claim_dispatch(self.r, other))

    def test_active_dispatch_and_its_call_do_not_double_count_global_slots(self):
        self.r.config["admin"]["max_concurrent_calls"] = 2
        self.problem()
        other_path = Path(self.temp.name) / "other"
        other = self.r.project(other_path)
        self.r.task_update(other, "other-chat", "otherbudget")
        self.r.feedback(other, "other-chat", "otherfailure", category="operation")
        self.assertTrue(claim_dispatch(self.r, self.p))
        self.assertEqual(self.claim()["status"], "reserved")
        self.assertTrue(claim_dispatch(self.r, other))


if __name__ == "__main__":
    unittest.main()
