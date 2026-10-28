"""Upgrade diagnostics are bounded, shared, resumable and never governance.

All records are synthetic and all homes come from the caller's isolated TMP.
No host process, administrator executor or model is started by these checks.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning import knowledge_health as health
from agent_learning.runtime import Runtime


class KnowledgeHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="alp-knowledge-health-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        # A registered, existing project models an upgrade with retained data.
        # Avoid treating an empty newly-created project's completed check as
        # an obligation to rescan when ordinary new knowledge is later added.
        self.p = "knowledge-health-existing-project"
        self.r.db.execute("INSERT INTO projects(id) VALUES(?)", (self.p,))
        self.sequence = 0

    def publish(self, *, project=None, title=None, content=None):
        project = project or self.p
        self.sequence += 1
        title = title or "Retained project fact " + str(self.sequence)
        problem = self.r.feedback(project, "health-source-" + str(self.sequence),
                                  title, category="project")
        refs = [row[0] for row in self.r.db.execute(
            "SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(project, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title,
            content=content or "Conclusion: preserve project identity and observable sources.",
            category="project", knowledge_kind="fact", applicability="This project and source observation",
            limitations="Source statement; factual verification remains separate",
            evidence_refs=refs, validation_level="reported", metadata={})
        return self.r.publish(project, candidate["candidate_id"])

    def finish(self, *, runtime=None, project=None, max_items=2):
        runtime, project = runtime or self.r, project or self.p
        for _ in range(80):
            health.advance(runtime, project, max_items=max_items, max_seconds=.25)
            result = health.report(runtime.db, project, limit=50)
            if result["state"] == "completed":
                return result
        self.fail("bounded health batches never reached a completed checkpoint")

    def body(self, record):
        return json.loads(self.r.db.execute(
            "SELECT body FROM knowledge WHERE project=? AND id=? AND revision=?",
            (self.p, record["knowledge_id"], record["revision"])).fetchone()[0])

    def source(self, outcomes, *, name="health-recovery"):
        events = []
        for number, outcome in enumerate(outcomes):
            event, _ = self.r._event(self.p, name, "PostToolUse",
                {"tool_name": "Bash", "tool_input": {"command": "python health_fixture.py"},
                 "tool_response": {"exit_code": 0 if outcome == "succeeded" else 1}},
                event_id=name + "-" + str(number).zfill(4), outcome=outcome)
            events.append(event)
        problem = self.r._problem(self.p, name, name, "operation", [row["id"] for row in events])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title=name,
            content="Conclusion: preserve the order of the cited source observations.",
            category="operation", knowledge_kind="solution", applicability="Only the cited source and command",
            limitations="A different source's success cannot prove this source recovered.",
            evidence_refs=[row["id"] for row in events], validation_level="reported", metadata={})
        return self.r.publish(self.p, candidate["candidate_id"]), events

    def replace_body(self, record, body):
        self.r.db.execute("UPDATE knowledge SET body=? WHERE project=? AND id=? AND revision=?",
            (json.dumps(body), self.p, record["knowledge_id"], record["revision"]))

    def authority(self):
        tables = ("knowledge", "active", "knowledge_fingerprints", "knowledge_sources", "candidates",
                  "events", "problems", "knowledge_actions", "governance_proposals", "admin_calls",
                  "publication_receipts", "projects")
        return {table: [tuple(row) for row in self.r.db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in tables}

    def test_three_hosts_share_one_completed_check_for_the_same_project(self):
        for _ in range(3):
            self.publish()
        first = self.finish()
        self.assertEqual(first["scanned"], 3)
        for host in ("dsh", "antigravity", "codex"):
            with self.subTest(host=host):
                runtime = Runtime(self.home, host=host)
                try:
                    before = runtime.db.total_changes
                    health.advance(runtime, self.p, max_items=1, max_seconds=.25)
                    self.assertEqual(runtime.db.total_changes, before)
                    after = health.report(runtime.db, self.p, limit=50)
                    self.assertEqual(after, first)
                finally:
                    runtime.close()

    def test_interrupted_scan_resumes_from_the_shared_checkpoint(self):
        for _ in range(5):
            self.publish()
        health.advance(self.r, self.p, max_items=1, max_seconds=.25)
        first = health.report(self.r.db, self.p, limit=50)
        self.assertEqual(first["state"], "checking")
        self.assertEqual(first["scanned"], 1)
        runtime = Runtime(self.home, host="antigravity")
        try:
            final = self.finish(runtime=runtime, max_items=1)
        finally:
            runtime.close()
        self.assertEqual(final["scanned"], 5)
        self.assertEqual(final["started"], first["started"])
        self.assertIsNotNone(final["completed"])

    def test_one_call_cannot_exceed_its_item_budget(self):
        for _ in range(4):
            self.publish()
        health.advance(self.r, self.p, max_items=2, max_seconds=.25)
        first = health.report(self.r.db, self.p)
        self.assertLessEqual(first["scanned"], 2)
        self.assertEqual(first["state"], "checking")
        self.assertEqual(self.finish()["scanned"], 4)

    def test_project_reopen_automatically_advances_retained_knowledge(self):
        self.publish()
        workspace = self.root / "project 中文"
        workspace.mkdir()
        self.r.config["project_aliases"] = {str(workspace): self.p}
        self.assertEqual(self.r.project(workspace), self.p)
        result = health.report(self.r.db, self.p)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["scanned"], 1)

    def test_concurrent_hosts_do_not_count_a_knowledge_record_twice(self):
        for _ in range(7):
            self.publish()
        barrier = threading.Barrier(3)

        def run(host):
            runtime = Runtime(self.home, host=host)
            try:
                barrier.wait(timeout=10)
                for _ in range(6):
                    health.maybe_advance(runtime, self.p)
            finally:
                runtime.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(run, ("codex", "antigravity", "dsh")))
        result = self.finish(max_items=1)
        self.assertEqual(result["scanned"], 7)
        identities = [(row["knowledge_id"], row["revision"]) for row in result["items"]]
        self.assertEqual(len(identities), len(set(identities)))

    def test_projects_and_homes_have_independent_checkpoints(self):
        self.publish()
        self.r.db.execute("INSERT INTO projects(id) VALUES('other-project')")
        self.publish(project="other-project")
        self.publish(project="other-project")
        self.assertEqual(self.finish()["scanned"], 1)
        self.assertEqual(health.report(self.r.db, "other-project")["state"], "not_started")
        self.assertEqual(self.finish(project="other-project")["scanned"], 2)
        other = Runtime(self.root / "other-home")
        try:
            other.db.execute("INSERT INTO projects(id) VALUES(?)", (self.p,))
            result = health.report(other.db, self.p)
            self.assertEqual(result["state"], "not_started")
            self.assertEqual(self.finish(runtime=other)["scanned"], 0)
        finally:
            other.close()

    def test_new_rule_runs_once_and_old_rule_does_not_overwrite_it(self):
        self.publish()
        first = self.finish()
        previous_rule = health.RULE_VERSION
        with patch.object(health, "RULE_VERSION", previous_rule + 1):
            updated = self.finish()
            self.assertEqual(updated["rule_version"], previous_rule + 1)
            self.assertEqual(updated["scanned"], first["scanned"])
            before = self.r.db.total_changes
            health.advance(self.r, self.p)
            self.assertEqual(self.r.db.total_changes, before)
        before = self.r.db.total_changes
        health.advance(self.r, self.p)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(health.report(self.r.db, self.p)["rule_version"], previous_rule + 1)

    def test_health_check_does_not_modify_knowledge_permissions_or_admin_state(self):
        record = self.publish()
        original = self.body(record)
        original.pop("knowledge_kind")
        original["category"] = "unknown"
        original["applicability"] = ""
        self.replace_body(record, original)
        before, config = self.authority(), json.dumps(self.r.config, sort_keys=True)
        result = self.finish()
        self.assertEqual(result["affected"], 1)
        self.assertTrue(result["items"][0]["issues"])
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.body(record), original)
        self.assertEqual(json.dumps(self.r.config, sort_keys=True), config)
        self.assertEqual(original["validation_level"], "reported")

    def test_report_is_readonly_and_cannot_start_or_advance_a_check(self):
        for _ in range(3):
            self.publish()
        before = self.r.db.total_changes
        self.assertEqual(health.report(self.r.db, self.p)["state"], "not_started")
        self.assertEqual(self.r.db.total_changes, before)
        health.advance(self.r, self.p, max_items=1, max_seconds=.25)
        before = self.r.db.total_changes
        for _ in range(3):
            self.assertEqual(health.report(self.r.db, self.p)["scanned"], 1)
        self.assertEqual(self.r.db.total_changes, before)

    def test_revoked_knowledge_is_not_resurrected_or_scanned(self):
        keep = self.publish()
        removed = self.publish()
        self.r.revoke(self.p, removed["knowledge_id"], removed["revision"])
        before = self.authority()
        result = self.finish()
        self.assertEqual(result["scanned"], 1)
        self.assertNotIn(removed["knowledge_id"], [row["knowledge_id"] for row in result["items"]])
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.r.db.execute("SELECT id FROM active WHERE project=?", (self.p,)).fetchone()[0],
                         keep["knowledge_id"])

    def test_later_publication_marks_snapshot_old_without_restarting_same_rule(self):
        self.publish()
        first = self.finish()
        self.assertTrue(first["snapshot_current"])
        self.publish()
        updated = health.report(self.r.db, self.p)
        self.assertFalse(updated["snapshot_current"])
        health.advance(self.r, self.p)
        self.assertEqual(health.report(self.r.db, self.p)["scanned"], 1)

    def test_reference_grade_and_unknown_classification_are_not_rewritten_as_verified(self):
        record = self.publish(content="The source claims an unverified project convention.")
        body = self.body(record)
        body["category"] = "unknown"
        self.replace_body(record, body)
        result = self.finish()
        self.assertEqual(result["affected"], 1)
        self.assertEqual(self.body(record)["category"], "unknown")
        self.assertEqual(self.body(record)["validation_level"], "reported")
        self.assertFalse(self.r.config["admin"]["enabled"])
        self.assertFalse(self.r.config["admin"]["auto_publish"])

    def test_missing_source_is_reported_without_revoking_the_retained_record(self):
        record = self.publish()
        self.r.db.execute("DELETE FROM knowledge_sources WHERE project=? AND knowledge_id=?",
                          (self.p, record["knowledge_id"]))
        body = self.body(record)
        body["evidence_refs"] = ["missing-synthetic-event"]
        self.replace_body(record, body)
        result = self.finish()
        self.assertEqual(result["affected"], 1)
        self.assertTrue(result["items"][0]["issues"])
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM active WHERE project=? AND id=?",
                                               (self.p, record["knowledge_id"])).fetchone())

    def test_exact_duplicate_detection_does_not_merge_distinct_identifiers(self):
        record = self.publish()
        original = self.r.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? AND revision=?",
            (self.p, record["knowledge_id"], record["revision"])).fetchone()
        duplicate_id = "synthetic-legacy-duplicate"
        self.r.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (
            self.p, duplicate_id, 1, original["body"], original["created"]))
        self.r.db.execute("INSERT INTO active VALUES(?,?,1)", (self.p, duplicate_id))
        self.r.db.execute("INSERT INTO knowledge_fingerprints SELECT project,?,1,fingerprint "
            "FROM knowledge_fingerprints WHERE project=? AND id=? AND revision=?",
            (duplicate_id, self.p, record["knowledge_id"], record["revision"]))
        before = self.authority()
        result = self.finish()
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["affected"], 2)
        self.assertEqual(self.authority(), before)

    def test_busy_database_defers_the_optional_check_without_changing_the_checkpoint(self):
        self.publish()
        lock = sqlite3.connect(self.home / "runtime.sqlite3", isolation_level=None)
        try:
            lock.execute("BEGIN IMMEDIATE")
            health.maybe_advance(self.r, self.p)
            self.assertEqual(health.report(self.r.db, self.p)["state"], "not_started")
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(self.finish()["scanned"], 1)

    def test_optional_check_never_joins_an_existing_authoritative_transaction(self):
        self.publish()
        with self.r.transaction():
            self.r.db.execute("INSERT INTO metrics VALUES(?, 'health-transaction-probe', 1)", (self.p,))
            health.maybe_advance(self.r, self.p)
            self.assertTrue(self.r.db.in_transaction)
            self.assertEqual(health.report(self.r.db, self.p)["state"], "not_started")
        self.assertEqual(self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name=?",
            (self.p, "health-transaction-probe")).fetchone()[0], 1)
        self.assertEqual(self.finish()["scanned"], 1)

    def test_report_pagination_is_stable_bounded_and_does_not_repeat_items(self):
        for _ in range(5):
            record = self.publish()
            body = self.body(record)
            body["category"] = "unknown"
            self.replace_body(record, body)
        final = self.finish()
        seen, offset = [], 0
        while offset is not None:
            page = health.report(self.r.db, self.p, offset=offset, limit=2)
            self.assertLessEqual(len(page["items"]), 2)
            seen.extend(row["knowledge_id"] for row in page["items"])
            offset = page["next_offset"]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        self.assertEqual(final["affected"], 5)
        summary = health.report(self.r.db, self.p, limit=0)
        self.assertEqual(summary["items"], [])
        self.assertIsNone(summary["next_offset"])
        self.assertEqual(summary["affected"], 5)

    def test_older_readonly_database_returns_not_started_without_schema_mutation(self):
        legacy = sqlite3.connect(":memory:", isolation_level=None)
        legacy.row_factory = sqlite3.Row
        try:
            legacy.execute("PRAGMA query_only=ON")
            before = legacy.total_changes
            result = health.report(legacy, self.p)
            self.assertEqual(result["state"], "not_started")
            self.assertEqual(result["scanned"], 0)
            self.assertEqual(legacy.total_changes, before)
            self.assertEqual(legacy.execute("SELECT name FROM sqlite_master").fetchall(), [])
        finally:
            legacy.close()

    def test_a_failed_page_is_rolled_back_and_can_resume_without_double_counting(self):
        for _ in range(3):
            self.publish()
        original = health._issues
        calls = []

        def fail_after_one(runtime, project, row):
            calls.append(row["id"])
            if len(calls) == 2:
                raise sqlite3.OperationalError("synthetic source-query failure")
            return original(runtime, project, row)

        timeout = self.r.db.execute("PRAGMA busy_timeout").fetchone()[0]
        with patch.object(health, "_issues", side_effect=fail_after_one):
            result = health.maybe_advance(self.r, self.p)
        self.assertEqual(result["state"], "deferred")
        self.assertFalse(self.r.db.in_transaction)
        self.assertEqual(self.r.db.execute("PRAGMA busy_timeout").fetchone()[0], timeout)
        self.assertEqual(health.report(self.r.db, self.p)["state"], "not_started")
        self.assertEqual(self.finish()["scanned"], 3)

    def test_budget_interrupted_first_item_is_recorded_as_partial_and_does_not_stall_later_items(self):
        for _ in range(3):
            self.publish()
        with patch.object(health, "_issues", side_effect=sqlite3.OperationalError("interrupted")) as inspect:
            health.advance(self.r, self.p, max_items=1, max_seconds=.25)
            inspect.assert_called_once()
        first = health.report(self.r.db, self.p)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["state"], "checking")
        self.assertEqual(first["issue_counts"].get("check_budget_partial"), 1)
        final = self.finish(max_items=1)
        self.assertEqual(final["scanned"], 3)
        self.assertEqual(final["issue_counts"].get("check_budget_partial"), 1)
        self.assertEqual(final["items"][0]["knowledge_id"], first["items"][0]["knowledge_id"])

    def test_budget_interruption_commits_a_completed_prefix_without_losing_its_checkpoint(self):
        for _ in range(3):
            self.publish()
        original, inspected = health._issues, []

        def interrupt_second(runtime, project, row):
            inspected.append(row["id"])
            if len(inspected) == 2:
                raise sqlite3.OperationalError("interrupted")
            return original(runtime, project, row)

        with patch.object(health, "_issues", side_effect=interrupt_second):
            health.advance(self.r, self.p, max_items=2, max_seconds=.25)
        prefix = health.report(self.r.db, self.p)
        self.assertEqual(prefix["scanned"], 2)
        self.assertEqual(prefix["issue_counts"].get("check_budget_partial"), 1)
        final = self.finish(max_items=1)
        self.assertEqual(final["scanned"], 3)
        self.assertEqual(final["started"], prefix["started"])

    def test_budget_expired_before_first_inspection_still_advances_a_partial_checkpoint(self):
        for _ in range(3):
            self.publish()
        clock = iter((0.0, 1.0))
        with patch.object(health.time, "monotonic", side_effect=lambda: next(clock, 1.0)), \
                patch.object(health, "_issues", side_effect=AssertionError("expired batch must not inspect")):
            health.advance(self.r, self.p, max_items=2, max_seconds=.25)
        first = health.report(self.r.db, self.p)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["issue_counts"].get("check_budget_partial"), 1)
        self.assertEqual(self.finish()["scanned"], 3)

    def test_recovered_source_is_not_flagged_as_an_unresolved_failure(self):
        self.source(("failed", "succeeded"))
        result = self.finish()
        self.assertEqual(result["scanned"], 1)
        self.assertNotIn("unresolved_source_failure", result["issue_counts"])
        self.assertEqual(result["affected"], 0)

    def test_later_failure_in_the_same_source_remains_a_risk(self):
        record, _ = self.source(("failed", "succeeded", "failed"))
        result = self.finish()
        self.assertEqual(result["issue_counts"].get("unresolved_source_failure"), 1)
        self.assertEqual(result["items"][0]["knowledge_id"], record["knowledge_id"])
        self.assertEqual(self.body(record)["validation_level"], "reported")

    def test_truncated_source_with_recovery_outside_budget_is_unknown_not_unresolved(self):
        self.source(("failed",) * (health.MAX_REFS + 1) + ("succeeded",))
        result = self.finish()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["issue_counts"].get("source_check_partial"), 1)
        self.assertEqual(result["issue_counts"].get("source_observed_failure_recovery_unknown"), 1)
        self.assertNotIn("unresolved_source_failure", result["issue_counts"])

    def test_revoked_item_in_a_completed_report_is_marked_as_historical(self):
        record = self.publish()
        body = self.body(record)
        body["category"] = "unknown"
        self.replace_body(record, body)
        self.finish()
        self.r.revoke(self.p, record["knowledge_id"], record["revision"])
        report = health.report(self.r.db, self.p)
        self.assertFalse(report["snapshot_current"])
        self.assertFalse(report["items"][0]["current"])

    def test_bad_legacy_body_does_not_abort_other_records(self):
        broken, normal = self.publish(), self.publish()
        self.r.db.execute("UPDATE knowledge SET body='not-json' WHERE project=? AND id=?",
                          (self.p, broken["knowledge_id"]))
        before = self.authority()
        result = self.finish()
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["issue_counts"].get("body_invalid"), 1)
        self.assertEqual(self.authority(), before)
        self.assertIsNotNone(self.r.read(self.p, normal["knowledge_id"]))


if __name__ == "__main__":
    unittest.main()
