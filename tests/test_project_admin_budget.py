"""Project ledger attribution, two-scope admission and explicit control compatibility."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint, _review_configuration
from agent_learning.admin_control import apply_control, control_status
from agent_learning.admin_decisions import review_signature
from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent, mark_started
from agent_learning.admin_usage import (
    PROJECT_BUDGET_FIELDS, admin_admission_status, admin_budget_policy, admin_budget_status,
    admin_project_budget_status, configure_admin_budget, configure_project_budget, install_budget,
    project_budget_policy,
)
from agent_learning.runtime import ContractError, Runtime


class ProjectAdminBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-project-budget-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "project-a"
        self.workspace.mkdir()
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.a = self.r.project(self.workspace)
        self.b = self.r.project(self.root / "project-b")

    def global_limit(self, **values):
        return configure_admin_budget(self.r, values, expected_version=admin_budget_policy(self.r)["version"])

    def project_limit(self, project=None, **values):
        project = project or self.a
        return configure_project_budget(self.r, project, values,
                                        expected_version=project_budget_policy(self.r, project)["version"])

    def claim(self, project=None, problem="case"):
        with self.r.transaction():
            return claim_call(self.r, project or self.a, problem, 1, problem, self.r.host_epoch(), 30)

    def started(self, project=None, problem="case"):
        call = self.claim(project, problem)
        self.assertEqual(call["status"], "reserved", call)
        mark_launch_intent(self.r, call["call_id"], call["owner"], call["host_epoch"])
        mark_started(self.r, call["call_id"], call["owner"])
        return call

    def receipt(self, call_id, *, duplicate_of=None):
        row = self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone()
        self.r.db.execute(
            "INSERT INTO review_receipts(project,problem,revision,review_signature,evidence_signature,call_id,"
            "decision,reason,response,created,dependency_signature,dependencies,duplicate_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["project"], row["problem"], row["revision"], "receipt-" + call_id, "evidence", call_id,
             row["state"], "fixture", "{}", time.time(), "deps", "{}", duplicate_of))

    def body(self, project=None, **updates):
        state = control_status(self.r, project or self.a)
        body = {"request_id": "project-control", "expected_policy_version": state["policy"]["policy_version"],
                "expected_project_budget_version": state["project_budget_policy"]["version"],
                "expected_settings_version": state["settings_version"], "expected_session_epoch": state["session_epoch"],
                "expected_governance_epoch": state["governance_epoch"], "enabled": True,
                "model_mode": "default", "model": "", "project_limits": {"max_calls_per_day": 12}, "start": False}
        body.update(updates)
        return body

    def test_project_defaults_do_not_copy_persistent_global_overrides(self):
        self.global_limit(max_calls_per_day=300, max_calls_per_batch=10, max_run_seconds=1200)
        before = self.r.db.total_changes
        policy = project_budget_policy(self.r, self.a)
        self.assertEqual((policy["scope"], policy["project_id"], policy["version"], policy["configured"]),
                         ("project", self.a, 0, False))
        self.assertEqual(set(policy["values"]), PROJECT_BUDGET_FIELDS)
        self.assertEqual((policy["values"]["max_calls_per_day"], policy["values"]["max_calls_per_batch"],
                          policy["values"]["max_run_seconds"]), (100, 5, 600))
        self.assertEqual(self.r.db.total_changes, before)

    def test_project_cas_persists_across_hosts_and_does_not_change_other_projects(self):
        policy = self.project_limit(max_calls_per_day=7, max_calls_per_batch=2)
        self.assertEqual(policy["version"], 1)
        self.assertFalse(self.project_limit(max_calls_per_day=7)["changed"])
        with self.assertRaisesRegex(ContractError, "project budget changed"):
            configure_project_budget(self.r, self.a, {"max_calls_per_day": 9}, expected_version=0)
        other = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        self.assertEqual(project_budget_policy(other, self.a)["values"]["max_calls_per_day"], 7)
        self.assertEqual(other.admin_effective_config(self.a)["max_calls_per_batch"], 2)
        self.assertEqual(project_budget_policy(other, self.b)["version"], 0)
        self.assertEqual(admin_budget_policy(other)["version"], 0)

    def test_shared_batch_tuning_does_not_change_project_processing_limits(self):
        self.global_limit(max_calls_per_batch=10, max_run_seconds=1200)
        self.assertEqual(admin_budget_policy(self.r)["values"]["max_calls_per_batch"], 10)
        self.assertEqual(self.r.admin_effective_config(self.a)["max_calls_per_batch"], 5)
        self.project_limit(max_calls_per_batch=2, max_run_seconds=90)
        self.global_limit(max_calls_per_batch=15, max_run_seconds=1800)
        self.assertEqual((self.r.admin_effective_config(self.a)["max_calls_per_batch"],
                          self.r.admin_effective_config(self.a)["max_run_seconds"]), (2, 90))
        self.assertEqual(self.r.admin_effective_config(self.b)["max_calls_per_batch"], 5)

    def test_project_call_count_is_shared_across_codex_antigravity_and_dsh(self):
        self.project_limit(max_calls_per_day=2)
        first = self.started(problem="codex-case")
        finish_call(self.r, first["call_id"], "failed")
        for host in ("antigravity", "dsh"):
            other = Runtime(self.home, config={}, host=host)
            self.addCleanup(other.close)
            if host == "antigravity":
                with other.transaction():
                    second = claim_call(other, self.a, "antigravity-case", 1, "ag", other.host_epoch(), 30)
                self.assertEqual(second["status"], "reserved")
                finish_call(other, second["call_id"], "failed")
            else:
                with other.transaction():
                    blocked = claim_call(other, self.a, "dsh-case", 1, "dsh", other.host_epoch(), 30)
                self.assertEqual((blocked["status"], blocked["blocking_scope"]), ("budget_exhausted", "project"))
            self.assertEqual(admin_project_budget_status(other, self.a)["used_calls"], 2)
        self.assertEqual(admin_budget_status(self.r)["used_calls"], 2)

    def test_invalid_project_budget_and_unknown_project_never_write(self):
        invalid = ({}, {"max_calls_per_day": True}, {"max_calls_per_day": -1},
                   {"max_calls_per_day": 100001}, {"max_calls_per_batch": 0},
                   {"max_reported_tokens_per_day": False}, {"max_concurrent_calls": 2},
                   {"token_reservation_per_call": 10}, {"enabled": True})
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ContractError):
                configure_project_budget(self.r, self.a, values, expected_version=0)
        with self.assertRaisesRegex(ContractError, "unknown"):
            configure_project_budget(self.r, "missing", {"max_calls_per_day": 1}, expected_version=0)
        with self.assertRaises(ContractError):
            configure_project_budget(self.r, self.a, {"max_calls_per_day": 1}, expected_version=0,
                                     expected_epoch=self.r.session_epoch() + 1)
        self.assertEqual(project_budget_policy(self.r, self.a)["version"], 0)

    def test_project_used_calls_never_include_another_projects_attempts(self):
        call = self.started()
        finish_call(self.r, call["call_id"], "failed")
        a = admin_project_budget_status(self.r, self.a)
        b = admin_project_budget_status(self.r, self.b)
        self.assertEqual((a["used_calls"], a["started_attempts"], a["failed_attempts"], a["usage_unknown_attempts"]), (1, 1, 1, 1))
        self.assertEqual((b["used_calls"], b["remaining_calls"]), (0, 100))
        self.assertEqual(admin_budget_status(self.r)["used_calls"], 1)
        self.assertEqual((a["completed_reviews"], a["reported_tokens"], a["accounted_tokens"]), (0, 0, 4000))

    def test_project_limit_blocks_only_its_own_budget_while_shared_guard_still_applies(self):
        self.project_limit(max_calls_per_day=1)
        first = self.started()
        finish_call(self.r, first["call_id"], "failed")
        blocked = self.claim(problem="next-a")
        self.assertEqual((blocked["status"], blocked["blocking_scope"]), ("budget_exhausted", "project"))
        second = self.claim(self.b, "first-b")
        self.assertEqual(second["status"], "reserved")
        finish_call(self.r, second["call_id"], "not_started")
        self.global_limit(max_calls_per_day=1)
        blocked = self.claim(self.b, "still-first-b")
        self.assertEqual((blocked["status"], blocked["blocking_scope"]), ("budget_exhausted", "ALP_HOME"))
        self.assertEqual(admin_project_budget_status(self.r, self.b)["used_calls"], 0)

    def test_token_limit_is_project_scoped_but_uses_shared_call_reservations(self):
        self.project_limit(max_reported_tokens_per_day=3999)
        blocked = self.claim()
        self.assertEqual((blocked["status"], blocked["blocking_scope"]), ("token_budget_exhausted", "project"))
        other = self.claim(self.b)
        self.assertEqual(other["status"], "reserved")
        finish_call(self.r, other["call_id"], "not_started")
        self.project_limit(max_reported_tokens_per_day=None)
        self.global_limit(max_reported_tokens_per_day=3999)
        blocked = self.claim()
        self.assertEqual((blocked["status"], blocked["blocking_scope"]), ("token_budget_exhausted", "ALP_HOME"))

    def test_counts_require_accepted_receipts_and_preserve_unknown_attempts(self):
        usage = {"token_totals": {"input_tokens": 10, "output_tokens": 2}, "completeness": "complete"}
        accepted = self.started(problem="accepted")
        finish_call(self.r, accepted["call_id"], "skip", usage)
        self.receipt(accepted["call_id"])
        missing = self.started(problem="missing-receipt")
        finish_call(self.r, missing["call_id"], "skip", usage)
        unknown = self.started(problem="unknown")
        finish_call(self.r, unknown["call_id"], "timeout_unknown")
        not_started = self.claim(problem="not-started")
        finish_call(self.r, not_started["call_id"], "not_started")
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created) "
                          "VALUES('duplicate',?,'duplicate',1,?,'deduplicated',?)",
                          (self.a, admin_budget_status(self.r)["utc_day"], time.time()))
        result = admin_project_budget_status(self.r, self.a)
        self.assertEqual((result["used_calls"], result["counted_attempts"], result["started_attempts"]), (3, 3, 3))
        self.assertEqual((result["completed_reviews"], result["result_unknown_attempts"],
                          result["usage_unknown_attempts"], result["not_started_attempts"],
                          result["deduplicated_attempts"]), (1, 1, 1, 1, 1))

    def test_reused_receipt_is_not_an_independent_completed_review(self):
        call = self.started()
        finish_call(self.r, call["call_id"], "skip")
        self.receipt(call["call_id"], duplicate_of="another-accepted-call")
        status = admin_project_budget_status(self.r, self.a)
        self.assertEqual((status["used_calls"], status["completed_reviews"]), (1, 0))

    def test_prior_day_active_call_still_occupies_the_shared_concurrency_slot(self):
        call = self.started()
        self.r.db.execute("UPDATE admin_calls SET day='1970-01-01' WHERE id=?", (call["call_id"],))
        budget = admin_admission_status(self.r, self.b, now=172799)
        self.assertEqual((budget["utc_day"], budget["next_reset_at"]), ("1970-01-02", 172800))
        self.assertEqual(budget["project_usage"]["used_calls"], 0)
        self.assertEqual((budget["admission_status"], budget["blocking_scope"]), ("concurrency_limited", "ALP_HOME"))

    def test_lowering_project_budget_does_not_cancel_or_erase_inflight_attempt(self):
        call = self.started()
        self.project_limit(max_calls_per_day=0)
        result = admin_project_budget_status(self.r, self.a)
        self.assertEqual((result["used_calls"], result["remaining_calls"]), (1, 0))
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()[0], "started")
        self.assertEqual(project_budget_policy(self.r, self.b)["values"]["max_calls_per_day"], 100)

    def test_project_budget_changes_do_not_change_paid_review_signatures(self):
        packet = {"problem": {"id": "case", "revision": 1, "summary": "review", "category": "project"},
                  "events": [], "task": None}
        before = review_signature(packet, _review_configuration(self.r.admin_effective_config(self.a), {}), _fingerprint(packet))
        self.project_limit(max_calls_per_day=8, max_calls_per_batch=2, max_cases_per_batch=10,
                           max_batch_seconds=20, max_run_seconds=30, timeout_seconds=10,
                           max_reported_tokens_per_day=12000)
        after = review_signature(packet, _review_configuration(self.r.admin_effective_config(self.a), {}), _fingerprint(packet))
        self.assertEqual(before, after)

    def test_new_project_controls_replay_without_modifying_shared_guard_or_other_project(self):
        body = self.body()
        with patch.object(self.r, "_launch_admin") as launch, patch("subprocess.Popen") as model:
            result = apply_control(self.r, self.a, body)
            self.assertEqual(apply_control(self.r, self.a, body), result)
        self.assertEqual(result["settings"]["project_budget_policy"]["values"]["max_calls_per_day"], 12)
        self.assertEqual(admin_budget_policy(self.r)["version"], 0)
        self.assertFalse(project_budget_policy(self.r, self.b)["configured"])
        launch.assert_not_called()
        model.assert_not_called()

    def test_new_project_controls_do_not_depend_on_shared_guard_version(self):
        body = self.body(expected_budget_version=0)
        self.global_limit(max_calls_per_day=300)
        result = apply_control(self.r, self.a, body)
        self.assertEqual(result["settings"]["project_budget_policy"]["values"]["max_calls_per_day"], 12)
        self.assertEqual(result["settings"]["global_budget_policy"]["values"]["max_calls_per_day"], 300)

    def test_project_stale_version_and_mismatched_scope_are_rejected(self):
        stale = self.body()
        self.project_limit(max_calls_per_day=30)
        with self.assertRaisesRegex(ContractError, "settings changed"):
            apply_control(self.r, self.a, stale)
        for updates in ({"project_limits": {"max_concurrent_calls": 5}},
                        {"project_limits": {"max_calls_per_day": True}}):
            with self.subTest(updates=updates), self.assertRaises(ContractError):
                apply_control(self.r, self.a, self.body(**updates))
        body = self.body()
        del body["expected_project_budget_version"]
        body["expected_budget_version"] = 0
        with self.assertRaises(ContractError):
            apply_control(self.r, self.a, body)

    def test_project_control_version_is_independent_from_other_projects(self):
        body = self.body()
        self.project_limit(self.b, max_calls_per_day=3)
        apply_control(self.r, self.a, body)
        self.assertEqual(project_budget_policy(self.r, self.a)["values"]["max_calls_per_day"], 12)
        self.assertEqual(project_budget_policy(self.r, self.b)["values"]["max_calls_per_day"], 3)

    def test_status_is_readonly_and_keeps_legacy_fields_global(self):
        self.project_limit(max_calls_per_day=7)
        call = self.started()
        finish_call(self.r, call["call_id"], "failed")
        before = self.r.db.total_changes
        status = control_status(self.r, self.b)
        runtime_status = self.r.status(self.b)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(status["project_usage"]["used_calls"], 0)
        self.assertEqual(status["usage"]["used_calls"], 1)
        self.assertEqual(status["usage"], status["global_usage"])
        self.assertEqual(status["budget_policy"]["scope"], "ALP_HOME")
        self.assertEqual(runtime_status["project_usage"]["used_calls"], 0)
        self.assertEqual(runtime_status["admin_budget"]["used_calls"], 1)

    def test_old_calls_need_no_rewriting_and_missing_project_table_read_does_not_recreate_it(self):
        call = self.started()
        finish_call(self.r, call["call_id"], "failed")
        old_call = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone())
        self.r.db.execute("DROP TABLE admin_project_budget_policies")
        before = self.r.db.total_changes
        status = admin_project_budget_status(self.r, self.a)
        self.assertEqual(status["used_calls"], 1)
        self.assertFalse(status["budget_policy"]["configured"])
        self.assertEqual(self.r.db.total_changes, before)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_project_budget_policies'").fetchone())
        with self.r.transaction():
            install_budget(self.r)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()), old_call)

    def test_concurrent_projects_cannot_both_claim_the_last_shared_slot(self):
        self.global_limit(max_calls_per_day=1, max_concurrent_calls=2)
        barrier, results, errors = threading.Barrier(2), [], []

        def claim_in_thread(project):
            runtime = None
            try:
                runtime = Runtime(self.home, config={})
                barrier.wait(timeout=10)
                with runtime.transaction():
                    results.append(claim_call(runtime, project, "race-" + project, 1, project, runtime.host_epoch(), 30))
            except BaseException as exc:
                errors.append(exc)
                barrier.abort()
            finally:
                if runtime:
                    runtime.close()

        threads = [threading.Thread(target=claim_in_thread, args=(project,)) for project in (self.a, self.b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result["status"] for result in results), ["budget_exhausted", "reserved"])
        self.assertEqual(admin_budget_status(self.r)["used_calls"], 1)

    def test_schema18_upgrade_fences_old_connections_and_preserves_unknown_calls(self):
        from agent_learning.mcp import Server
        old_server = Server(self.r, self.a)
        native = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(native.close)
        old_native_server = Server(native, self.a)
        call = self.started()
        old_epoch = self.r.host_epoch()
        self.r.db.execute("DROP TABLE admin_project_budget_policies")
        self.r.db.execute("PRAGMA user_version=17")
        upgraded = Runtime(self.home, config={})
        self.addCleanup(upgraded.close)
        self.assertGreater(upgraded.host_epoch(), old_epoch)
        self.assertGreater(native.session_epoch(), old_native_server.epoch)
        for server in (old_server, old_native_server):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                server.call("learning_status", {})
        record = upgraded.db.execute("SELECT * FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()
        self.assertEqual(record["state"], "orphaned_unknown")
        self.assertEqual(record["retry_allowed"], 0)
        self.assertEqual(admin_project_budget_status(upgraded, self.a)["used_calls"], 1)
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 24)
        reopened = Runtime(self.home, config={})
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.host_epoch(), upgraded.host_epoch())

    def test_thirty_two_failed_attempts_do_not_appear_in_another_project(self):
        for index in range(32):
            call = self.started(self.a, "failed-case-" + str(index))
            finish_call(self.r, call["call_id"], "failed")
        a, b = control_status(self.r, self.a), control_status(self.r, self.b)
        self.assertEqual(a["project_usage"]["counted_attempts"], 32)
        self.assertEqual(a["project_usage"]["completed_reviews"], 0)
        self.assertEqual(a["project_usage"]["failed_attempts"], 32)
        self.assertEqual(a["project_usage"]["usage_unknown_attempts"], 32)
        self.assertEqual(b["project_usage"]["counted_attempts"], 0)
        self.assertEqual(b["project_usage"]["remaining_calls"], 100)
        self.assertEqual(b["global_usage"]["counted_attempts"], 32)
        self.assertEqual(b["admission"]["remaining_calls"], 68)


if __name__ == "__main__":
    unittest.main()
