"""Missing original Stop diagnostics are bounded observations, never recovery."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.phase_boundary_diagnostics import delivery_boundary_diagnostics, EVENT_SCAN_LIMIT, SAMPLE_LIMIT
from agent_learning.phase_review import configure, status
from agent_learning.runtime import Runtime


class PhaseBoundaryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="boundary-diag-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.serial = 0

    def runtime(self, host="codex"):
        self.serial += 1
        r = Runtime(self.root / (host + str(self.serial)), host=host, config={})
        self.addCleanup(r.close)
        p = r.project(self.workspace)
        configure(r, p, {"mode": "phase", "every_phases": 20}, 0)
        return r, p

    def phase(self, r, p, native="chat"):
        c = r.host_conversation(native)
        t = r.task_update(p, c, "SECRET_TASK_GOAL", new_task=True)
        t = r.task_update(p, c, t["goal"], status="delivered")
        phase = dict(r.db.execute("SELECT * FROM review_phases WHERE task_id=?", (t["id"],)).fetchone())
        return c, phase

    def stop(self, r, p, c, **kwargs):
        task = r.task(p, c)
        kwargs.setdefault("task_id", task["id"])
        kwargs.setdefault("task_revision", task["revision"])
        event, _ = r._event(p, c, "Stop", {"last_assistant_message": "SECRET_PUBLIC_TEXT"},
                            outcome="reported", **kwargs)
        return event

    def request(self, r, phase, stop, attempts=0):
        r.db.execute("INSERT INTO phase_seal_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            phase["id"], phase["project"], phase["host_scope"], phase["session_epoch"], phase["governance_epoch"],
            stop["id"], str(self.workspace), '{"SECRET_CAPTURE":true}', time.time(), attempts,
            "local_seal_failed" if attempts else None, 0))

    def only(self, r, p):
        before = r.db.total_changes
        with patch("subprocess.Popen", side_effect=AssertionError("diagnostics cannot start processes")), \
                patch("agent_learning.phase_review.prepare", side_effect=AssertionError("diagnostics cannot prepare")), \
                patch("agent_learning.phase_seal_recovery.recover", side_effect=AssertionError("diagnostics cannot recover")):
            result = delivery_boundary_diagnostics(r, p)
        self.assertTrue(result["checked"], result)
        self.assertEqual(r.db.total_changes, before)
        self.assertEqual(result["counts_scope"], "sample")
        self.assertEqual(result["sampled"], len(result["items"]))
        encoded = json.dumps(result)
        self.assertNotIn("SECRET", encoded)
        self.assertEqual(len(result["items"]), 1)
        return result["items"][0]

    def test_empty_status_needs_no_event_or_capture_queries(self):
        r, p = self.runtime()
        statements = []
        r.db.set_trace_callback(statements.append)
        value = delivery_boundary_diagnostics(r, p)
        r.db.set_trace_callback(None)
        self.assertEqual(value["sampled"], 0)
        self.assertFalse(value["truncated"])
        self.assertFalse(any("FROM events" in sql or "FROM phase_seal_requests" in sql for sql in statements))

    def test_all_hosts_separate_current_wait_from_missing_old_delivery(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p = self.runtime(host)
                c, phase = self.phase(r, p)
                item = self.only(r, p)
                self.assertEqual(item["reason"], "awaiting_host_stop")
                self.assertFalse(item["stop_observed"])
                r.task_update(p, c, "Different later task", new_task=True)
                item = self.only(r, p)
                self.assertEqual(item["reason"], "missing_host_stop_after_boundary")
                self.assertEqual(item["task_id"], phase["task_id"])
                self.assertEqual(item["delivery_version"], phase["delivery_version"])
                self.assertEqual(item["current_task_relation"], "task_changed")
                self.assertEqual(r.db.execute("SELECT state,event_id FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], "awaiting_stop")

    def test_current_revision_or_status_change_is_an_observed_boundary(self):
        for field, value in (("revision", 2), ("status", "active")):
            r, p = self.runtime()
            c, _ = self.phase(r, p)
            r.db.execute("UPDATE tasks SET " + field + "=? WHERE project=? AND conversation=?", (value, p, c))
            self.assertEqual(self.only(r, p)["reason"], "missing_host_stop_after_boundary")

    def test_delivered_write_fence_alone_is_not_a_missing_delivery(self):
        r, p = self.runtime()
        c, _ = self.phase(r, p)
        r.db.execute("UPDATE tasks SET write_version=write_version+1 WHERE project=? AND conversation=?", (p, c))
        item = self.only(r, p)
        self.assertEqual(item["current_task_relation"], "snapshot_advanced")
        self.assertEqual(item["reason"], "awaiting_host_stop")

    def test_original_matching_stop_without_request_is_local_seal_pending(self):
        r, p = self.runtime()
        c, phase = self.phase(r, p)
        self.stop(r, p, c)
        r.task_update(p, c, "Another task", new_task=True)
        item = self.only(r, p)
        self.assertEqual(item["reason"], "awaiting_local_seal")
        self.assertTrue(item["stop_observed"])
        self.assertFalse(item["request_pending"])
        self.assertIsNone(r.db.execute("SELECT event_id FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0])

    def test_exact_pending_request_reports_local_retry_without_reading_capture(self):
        for attempts, reason in ((0, "awaiting_local_seal"), (2, "local_seal_retry")):
            r, p = self.runtime()
            c, phase = self.phase(r, p)
            stop = self.stop(r, p, c)
            self.request(r, phase, stop, attempts)
            item = self.only(r, p)
            self.assertEqual(item["reason"], reason)
            self.assertEqual(item["attempts"], attempts)
            self.assertTrue(item["request_pending"])

    def test_later_child_active_and_wrong_version_stops_cannot_be_borrowed(self):
        r, p = self.runtime()
        c, phase = self.phase(r, p)
        self.stop(r, p, c, agent_id="child")
        self.stop(r, p, c, task_write_version=phase["delivery_version"] + 1)
        self.stop(r, p, c, task_status="active")
        r.task_update(p, c, "Later task", new_task=True)
        self.stop(r, p, c)
        item = self.only(r, p)
        self.assertEqual(item["reason"], "missing_host_stop_after_boundary")
        self.assertFalse(item["stop_observed"])

    def test_wrong_request_or_stop_binding_remains_unknown(self):
        for mismatch in ("request", "stop"):
            r, p = self.runtime()
            c, phase = self.phase(r, p)
            stop = self.stop(r, p, c)
            self.request(r, phase, stop)
            if mismatch == "request":
                r.db.execute("UPDATE phase_seal_requests SET governance_epoch=governance_epoch+1")
            else:
                r.db.execute("UPDATE events SET task_write_version=task_write_version+1 WHERE id=?", (stop["id"],))
            item = self.only(r, p)
            self.assertEqual(item["reason"], "boundary_unverified")
            self.assertIsNone(item["stop_observed"])

    def test_historical_epoch_does_not_borrow_current_receipt(self):
        for field in ("session_epoch", "governance_epoch"):
            r, p = self.runtime()
            c, _ = self.phase(r, p)
            self.stop(r, p, c)
            r.db.execute("UPDATE review_phases SET " + field + "=" + field + "+1")
            item = self.only(r, p)
            self.assertEqual(item["reason"], "historical_boundary")
            self.assertIsNone(item["stop_observed"])

    def test_project_and_host_scope_are_isolated(self):
        r, p = self.runtime()
        _, phase = self.phase(r, p)
        other_project = r.project(self.root / "other")
        self.assertEqual(delivery_boundary_diagnostics(r, other_project)["sampled"], 0)
        r.db.execute("UPDATE review_phases SET host_scope='dsh:default' WHERE id=?", (phase["id"],))
        self.assertEqual(delivery_boundary_diagnostics(r, p)["sampled"], 0)

    def test_tail_limit_returns_unknown_instead_of_false_missing_stop(self):
        r, p = self.runtime()
        c, _ = self.phase(r, p)
        self.stop(r, p, c)
        r.task_update(p, c, "Later task", new_task=True)
        for n in range(EVENT_SCAN_LIMIT + 5):
            r._event(p, c, "PostToolUse", {"ordinal": n})
        value = delivery_boundary_diagnostics(r, p)
        self.assertEqual(value["unknown_count"], 1)
        self.assertEqual(value["items"][0]["reason"], "stop_lookup_limited")
        self.assertIsNone(value["items"][0]["stop_observed"])

    def test_later_phase_inside_covered_tail_can_still_be_decisive(self):
        r, p = self.runtime()
        c, _ = self.phase(r, p)
        for n in range(EVENT_SCAN_LIMIT + 5):
            r._event(p, c, "PostToolUse", {"ordinal": n})
        _, phase = self.phase(r, p)
        self.stop(r, p, c)
        value = delivery_boundary_diagnostics(r, p)
        recent = next(item for item in value["items"] if item["phase_id"] == phase["id"])
        self.assertEqual(recent["reason"], "awaiting_local_seal")

    def test_samples_are_bounded_and_counts_are_not_global_totals(self):
        r, p = self.runtime()
        for n in range(SAMPLE_LIMIT + 4):
            self.phase(r, p, "chat-" + str(n))
        value = delivery_boundary_diagnostics(r, p)
        self.assertTrue(value["truncated"])
        self.assertEqual(value["sampled"], SAMPLE_LIMIT)
        self.assertEqual(sum(value["counts"].values()), SAMPLE_LIMIT)
        self.assertEqual(value["counts_scope"], "sample")

    def test_reads_never_access_public_text_goals_or_captured_payload(self):
        r, p = self.runtime()
        c, phase = self.phase(r, p)
        self.request(r, phase, self.stop(r, p, c), 1)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and ((table == "events" and column == "payload")
                    or (table == "tasks" and column in {"goal", "scope"})
                    or (table == "phase_seal_requests" and column == "capture")):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        r.db.set_authorizer(authorize)
        r.db.execute("PRAGMA query_only=ON")
        try:
            self.assertEqual(self.only(r, p)["reason"], "local_seal_retry")
        finally:
            r.db.set_authorizer(None)
            r.db.execute("PRAGMA query_only=OFF")

    def test_unavailable_diagnostics_do_not_break_optional_projection(self):
        r, p = self.runtime()
        self.phase(r, p)
        r.db.execute("DROP TABLE phase_seal_requests")
        value = delivery_boundary_diagnostics(r, p)
        self.assertFalse(value["checked"])
        self.assertEqual(value["reason"], "diagnostics_unavailable")
        self.assertIsNone(value["unknown_count"])
        self.assertEqual(value["items"], [])

    def test_inconsistent_or_malformed_awaiting_metadata_is_unknown(self):
        for field, value in (("event_id", "unexpected-already-set"), ("task_revision", "not-a-version")):
            r, p = self.runtime()
            self.phase(r, p)
            r.db.execute("UPDATE review_phases SET " + field + "=?", (value,))
            item = self.only(r, p)
            self.assertEqual(item["reason"], "boundary_unverified")
            self.assertIsNone(item["stop_observed"])

    def test_large_backlog_and_event_history_fit_a_bounded_sql_work_budget(self):
        r, p = self.runtime()
        c, original = self.phase(r, p)
        columns = list(original)
        with r.transaction():
            r.db.executemany("INSERT INTO review_phases(" + ",".join(columns) + ") VALUES("
                + ",".join("?" for _ in columns) + ")", [
                    tuple(({**original, "id": "old-phase-" + str(n), "task_id": "old-task-" + str(n),
                            "created": original["created"] - 1 - n})[key] for key in columns)
                    for n in range(2500)])
            r.db.executemany("INSERT INTO events(id,project,conversation,type,outcome,payload,created) "
                "VALUES(?,?,?,'PostToolUse','unknown','{}',?)", [
                    ("history-" + str(n), p, c, original["created"] + n + 1) for n in range(2500)])
        work = 0
        def bounded():
            nonlocal work
            work += 100
            return work > 50000
        r.db.set_progress_handler(bounded, 100)
        before = r.db.total_changes
        try:
            value = delivery_boundary_diagnostics(r, p)
        finally:
            r.db.set_progress_handler(None, 0)
        self.assertTrue(value["checked"], value)
        self.assertTrue(value["truncated"])
        self.assertEqual(value["sampled"], SAMPLE_LIMIT)
        self.assertEqual(value["unknown_count"], SAMPLE_LIMIT)
        self.assertLessEqual(work, 50000)
        self.assertEqual(r.db.total_changes, before)

    def test_light_status_never_runs_boundary_scan(self):
        r, p = self.runtime()
        self.phase(r, p)
        with patch("agent_learning.phase_boundary_diagnostics.delivery_boundary_diagnostics",
                   side_effect=AssertionError("light settings cannot run boundary diagnostics")):
            self.assertNotIn("delivery_boundary_diagnostics", status(r, p, include_health=False))
        self.assertIn("delivery_boundary_diagnostics", status(r, p))


if __name__ == "__main__":
    unittest.main()
