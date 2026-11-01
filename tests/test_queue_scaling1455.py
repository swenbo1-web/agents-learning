"""Queue SQL equivalence and work bounds on isolated SQLite data, without models."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin_queue import (REVIEW_ORDER, eligible_query, executor_query,
    executor_review_order, install_queue)
from agent_learning.admin_workload import CASE_VIEW, workload_summary
from agent_learning.runtime import Runtime


# Keep the previous query independent of the implementation under test.
OLD_REVIEW_ORDER = (
    "ORDER BY COALESCE((SELECT MAX(c.finished) FROM admin_calls c WHERE c.project=p.project "
    "AND c.problem=p.id AND c.revision=p.revision AND c.state='not_started'),0),"
    "CASE WHEN p.knowledge_id IS NOT NULL THEN 0 "
    "WHEN p.resolution_status='recovery_observed' THEN 1 "
    "WHEN EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
    "WHERE pe.problem=p.id AND e.project=p.project AND e.type='PostToolUse' AND e.outcome='succeeded') THEN 2 "
    "WHEN EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
    "WHERE pe.problem=p.id AND e.project=p.project AND e.type NOT IN ('PreToolUse','PostToolUse')) THEN 3 "
    "ELSE 4 END,p.created,p.id ")


class QueueScaling1455Tests(unittest.TestCase):
    def setUp(self):
        local = Path(__file__).resolve().parents[1] / "build" / "local"
        local.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="queue1455-", dir=local)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        isolated = {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(self.root / "config.json"),
                    "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
                    "CODEX_HOME": str(self.root / "codex"),
                    **{key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}}
        (self.root / "tmp").mkdir()
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        self.env = patch.dict(os.environ, isolated)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.r = Runtime(self.root / "home", config={"admin": {
            "enabled": True, "model_mode": "fixed", "model": "fixture", "command": [sys.executable]}})
        self.addCleanup(self.r.close)
        self.project = self.r.project(self.root / "project")
        self.other = self.r.project(self.root / "other-project")

    def problem(self, ident, *, project=None, created=1, conversation="chat", knowledge=None, recovery=False):
        self.r.db.execute("INSERT INTO problems(id,project,conversation,revision,summary,category,status,"
            "knowledge_id,created,resolution_status) VALUES(?,?,?,1,?,'operation','pending',?,?,?)",
            (ident, project or self.project, conversation, ident, knowledge, created,
             "recovery_observed" if recovery else "open"))
        return ident

    def event(self, ident, kind="PreToolUse", outcome="unknown", *, project=None, problem=None):
        self.r.db.execute("INSERT INTO events(id,project,conversation,type,outcome,payload,created) "
            "VALUES(?,?,'chat',?,?, '{}',1)", (ident, project or self.project, kind, outcome))
        if problem:
            self.r.db.execute("INSERT INTO problem_events(problem,event) VALUES(?,?)", (problem, ident))

    def call(self, ident, problem, state, *, project=None, revision=1, finished=None, retry=0):
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created,finished,retry_allowed) "
            "VALUES(?,?,?,?,'2026-01-01',?,1,?,?)",
            (ident, project or self.project, problem, revision, state, finished, retry))

    def noise_events(self, start, count):
        with self.r.transaction():
            self.r.db.executemany("INSERT INTO events(id,project,conversation,type,outcome,payload,created) "
                "VALUES(?,?,'noise',?,?,'{}',1)",
                ((f"noise-{number}", self.project, "PostToolUse" if number % 2 else "PreToolUse",
                  "succeeded" if number % 2 else "unknown") for number in range(start, start + count)))

    def measured(self, sql, args=()):
        ticks = 0

        def progress():
            nonlocal ticks
            ticks += 1
            return 0

        self.r.db.set_progress_handler(progress, 100)
        try:
            rows = [tuple(row) for row in self.r.db.execute(sql, args)]
        finally:
            self.r.db.set_progress_handler(None, 0)
        return rows, ticks * 100

    def plan(self, sql, args=()):
        # Avoid reusing a Python sqlite3 EXPLAIN statement across index changes.
        cookie = self.r.db.execute("PRAGMA schema_version").fetchone()[0]
        return [row[3] for row in self.r.db.execute(f"EXPLAIN QUERY PLAN {sql} /* schema {cookie} */", args)]

    def assert_orders_equal(self, **kwargs):
        query, args = eligible_query("fixture-signature", **kwargs)
        args = (self.project, *args)
        previous = [row["id"] for row in self.r.db.execute(query + OLD_REVIEW_ORDER, args)]
        current = [row["id"] for row in self.r.db.execute(query + REVIEW_ORDER, args)]
        self.assertEqual(current, previous)
        # Verify the full order, then all bounded prefixes used by consumers.
        for limit in (1, 40):
            bounded = [row["id"] for row in self.r.db.execute(query + REVIEW_ORDER + "LIMIT ?", (*args, limit))]
            self.assertEqual(bounded, previous[:limit])
        return current

    def test_full_order_keeps_evidence_priorities_failure_rotation_and_project_boundaries(self):
        priorities = {}
        for number in range(72):
            ident = f"case-{number:03}"
            kind = number % 12
            self.problem(ident, created=number % 4, conversation=("antigravity:chat" if number % 2 else "chat"),
                         knowledge="known" if kind == 0 else None, recovery=kind == 1)
            priorities[ident] = 0 if kind == 0 else 1 if kind == 1 else 2 if kind in (2, 11) else 3 if kind == 3 else 4
            if kind in (2, 11):
                self.event("success-" + ident, "PostToolUse", "succeeded", problem=ident)
            if kind in (3, 11):
                self.event("report-" + ident, "Feedback", "reported", problem=ident)
            if kind == 4:
                self.event("failure-" + ident, "PostToolUse", "failed", problem=ident)
            if kind == 5:
                self.event("pre-" + ident, "PreToolUse", "succeeded", problem=ident)
            if kind == 6:
                self.event("foreign-" + ident, "PostToolUse", "succeeded", project=self.other, problem=ident)
                self.event("foreign-report-" + ident, "Feedback", "reported", project=self.other, problem=ident)
            if kind == 7:
                self.r.db.execute("INSERT INTO problem_events(problem,event) VALUES(?,?)", (ident, "absent-" + ident))
            if kind == 8:
                self.event("unknown-" + ident, "PostToolUse", "unknown", problem=ident)
        delayed = "case-000"
        self.call("not-started-old", delayed, "not_started", finished=5)
        self.call("not-started-new", delayed, "not_started", finished=9)
        self.call("wrong-revision", "case-002", "not_started", revision=2, finished=1000)
        self.call("wrong-project", "case-003", "not_started", project=self.other, finished=1000)
        self.call("null-finish", "case-004", "not_started")
        self.call("locked-failure", "case-005", "failed")
        self.call("locked-unknown", "case-006", "timeout_unknown")
        self.call("retry-explicit", "case-007", "failed", retry=1)
        self.call("stale-failure", "case-008", "failed", revision=2)
        self.problem("foreign-problem", project=self.other)
        changes = self.r.db.total_changes
        current = self.assert_orders_equal()
        expected = sorted((ident for ident in priorities if ident not in {"case-005", "case-006"}),
                          key=lambda ident: (9 if ident == delayed else 0, priorities[ident], int(ident[-3:]) % 4, ident))
        self.assertEqual(current, expected)
        self.assert_orders_equal(include_cached=True)
        self.assert_orders_equal(problem_id="case-005")
        self.assertEqual(self.r.db.total_changes, changes)

    def test_executor_route_and_phase_scopes_keep_the_previous_order(self):
        for number in range(45):
            ident = self.problem(f"route-{number:03}", created=number)
            if number % 2:
                self.event("route-event-" + ident, "PostToolUse", "succeeded", problem=ident)
        self.r.db.execute("INSERT INTO admin_review_checks(project,problem,revision,signature,task_updated,updated,blocked_call) "
                          "VALUES(?,?,1,?,NULL,10,'executor_scan')", (self.project, "route-001", "route-signature"))
        query, args = eligible_query("different-scan-signature")
        query, args = executor_query(self.r, self.project, query, args)
        order, order_args = executor_review_order(self.r, self.project, "route-signature")
        old_order = order.replace(REVIEW_ORDER.removeprefix("ORDER BY "), OLD_REVIEW_ORDER.removeprefix("ORDER BY "))
        params = (self.project, *args, *order_args)
        old = [row["id"] for row in self.r.db.execute(query + old_order, params)]
        current = [row["id"] for row in self.r.db.execute(query + order, params)]
        self.assertEqual(current, old)
        self.assertEqual(current[-1], "route-001")
        for number, scope in enumerate((self.r._session_scope(), "another-host")):
            ident = self.problem(f"phase-{number}", conversation=f"phase-review:{number}")
            self.r.db.execute("INSERT INTO phase_review_batches(project,problem_id,host_scope,session_epoch,"
                "governance_epoch,policy_version,phase_ids,packet,state,created) VALUES(?,?,?,?,?,1,'[]','{}','pending',1)",
                (self.project, ident, scope, self.r.session_epoch(), self.r.host_epoch()))
        self.assertEqual(self.assert_orders_equal(phase_scope=self.r._session_scope()), ["phase-0"])
        self.assertFalse(any(ident.startswith("phase-") for ident in self.assert_orders_equal()))

    def test_limit_forty_uses_linked_evidence_work_instead_of_all_project_events(self):
        for number in range(160):
            ident = self.problem(f"scaling-{number:03}", created=number)
            self.event("pre-" + ident, problem=ident)
        # Both evidence predicates must fail after checking each linked row.
        self.noise_events(0, 1500)
        query, args = eligible_query("scaling-signature")
        args = (self.project, *args)
        small_new, new_small_steps = self.measured(query + REVIEW_ORDER + "LIMIT 40", args)
        small_old, old_small_steps = self.measured(query + OLD_REVIEW_ORDER + "LIMIT 40", args)
        self.noise_events(1500, 18000)
        large_new, new_large_steps = self.measured(query + REVIEW_ORDER + "LIMIT 40", args)
        large_old, old_large_steps = self.measured(query + OLD_REVIEW_ORDER + "LIMIT 40", args)
        self.assertEqual(small_new, small_old)
        self.assertEqual(large_new, large_old)
        self.assertEqual(large_new, small_new)
        self.assertLess(new_large_steps, new_small_steps * 1.2 + 1000)
        self.assertGreater(old_large_steps, old_small_steps * 4)
        self.assertGreater(old_large_steps, new_large_steps * 10)
        details = self.plan(query + REVIEW_ORDER + "LIMIT 40", args)
        link_searches = [i for i, text in enumerate(details) if "SEARCH pe " in text and "(problem=?)" in text]
        event_searches = [i for i, text in enumerate(details) if "SEARCH e " in text and "(id=?)" in text]
        self.assertEqual(len(link_searches), 2, details)
        self.assertEqual(len(event_searches), 2, details)
        self.assertTrue(all(link < event for link, event in zip(link_searches, event_searches)), details)
        self.assertFalse(any("SEARCH e " in text and "(project=?" in text for text in details), details)
        print(json.dumps({"queue_vm_steps": {"old_small": old_small_steps, "old_large": old_large_steps,
            "new_small": new_small_steps, "new_large": new_large_steps}, "queue_plan": details}))

    def test_candidate_lookup_work_does_not_grow_with_unrelated_candidates(self):
        for number in range(100):
            self.problem(f"candidate-case-{number:03}")
        self.r.db.execute("INSERT INTO candidates VALUES('matching',?,?,1,'{}','deferred',1)",
                          (self.project, "candidate-case-000"))
        self.r.db.execute("INSERT INTO candidates VALUES('stale',?,?,2,'{}','candidate',1)",
                          (self.project, "candidate-case-001"))
        self.r.db.execute("INSERT INTO candidates VALUES('foreign',?,?,1,'{}','candidate',1)",
                          (self.other, "candidate-case-002"))
        query = CASE_VIEW + "SELECT kind,queue_state,status,COUNT(*) FROM cases GROUP BY kind,queue_state,status"
        small, small_steps = self.measured(query, (self.project,))
        with self.r.transaction():
            self.r.db.executemany("INSERT INTO candidates VALUES(?,?,?,1,'{}','candidate',1)",
                                 ((f"unrelated-{number}", self.project, "unrelated-problem") for number in range(10000)))
        indexed, indexed_steps = self.measured(query, (self.project,))
        self.r.db.execute("DROP INDEX candidates_problem_status")
        unindexed, unindexed_steps = self.measured(query, (self.project,))
        install_queue(self.r)
        self.assertEqual(indexed, small)
        self.assertEqual(indexed, unindexed)
        self.assertLess(indexed_steps, small_steps * 1.2 + 1000)
        self.assertGreater(unindexed_steps, indexed_steps * 10)
        summary = workload_summary(self.r.db, self.project)
        self.assertEqual(summary["stages"]["awaiting_approval"], 1)
        self.assertEqual(summary["stages"]["pending_review"], 99)
        details = self.plan(query, (self.project,))
        self.assertTrue(any("COVERING INDEX candidates_problem_status" in text and "problem_revision=?" in text
                            for text in details), details)
        print(json.dumps({"candidate_vm_steps": {"small": small_steps, "indexed_large": indexed_steps,
                                                  "unindexed_large": unindexed_steps}}))

    def test_indexes_reinstall_on_existing_database_without_schema_or_record_changes(self):
        self.problem("persisted")
        self.event("persisted-event", "Feedback", "reported", problem="persisted")
        before = self.r.db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(before, 25)
        self.r.db.execute("DROP INDEX events_project_type")
        self.r.db.execute("DROP INDEX candidates_problem_status")
        self.r.close()
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        with self.r.transaction():
            install_queue(self.r)
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], before)
        self.assertEqual([row[2] for row in self.r.db.execute("PRAGMA index_info(events_project_type)")],
                         ["project", "type"])
        self.assertEqual([row[2] for row in self.r.db.execute("PRAGMA index_info(candidates_problem_status)")],
                         ["project", "problem", "problem_revision", "status"])
        details = self.plan("SELECT type,COUNT(*) FROM events WHERE project=? GROUP BY type", (self.project,))
        self.assertTrue(any("COVERING INDEX events_project_type" in text for text in details), details)
        self.assertFalse(any("TEMP B-TREE" in text for text in details), details)
        self.assertEqual(self.r.db.execute("SELECT summary FROM problems WHERE id='persisted'").fetchone()[0], "persisted")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problem_events").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
