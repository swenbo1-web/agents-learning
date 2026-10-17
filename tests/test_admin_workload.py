"""Real isolated SQLite: exclusive stages, bounded groups and incremental review."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint, admin_once
from agent_learning.admin_control import control_status
from agent_learning.admin_decisions import persist_receipt, review_signature
from agent_learning.admin_workload import hold_unchanged_review, workload_page, workload_summary
from agent_learning.knowledge_browser import _read_api
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class AdminWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="admin-workload-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.r = Runtime(root / "home", config={"admin": {"enabled": True, "command": [sys.executable],
                                                         "max_calls_per_day": 100}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def problem(self, summary="compiler failed", chat="root", **kwargs):
        return self.r.feedback(self.p, chat, summary, category="operation", **kwargs)

    def decision(self, problem, decision="needs_evidence"):
        packet = self.r.review_packet(self.p, problem["id"])
        evidence = _fingerprint(packet)
        signature = review_signature(packet, self.r.config["admin"], evidence)
        persist_receipt(self.r, self.p, packet, evidence, signature, None,
                        {"decision": decision, "reason": "Needs a real verification"})
        return packet, evidence

    def test_stages_partition_rows_and_history_is_not_daily_work(self):
        ready = self.problem("ready")
        self.decision(self.problem("needs evidence"))
        self.decision(self.problem("local wait"), "preflight_blocked")
        self.decision(self.problem("skipped"), "skip")
        candidate = self.problem("candidate")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            admin_once(self.r, self.p, candidate["id"])
        blocked = self.problem("unknown")
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created) "
                          "VALUES(?,?,?,?,?,'timeout_unknown',0)",
                          ("unknown-call", self.p, blocked["id"], blocked["revision"], "2000-01-01"))
        running = self.problem("running")
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created) "
                          "VALUES(?,?,?,?,?,'launch_intent',0)",
                          ("running-call", self.p, running["id"], running["revision"], "2000-01-01"))
        (self.workspace / "README.md").write_text("# Project\nThe database is SQLite.\n", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.r.history_run(self.p, job, allow_review=False, max_calls=0)
        before = self.r.db.total_changes
        result = workload_summary(self.r.db, self.p)
        self.assertEqual(result["stages"]["pending_review"], 1)
        self.assertEqual(result["stages"]["reviewing"], 1)
        self.assertEqual(result["stages"]["awaiting_evidence"], 1)
        self.assertEqual(result["stages"]["local_deferred"], 1)
        self.assertEqual(result["stages"]["awaiting_approval"], 1)
        self.assertEqual(result["stages"]["blocked"], 1)
        self.assertEqual(result["stages"]["skipped"], 1)
        self.assertEqual(sum(result["stages"].values()), 7)
        self.assertGreater(result["history_pending_units"], 0)
        self.assertEqual(result["pending_total"], 7 + result["history_pending_units"])
        page = workload_page(self.r.db, self.p, stage="pending_review")
        self.assertEqual(page["total_cases"], 1)
        self.assertEqual(page["items"][0]["members"][0]["id"], ready["id"])
        self.assertEqual(self.r.db.total_changes, before)

    def test_old_review_does_not_hide_new_evidence_and_candidate_rejection_completes(self):
        problem = self.problem()
        self.decision(problem)
        self.problem(resolution="New verified outcome")
        self.assertEqual(workload_summary(self.r.db, self.p)["stages"]["pending_review"], 1)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(workload_summary(self.r.db, self.p)["stages"]["awaiting_approval"], 1)
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (result["candidate_id"],))
        stages = workload_summary(self.r.db, self.p)["stages"]
        self.assertEqual(stages["awaiting_approval"], 0)
        self.assertEqual(stages["completed"], 1)

    def test_related_reports_group_without_merging_records_or_business_ids(self):
        a = self.problem('shell failed: {"session_id":"first", "code":400}', "one")
        b = self.problem('shell failed: {"session_id":"second", "code":400}', "two")
        self.problem('shell failed: {"session_id":"third", "code":500}', "three")
        other_host = Runtime(self.r.home, host="dsh", config={})
        try:
            other_host.feedback(self.p, "dsh:default:four", 'shell failed: {"session_id":"fourth", "code":400}', category="operation")
        finally:
            other_host.close()
        business = "1c90160b-1111-2222-3333-123456789012"
        self.problem("Object " + business, "five")
        self.problem("Object " + business.replace("1111", "7777"), "six")
        before = self.r.db.total_changes
        data = workload_page(self.r.db, self.p)
        group = next(g for g in data["items"] if g["member_count"] == 2)
        self.assertEqual({m["id"] for m in group["members"]}, {a["id"], b["id"]})
        self.assertEqual(data["total"], 5)
        self.assertTrue(group["grouping_only"])
        members = workload_page(self.r.db, self.p, view="cases", group_id=group["group_id"], limit=1)
        self.assertEqual(members["total"], 2)
        other = workload_page(self.r.db, self.p, view="cases", group_id=group["group_id"], limit=1, offset=1)
        self.assertNotEqual(other["items"][0]["id"], members["items"][0]["id"])
        self.assertIsNone(other["next_offset"])
        self.assertEqual(self.r.db.total_changes, before)

    def test_task_scope_and_projects_remain_separate(self):
        for chat, environment in (("one", "A"), ("two", "B")):
            self.r.task_update(self.p, chat, "Build", {"environment": environment})
            self.problem(chat=chat)
        self.assertEqual(workload_page(self.r.db, self.p)["total"], 2)
        self.assertEqual(workload_page(self.r.db, "foreign")["total"], 0)

    def test_bounded_groups_leave_individual_pagination_available(self):
        for i in range(4):
            self.problem(chat=str(i))
        with patch("agent_learning.admin_workload.GROUP_SCAN_LIMIT", 2):
            data = workload_page(self.r.db, self.p)
            self.assertTrue(data["groups_truncated"])
            self.assertEqual((data["sampled_cases"], data["total_cases"]), (2, 4))
            self.assertEqual(workload_page(self.r.db, self.p, view="cases", offset=2)["total"], 4)

    def test_api_readonly_stats_and_validation(self):
        self.problem()
        before = self.r.db.total_changes
        data = _read_api(self.r.db, self.p, "/api/admin-groups", {"stage": "pending_review"})
        self.assertEqual(data["total_cases"], 1)
        stats = _read_api(self.r.db, self.p, "/api/stats", {})
        self.assertEqual(stats["workload"]["stages"]["pending_review"], 1)
        self.assertEqual(self.r.db.total_changes, before)
        for values in ({"stage": "bogus"}, {"view": "bogus"}, {"group_id": "x"}, {"extra": "x"}):
            with self.subTest(values=values), self.assertRaises(ContractError):
                _read_api(self.r.db, self.p, "/api/admin-groups", values)

    def test_response_budget_keeps_a_correct_continuation_cursor(self):
        for i in range(4):
            self.problem("Report " + str(i))
        with patch("agent_learning.admin_workload.PAGE_BYTES", 1):
            page = workload_page(self.r.db, self.p, view="cases")
            self.assertTrue(page["page_budget_limited"])
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual(page["next_offset"], 1)
            second = workload_page(self.r.db, self.p, view="cases", offset=page["next_offset"])
            self.assertEqual(second["next_offset"], 2)
            self.assertNotEqual(page["items"][0]["id"], second["items"][0]["id"])

    def test_recovery_evidence_precedes_many_older_bare_errors(self):
        for i in range(12):
            event = self.r.record_event(self.p, str(i), "PostToolUse", {"tool_name": "shell"}, outcome="failed")
            self.r._problem(self.p, str(i), "Failure " + str(i), "operation", [event["id"]])
        recovered = self.problem("Has a recovery result")
        self.r.db.execute("UPDATE problems SET resolution_status='recovery_observed' WHERE id=?", (recovered["id"],))
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["problem_id"], recovered["id"])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["scanned_count"], 1)

    def test_older_browser_database_keeps_basic_stats_without_migration(self):
        self.problem()
        self.r.db.execute("DROP TABLE work_summary_links")
        self.r.db.execute("PRAGMA user_version=17")
        before = self.r.db.total_changes
        stats = _read_api(self.r.db, self.p, "/api/stats", {})
        self.assertEqual(stats["problems"]["pending"], 1)
        self.assertFalse(stats["workload"]["available"])
        self.assertIsNone(stats["workload"]["stages"]["pending_review"])
        page = _read_api(self.r.db, self.p, "/api/admin-groups", {})
        self.assertEqual(page["summary"]["reason"], "workload_upgrade_required")
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 17)
        self.assertEqual(self.r.db.total_changes, before)

    def test_missing_evidence_holds_on_model_change_but_explicit_review_is_available(self):
        problem = self.problem()
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("needs_evidence")):
            admin_once(self.r, self.p)
        self.r.config["admin"].update(model_mode="fixed", model="different-model")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            admin_once(self.r, self.p)
            model.assert_not_called()
            admin_once(self.r, self.p, problem["id"])
            self.assertEqual(model.call_count, 1)

    def test_missing_evidence_gate_observes_health_and_dependencies(self):
        packet, evidence = self.decision(self.problem())
        self.assertTrue(hold_unchanged_review(self.r, self.p, packet, evidence))
        for field, value in (("related_knowledge", [{"knowledge_id": "new", "revision": 1}]),
                             ("related_candidates", [{"candidate_id": "new"}]), ("review_context_revision", 1)):
            changed = copy.deepcopy(packet)
            changed[field] = value
            self.assertFalse(hold_unchanged_review(self.r, self.p, changed, evidence))
        self.assertFalse(hold_unchanged_review(self.r, self.p, packet, "changed-health-or-evidence"))
        changed = copy.deepcopy(packet)
        changed["problem"]["knowledge_id"] = "feedback-target"
        self.assertFalse(hold_unchanged_review(self.r, self.p, changed, evidence))

    def test_all_host_snapshots_use_the_same_additive_counts(self):
        self.problem()
        self.decision(self.problem("wait"))
        for host in ("codex", "antigravity", "dsh"):
            other = Runtime(self.r.home, host=host, config={})
            try:
                status = other.status(self.p)
                self.assertEqual(status["counts"]["pending_problems"], 2)
                self.assertEqual(status["counts"]["pending_review_cases"], 1)
                self.assertEqual(status["counts"]["awaiting_evidence_cases"], 1)
                control = control_status(other, self.p)
                self.assertEqual(control["queue"]["workload"]["stages"]["pending_review"], 1)
            finally:
                other.close()


if __name__ == "__main__":
    unittest.main()
