"""历史吸收的真实隔离 SQLite、权限与模型回执检查，不调用外部模型。"""
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_governance import propose_governance
from agent_learning.admin_queue import claim_dispatch
from agent_learning.history_import import install_storage
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class HistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-history-import-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.document = self.workspace / "README.md"
        self.document.write_text("# Agent project notes\nUse local SQLite for project memory.\n", encoding="utf-8")
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)

    def prepare(self, **kwargs):
        return self.r.history_prepare(self.p, self.workspace, **kwargs)["job_id"]

    def materialize(self, **kwargs):
        job = self.prepare(**kwargs)
        self.r.history_run(self.p, job)
        return job

    def case(self, job):
        return self.r.db.execute("SELECT p.* FROM problems p JOIN history_case_links l ON l.problem_id=p.id AND l.project=p.project WHERE l.job_id=? ORDER BY l.unit_id LIMIT 1", (job,)).fetchone()

    def candidate(self, job):
        case = self.case(job)
        packet = self.r.review_packet(self.p, case["id"])
        return self.r.submit_candidate(self.p, problem_id=case["id"], problem_revision=case["revision"],
                                       title="Project fact", content="Project documentation states local SQLite is used.",
                                       category="project", knowledge_kind="fact", applicability="This source revision",
                                       limitations="Historical reported text, not a verified current implementation.",
                                       evidence_refs=[e["id"] for e in packet["events"]], validation_level="reported")

    def test_prepare_only_freezes_sources_without_model_or_case(self):
        with patch("agent_learning.admin.subprocess.Popen") as model:
            job = self.prepare()
        model.assert_not_called()
        self.assertEqual(self.r.history_status(self.p, job)["job"]["state"], "prepared")
        for table in ("events", "problems", "candidates", "active", "contexts", "session_projects", "tasks"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)

    def test_snapshot_survives_source_deletion_and_materializes_reported(self):
        job = self.prepare()
        self.document.unlink()
        self.r.history_run(self.p, job)
        case = self.case(job)
        self.assertIsNone(case["task_id"])
        event = self.r.db.execute("SELECT * FROM events WHERE type='HistoryImport'").fetchone()
        self.assertEqual(event["outcome"], "reported")
        for field in ("task_id", "task_revision", "assignment_id", "operation", "agent"):
            self.assertIsNone(event[field])
        self.assertIn("SQLite", event["payload"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM session_projects").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_repeated_prepare_and_run_are_idempotent(self):
        job = self.materialize()
        before = [self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("events", "problems", "history_units")]
        self.assertEqual(self.prepare(), job)
        self.r.history_run(self.p, job)
        self.assertEqual(before, [self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("events", "problems", "history_units")])

    def test_changed_source_creates_new_version_preserving_original(self):
        original = self.materialize()
        self.document.write_text("# Agent project notes\nNew storage documentation.\n", encoding="utf-8")
        newer = self.materialize()
        self.assertNotEqual(original, newer)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM history_materials").fetchone()[0], 2)
        self.assertNotEqual(self.case(original)["id"], self.case(newer)["id"])

    def test_normal_enabled_admin_does_not_select_history(self):
        job = self.materialize()
        self.r.config["admin"]["enabled"] = True
        with patch("agent_learning.admin.subprocess.Popen") as model:
            self.assertIsNone(claim_dispatch(self.r, self.p))
            self.assertEqual(admin_once(self.r, self.p)["status"], "idle")
            self.assertEqual(admin_once(self.r, self.p, self.case(job)["id"])["status"], "idle")
        model.assert_not_called()

    def test_explicit_review_works_with_global_admin_off_and_never_publishes(self):
        job = self.prepare()
        self.r.config["admin"]["auto_publish"] = True
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            result = self.r.history_run(self.p, job, allow_review=True)
            self.assertEqual(model.call_count, 1)
        self.assertFalse(self.r.config["admin"]["enabled"])
        self.assertEqual(result["job"]["counts"]["candidates"], 1)
        self.assertEqual(result["job"]["counts"]["published_candidates"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(result["job"]["counts"]["calls"], 1)

    def test_reviewed_material_reuses_receipt_without_new_model(self):
        job = self.prepare()
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            self.r.history_run(self.p, job, allow_review=True)
            self.r.history_run(self.p, job, allow_review=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.r.history_status(self.p, job)["job"]["counts"]["calls"], 1)

    def test_partial_conversation_can_produce_reported_candidate(self):
        transcript = Path(self.temp.name) / "conversation.json"
        transcript.write_text(json.dumps({"messages": [{"role": "assistant", "content": "The past design used SQLite.", "reasoning": "Excluded internal text"}]}), encoding="utf-8")
        self.document.unlink()
        job = self.prepare(extra_sources=[{"path": str(transcript), "kind": "conversation"}])
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            result = self.r.history_run(self.p, job, allow_review=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["job"]["counts"]["candidates"], 1)
        event = self.r.db.execute("SELECT * FROM events WHERE type='HistoryImport'").fetchone()
        self.assertNotIn("Excluded internal text", event["payload"])
        payload = json.loads(event["payload"])
        self.assertEqual(payload["source"]["coverage"], "partial")
        self.assertNotIn("truncated", payload)
        case = self.case(job)
        refs = [e["id"] for e in self.r.review_packet(self.p, case["id"])["events"]]
        with self.assertRaises(ContractError):
            self.r.submit_candidate(self.p, problem_id=case["id"], problem_revision=case["revision"],
                                    title="Claim", content="Historical text", category="project", applicability="visible text",
                                    limitations="reported", evidence_refs=refs, validation_level="tool_success")

    def test_needs_evidence_preserves_manual_state_and_counts(self):
        job = self.prepare()
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("needs_evidence")):
            result = self.r.history_run(self.p, job, allow_review=True)
        self.assertEqual(result["job"]["state"], "materialized")
        self.assertEqual(result["job"]["counts"]["pending_cases"], 1)
        self.assertEqual(result["job"]["counts"]["awaiting_evidence_cases"], 1)
        self.assertEqual(result["job"]["counts"]["review_decision_cases"], 1)
        self.assertEqual(result["job"]["counts"]["completed_review_cases"], 0)
        self.assertEqual(result["job"]["last_result"]["reason"], "needs_evidence")

    def test_skipped_and_pending_reports_keep_items_after_first_forty(self):
        for index in range(45):
            (self.workspace / (str(index) + ".unparsed")).write_text("unknown format", encoding="utf-8")
        job = self.prepare()
        for section in ("pending", "skipped"):
            page = self.r.history_source_report(self.p, job, section=section, offset=40, limit=5)
            self.assertEqual(page["total"], 45)
            self.assertEqual(len(page["items"]), 5)
            self.assertIsNone(page["next_offset"])
        source = self.r.history_source_report(self.p, job)["items"][0]
        self.assertNotIn("content", source)
        self.assertIn("content_hash", source)

    def test_event_locator_identifies_exact_source_chunk_characters(self):
        self.document.write_text("SQLite design notes. " * 250, encoding="utf-8")
        job = self.materialize()
        source = json.loads(self.r.db.execute("SELECT body FROM history_materials LIMIT 1").fetchone()[0])
        for row in self.r.db.execute("SELECT payload FROM events WHERE type='HistoryImport'"):
            payload = json.loads(row[0])
            locator = payload["locator"]
            text = source["chunks"][locator["chunk_index"]]["text"]
            self.assertEqual(payload["content"], text[locator["char_offset"]:locator["char_offset"] + locator["char_length"]])
        self.assertLess(self.r.history_status(self.p, job)["job"]["counts"]["cases"], self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_redacted_url_marker_is_not_split_or_changed_at_1496(self):
        self.document.write_text("a" * 1482 + "?access_token=private-value " + "z" * 1000, encoding="utf-8")
        job = self.materialize()
        source = json.loads(self.r.db.execute("SELECT body FROM history_materials LIMIT 1").fetchone()[0])
        self.assertEqual(source["content"].index("[REDACTED]"), 1496)
        parts = []
        for row in self.r.db.execute("SELECT payload FROM events WHERE type='HistoryImport' ORDER BY rowid"):
            payload = json.loads(row[0])
            locator = payload["locator"]
            self.assertEqual(len(payload["content"]), locator["char_length"])
            text = source["chunks"][locator["chunk_index"]]["text"]
            self.assertEqual(payload["content"], text[locator["char_offset"]:locator["char_offset"] + locator["char_length"]])
            parts.append(payload["content"])
        self.assertEqual("".join(parts), source["content"])
        self.assertEqual(self.r.history_status(self.p, job)["job"]["counts"]["cases"], 1)

    def test_cancel_between_units_keeps_committed_progress_without_locking_file(self):
        from agent_learning.history_import import _unit_groups
        self.document.write_text("SQLite design notes. " * 700, encoding="utf-8")
        job = self.prepare()
        other = Runtime(self.home, config={})
        self.addCleanup(other.close)
        def interrupt(source):
            for index, group in enumerate(_unit_groups(source)):
                if index == 1:
                    other.history_cancel(self.p, job)
                yield group
        with patch("agent_learning.history_import._unit_groups", interrupt):
            with self.assertRaises(ContractError):
                self.r.history_run(self.p, job)
        state = self.r.history_status(self.p, job)["job"]
        self.assertEqual(state["state"], "cancelled")
        self.assertEqual(state["counts"]["cases"], 1)
        self.assertEqual(state["counts"]["materialized_sources"], 0)

    def test_direct_history_executor_requires_current_job_approval(self):
        job = self.materialize()
        with self.assertRaises(ContractError):
            admin_once(self.r, self.p, self.case(job)["id"], _history_job_id=job)

    def test_expired_review_run_requires_manual_resume_before_candidate(self):
        job = self.materialize()
        self.r.db.execute("UPDATE history_jobs SET state='reviewing',review_enabled=1,owner='expired',lease_until=0 WHERE id=?", (job,))
        with self.assertRaises(ContractError):
            self.candidate(job)
        self.assertTrue(self.r.history_status(self.p, job)["job"]["resume_required"])
        self.r.history_run(self.p, job)
        self.candidate(job)

    def test_unknown_model_call_is_not_automatically_retried(self):
        import subprocess
        job = self.prepare()
        def timeout(packet, process):
            raise subprocess.TimeoutExpired("controlled-reviewer", 1)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip", timeout)) as model:
            first = self.r.history_run(self.p, job, allow_review=True)
            self.r.history_run(self.p, job, allow_review=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(first["job"]["state"], "paused")
        self.assertEqual(first["job"]["last_result"]["reason"], "timeout_unknown")
        self.assertEqual(self.r.history_status(self.p, job)["job"]["counts"]["calls"], 1)

    def test_paused_cancelled_and_account_changed_block_manual_candidate(self):
        job = self.materialize()
        self.r.history_pause(self.p, job)
        with self.assertRaises(ContractError):
            self.candidate(job)
        self.r.history_run(self.p, job)
        self.r.account_changed()
        with self.assertRaises(ContractError):
            self.candidate(job)
        self.r.history_run(self.p, job)
        self.candidate(job)
        self.r.history_cancel(self.p, job)
        with self.assertRaises(ContractError):
            self.candidate(job)
        with self.assertRaises(ContractError):
            self.r.history_run(self.p, job)

    def test_cancel_during_model_rejects_candidate_and_preserves_fence(self):
        job = self.prepare()
        def cancel(packet, process):
            self.r.history_cancel(self.p, job)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate", cancel)):
            result = self.r.history_run(self.p, job, allow_review=True)
        self.assertEqual(result["job"]["state"], "cancelled")
        self.assertEqual(result["job"]["counts"]["candidates"], 0)

    def test_explicit_publish_of_reported_fact_and_cancel_keeps_knowledge(self):
        job = self.materialize()
        candidate = self.candidate(job)
        published = self.r.publish(self.p, candidate["candidate_id"])
        counts = self.r.history_status(self.p, job)["job"]["counts"]
        self.assertEqual(counts["candidate_cases"], 1)
        self.assertEqual(counts["published_cases"], 1)
        self.assertEqual(counts["untouched_cases"], 0)
        self.assertEqual(counts["review_decision_cases"], 0)
        self.assertEqual(counts["reviewed_cases"], 0)
        self.assertEqual(counts["pending_cases"], 1)
        self.r.history_cancel(self.p, job)
        self.assertEqual(self.r.read(self.p, published["knowledge_id"])["validation_level"], "reported")
        self.assertEqual(self.r.publish(self.p, candidate["candidate_id"]), published)

    def test_paused_job_blocks_new_publish_and_governance(self):
        job = self.materialize()
        candidate = self.candidate(job)
        case = self.case(job)
        self.r.history_pause(self.p, job)
        with self.assertRaises(ContractError):
            self.r.publish(self.p, candidate["candidate_id"])
        with self.assertRaises(ContractError):
            propose_governance(self.r, self.p, case["id"], case["revision"], "publish",
                               candidate_id=candidate["candidate_id"], reason="historical fact")

    def test_global_daily_quota_applies_to_explicit_history(self):
        job = self.prepare()
        self.r.config["admin"]["max_calls_per_day"] = 0
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.r.history_run(self.p, job, allow_review=True)
        model.assert_not_called()
        self.assertEqual(result["job"]["state"], "paused")
        self.assertEqual(result["job"]["last_result"]["reason"], "budget_exhausted")

    def test_launch_failure_refunds_only_job_not_started_claim(self):
        job = self.prepare()
        with patch("agent_learning.admin.subprocess.Popen", side_effect=OSError("controlled")):
            result = self.r.history_run(self.p, job, allow_review=True, max_calls=1)
        self.assertEqual(result["job"]["remaining_calls"], 1)
        self.assertEqual(result["job"]["counts"]["calls"], 0)
        self.assertEqual(result["job"]["state"], "paused")

    def test_shared_project_across_hosts_and_epoch_reapproval(self):
        job = self.materialize()
        other = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        self.assertEqual(other.project(self.workspace), self.p)
        self.assertEqual(other.history_status(self.p)["job"]["job_id"], job)
        other.account_changed()
        with self.assertRaises(ContractError):
            self.candidate(job)
        other.history_run(self.p, job)
        other.history_guard(self.p, self.case(job)["id"])

    def test_mcp_exposes_no_history_write_or_execution_tools(self):
        for role in ("worker", "admin"):
            server = Server(self.r, self.p, role)
            for name in ("history_prepare", "history_run", "history_pause", "history_cancel", "history_import", "history_approve"):
                self.assertNotIn(name, server.tools)

    def test_skip_is_persistent_without_project_scan(self):
        with patch("agent_learning.history_sources.scan_sources") as scan:
            self.r.history_skip(self.p)
        scan.assert_not_called()
        other = Runtime(self.home, config={})
        self.addCleanup(other.close)
        self.assertEqual(other.history_status(self.p)["onboarding"]["state"], "skipped")

    def test_job_case_paging_finds_current_material_beyond_global_old_queue(self):
        for index in range(100):
            (self.workspace / ("old-" + str(index) + ".md")).write_text("Previously documented project fact.", encoding="utf-8")
        old = self.materialize()
        self.r.history_pause(self.p, old)
        (self.workspace / "current.md").write_text("Current project fact.", encoding="utf-8")
        current = self.materialize(scan_options={"include": ["current.md"]})
        current_page = self.r.history_cases(self.p, current)
        self.assertEqual(current_page["total"], 1)
        item = current_page["items"][0]
        self.assertTrue(item["review_allowed"])
        self.assertEqual(item["revision"], self.case(current)["revision"])
        global_page = Server(self.r, self.p, "admin").call("problem_list", {"limit": 100})
        self.assertNotIn(item["problem_id"], [row["id"] for row in global_page])
        packet = self.r.review_packet(self.p, item["problem_id"], expected_revision=item["revision"])
        self.assertIn("Current project fact", json.dumps(packet))
        old_page = self.r.history_cases(self.p, old, offset=100, limit=10)
        self.assertEqual(old_page["total"], 101)
        self.assertEqual(len(old_page["items"]), 1)
        self.assertFalse(old_page["items"][0]["review_allowed"])
        with self.assertRaises(ContractError):
            self.r.review_packet(self.p, old_page["items"][0]["problem_id"])

    def test_case_paging_shows_manual_candidate_without_formal_review(self):
        job = self.materialize()
        self.assertEqual(self.r.history_status(self.p, job)["job"]["counts"]["untouched_cases"], 1)
        candidate = self.candidate(job)
        item = self.r.history_cases(self.p, job)["items"][0]
        self.assertIsNone(item["review"])
        self.assertEqual(item["candidate_count"], 1)
        self.assertEqual(item["candidates"][0]["candidate_id"], candidate["candidate_id"])
        with self.assertRaises(ContractError):
            self.r.history_cases(self.p, job, offset=-1)
        with self.assertRaises(ContractError):
            self.r.history_cases("another-project", job)

    def test_storage_requires_transaction_and_rolls_back(self):
        import sqlite3
        class Minimal:
            db = sqlite3.connect(":memory:")
        with self.assertRaises(ContractError):
            install_storage(Minimal())
        Minimal.db.execute("BEGIN")
        install_storage(Minimal())
        Minimal.db.rollback()
        self.assertIsNone(Minimal.db.execute("SELECT 1 FROM sqlite_master WHERE name='history_jobs'").fetchone())
        Minimal.db.close()


if __name__ == "__main__":
    unittest.main()
