"""History queues and coverage use isolated project files and controlled reviewers."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import history_import, history_progress
from agent_learning.runtime import Runtime
from tests.test_module6_queue import fake_reviewer


class HistoryOptimizationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "history-optimization-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="run-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "home"
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(command=[sys.executable], max_calls_per_day=100, timeout_seconds=10)

    def write(self, name, content):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def prepare(self, **kwargs):
        return self.r.history_prepare(self.p, self.workspace, **kwargs)["job_id"]

    def run_history(self, job, decision="skip", callback=None, **kwargs):
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer(decision, callback)) as model:
            result = self.r.history_run(self.p, job, allow_review=True, **kwargs)
        return result["job"], model.call_count

    @staticmethod
    def source(packet):
        return next(event["payload"] for event in packet["events"] if event["type"] == "HistoryImport")

    def test_model_queue_preserves_document_priority_and_unit_order(self):
        self.write("Source/z.cpp", "int important_code = 1;")
        self.write("Config/a.ini", "[project]\nmode=local")
        self.write("Docs/z-summary.md", "Agent design summary.")
        self.write("Docs/a.md", "Other ordinary project documentation.")
        self.write("README.md", "Project overview. " * 600)
        job = self.prepare()
        seen = []
        def observe(packet, _):
            source = self.source(packet)
            seen.append((Path(source["source"]["path"]).name, source["unit_index"]))
        result, calls = self.run_history(job, callback=observe, max_calls=20)
        sources = self.r.history_source_report(self.p, job)["items"]
        source_order = {Path(source["path"]).name: index for index, source in enumerate(sources)}
        self.assertEqual(seen, sorted(seen, key=lambda item: (source_order[item[0]], item[1])))
        self.assertEqual(seen[0], ("README.md", 0))
        self.assertEqual(calls, result["counts"]["cases"])
        self.assertEqual(result["state"], "review_complete")

    def test_resume_skips_completed_units_before_spending_new_call_budget(self):
        self.write("README.md", "Architecture overview.")
        self.write("Docs/new.md", "Previously unread project constraints.")
        job = self.prepare()
        seen = []
        def observe(packet, _):
            seen.append(Path(self.source(packet)["source"]["path"]).name)
        first, calls = self.run_history(job, callback=observe, max_calls=1)
        self.assertEqual(first["state"], "paused")
        second, new_calls = self.run_history(job, callback=observe, max_calls=1)
        self.assertEqual((calls, new_calls), (1, 1))
        self.assertEqual(seen, ["README.md", "new.md"])
        self.assertEqual(second["state"], "review_complete")
        self.assertEqual(second["last_result"]["queue_at_start"]["completed"], 1)
        self.assertEqual(len(second["last_result"]["reviews"]), 1)

    def test_completed_job_with_zero_new_budget_stays_complete(self):
        self.write("README.md", "Architecture overview.")
        job = self.prepare()
        self.run_history(job)
        result, calls = self.run_history(job, max_calls=0)
        self.assertEqual(calls, 0)
        self.assertEqual(result["state"], "review_complete")
        self.assertTrue(result["coverage"]["review"]["complete"])
        self.assertFalse(result["resume"]["explicit_run_required"])

    def test_awaiting_evidence_does_not_reconsume_the_next_unit_budget(self):
        self.write("README.md", "Unverified project overview.")
        self.write("Docs/new.md", "Additional independent project constraints.")
        job = self.prepare()
        first, _ = self.run_history(job, "needs_evidence", max_calls=1)
        self.assertEqual(first["coverage"]["review"]["awaiting_evidence_cases"], 1)
        result, calls = self.run_history(job, max_calls=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result["state"], "materialized")
        self.assertEqual(result["last_result"]["reason"], "needs_evidence")
        self.assertEqual(result["counts"]["pending_cases"], 1)
        self.assertEqual(result["coverage"]["review"]["completed_cases"], 1)
        self.assertFalse(result["coverage"]["review"]["complete"])
        again, calls = self.run_history(job, max_calls=0)
        self.assertEqual(calls, 0)
        self.assertEqual(again["state"], "materialized")
        self.assertFalse(again["resume"]["explicit_run_required"])

    def test_manual_candidate_is_not_reviewed_again_and_legacy_counts_remain(self):
        self.write("README.md", "Local SQLite storage is documented.")
        job = self.prepare()
        self.r.history_run(self.p, job)
        case = self.r.history_cases(self.p, job)["items"][0]
        packet = self.r.review_packet(self.p, case["problem_id"])
        self.r.submit_candidate(self.p, problem_id=case["problem_id"], problem_revision=case["revision"],
            title="Documented storage", content="The archived notes describe SQLite.", category="project", knowledge_kind="fact",
            applicability="Archived source", limitations="Reported; not execution verification",
            evidence_refs=[event["id"] for event in packet["events"]], validation_level="reported")
        result, calls = self.run_history(job, max_calls=0)
        self.assertEqual(calls, 0)
        self.assertEqual(result["state"], "review_complete")
        self.assertTrue(result["coverage"]["review"]["complete"])
        self.assertEqual(result["counts"]["completed_review_cases"], 0)
        self.assertEqual(result["counts"]["pending_cases"], 1)
        self.assertEqual(result["coverage"]["publication"]["awaiting_approval"], 1)
        self.assertEqual(result["coverage"]["publication"]["published_candidates"], 0)

    def test_unknown_call_remains_blocked_without_a_second_model_call(self):
        self.write("README.md", "Uncertain historical observation.")
        job = self.prepare()
        def timeout(packet, process):
            raise subprocess.TimeoutExpired("controlled reviewer", 1)
        first, calls = self.run_history(job, callback=timeout)
        second, second_calls = self.run_history(job)
        self.assertEqual((calls, second_calls), (1, 0))
        self.assertEqual(first["state"], "paused")
        self.assertEqual(second["state"], "paused")
        self.assertEqual(second["last_result"]["reason"], "review_required")
        self.assertEqual(second["coverage"]["review"]["blocked_cases"], 1)
        self.assertFalse(second["coverage"]["review"]["complete"])
        self.assertTrue(second["resume"]["requires_call_retry"])
        self.assertEqual(second["resume"]["blocked_calls"][0]["state"], "timeout_unknown")

    def test_unknown_unit_does_not_starve_other_unread_units(self):
        self.write("README.md", "Uncertain historical observation.")
        self.write("Docs/new.md", "An independent documented constraint.")
        job = self.prepare()
        def timeout(packet, process):
            raise subprocess.TimeoutExpired("controlled reviewer", 1)
        self.run_history(job, callback=timeout, max_calls=1)
        # Independent units resume only after the unhealthy executor's cooldown;
        # the unknown original unit remains blocked for explicit reconciliation.
        self.r.db.execute("UPDATE admin_executor_failures SET next_try=0")
        seen = []
        def observe(packet, _):
            seen.append(Path(self.source(packet)["source"]["path"]).name)
        result, calls = self.run_history(job, callback=observe, max_calls=1)
        self.assertEqual(calls, 1)
        self.assertEqual(seen, ["new.md"])
        self.assertEqual(result["coverage"]["review"]["completed_cases"], 1)
        self.assertEqual(result["coverage"]["review"]["blocked_cases"], 1)
        self.assertEqual(result["state"], "paused")

    def test_explicit_call_retry_requeues_unit_without_refunding_unknown_usage(self):
        from agent_learning.admin_lifecycle import retry_call
        self.write("README.md", "Uncertain historical observation.")
        job = self.prepare()
        def timeout(packet, process):
            raise subprocess.TimeoutExpired("controlled reviewer", 1)
        first, calls = self.run_history(job, callback=timeout, max_calls=1)
        call = first["resume"]["blocked_calls"][0]
        retry_call(self.r, self.p, call["call_id"], "User inspected the uncertain prior execution")
        waiting, waiting_calls = self.run_history(job, max_calls=1)
        self.assertEqual(waiting_calls, 0)
        self.assertEqual(waiting["state"], "paused")
        self.r.db.execute("UPDATE admin_executor_failures SET next_try=0")
        result, new_calls = self.run_history(job, max_calls=1)
        self.assertEqual((calls, new_calls), (1, 1))
        self.assertEqual(result["state"], "review_complete")
        self.assertEqual(result["counts"]["calls"], 2)
        self.assertEqual(result["coverage"]["review"]["blocked_cases"], 0)
        original = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()
        self.assertEqual(tuple(original), ("timeout_unknown", 2))

    def test_missing_executor_stops_with_actionable_reason(self):
        self.write("README.md", "First observation.")
        self.write("Docs/new.md", "Second observation.")
        job = self.prepare()
        with patch("agent_learning.admin.subprocess.Popen") as model, patch(
                "agent_learning.admin_providers.executor_availability", return_value={"cli_available": False, "reason": "missing_cli"}):
            result = self.r.history_run(self.p, job, allow_review=True)["job"]
        model.assert_not_called()
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["last_result"]["reason"], "executor_not_ready")
        self.assertEqual(len(result["last_result"]["reviews"]), 1)
        self.assertEqual(result["coverage"]["review"]["ready_cases"], 2)

    def test_changed_problem_revision_requeues_previously_reviewed_unit(self):
        self.write("README.md", "A documented observation.")
        job = self.prepare()
        self.run_history(job, "needs_evidence")
        case = self.r.history_cases(self.p, job)["items"][0]
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE project=? AND id=?", (self.p, case["problem_id"]))
        result, calls = self.run_history(job, max_calls=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result["state"], "review_complete")
        self.assertEqual(result["coverage"]["review"]["awaiting_evidence_cases"], 0)

    def test_archival_review_and_publication_coverages_are_independent(self):
        self.write("README.md", "A documented historical observation.")
        self.write("Docs/pending.md", "File outside the selected scan budget.")
        job = self.prepare(scan_options={"max_files": 1})
        result, calls = self.run_history(job, "candidate")
        self.assertEqual(calls, 1)
        coverage = result["coverage"]
        self.assertTrue(coverage["archive"]["selected_sources_complete"])
        self.assertTrue(coverage["archive"]["scan_incomplete"])
        self.assertFalse(coverage["archive"]["project_scan_complete"])
        self.assertTrue(coverage["review"]["complete"])
        self.assertEqual(coverage["publication"]["published_candidates"], 0)
        self.assertEqual(coverage["publication"]["awaiting_approval"], 1)
        self.assertFalse(coverage["publication"]["automatic"])

    def test_sibling_context_never_marks_another_unit_reviewed(self):
        self.write("README.md", "The overview references a separate implementation note.")
        self.write("Docs/note.md", "An implementation note with independent source evidence.")
        job = self.prepare()
        result, calls = self.run_history(job, max_calls=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result["coverage"]["review"]["completed_cases"], 1)
        self.assertEqual(result["coverage"]["review"]["ready_cases"], 1)
        self.assertFalse(result["coverage"]["review"]["complete"])

    def test_run_receipt_keeps_candidate_id_arrays(self):
        self.write("README.md", "A documented source.")
        job = self.prepare()
        with patch("agent_learning.admin.admin_once", return_value={"status": "candidate", "candidate_id": "first",
                "candidate_ids": ["first", "second"], "candidate_count": 2, "proposal_ids": ["proposal-first", "proposal-second"]}):
            result = self.r.history_run(self.p, job, allow_review=True)["job"]
        entry = result["last_result"]["reviews"][0]
        self.assertEqual(entry["candidate_id"], "first")
        self.assertEqual(entry["candidate_ids"], ["first", "second"])
        self.assertEqual(entry["candidate_count"], 2)
        self.assertEqual(entry["proposal_ids"], ["proposal-first", "proposal-second"])
        self.assertIn("problem_id", entry)
        self.assertFalse(result["last_result"]["reviews_truncated"])

    def parallel_candidate(self, job):
        other = Runtime(self.home, config={})
        self.addCleanup(other.close)
        case = self.r.history_cases(self.p, job)["items"][0]
        packet = self.r.review_packet(self.p, case["problem_id"])
        body = dict(problem_id=case["problem_id"], problem_revision=case["revision"], title="Concurrent project fact",
                    content="The archived source describes a local project fact.", category="project", knowledge_kind="fact",
                    applicability="Frozen source revision", limitations="Reported, not execution verification",
                    evidence_refs=[event["id"] for event in packet["events"]], validation_level="reported")
        return other, body

    def test_status_counts_and_queue_share_snapshot_during_concurrent_candidate(self):
        self.write("README.md", "A documented source.")
        job = self.prepare()
        self.r.history_run(self.p, job)
        other, body = self.parallel_candidate(job)
        original, writes = history_progress.progress_snapshots, []
        def concurrent(runtime, rows):
            self.assertTrue(runtime.db.in_transaction)
            if not writes:
                writes.append(other.submit_candidate(self.p, **body))
            return original(runtime, rows)
        with patch.object(history_progress, "progress_snapshots", side_effect=concurrent):
            result = self.r.history_status(self.p, job)["job"]
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["coverage"]["review"]["ready_cases"], 1)
        self.assertFalse(result["coverage"]["review"]["complete"])
        self.assertEqual(result["coverage"]["publication"]["awaiting_approval"], 0)
        self.assertFalse(self.r.db.in_transaction)
        following = self.r.history_status(self.p, job)["job"]
        self.assertEqual(following["counts"]["candidates"], 1)
        self.assertTrue(following["coverage"]["review"]["complete"])

    def test_jobs_keep_one_snapshot_across_all_summaries(self):
        self.write("README.md", "An older documented source.")
        older = self.prepare()
        self.r.history_run(self.p, older)
        self.r.history_defer(self.p, older, "Explicitly create a second frozen fixture")
        self.write("README.md", "A newer documented source.")
        newer = self.prepare()
        self.r.history_run(self.p, newer)
        self.r.db.execute("UPDATE history_jobs SET created=1 WHERE id=?", (older,))
        self.r.db.execute("UPDATE history_jobs SET created=2 WHERE id=?", (newer,))
        other, body = self.parallel_candidate(older)
        original, writes = history_progress.progress_snapshots, []
        def concurrent(runtime, rows):
            self.assertTrue(runtime.db.in_transaction)
            if not writes:
                self.assertEqual(rows[0]["id"], newer)
                writes.append(other.submit_candidate(self.p, **body))
            return original(runtime, rows)
        with patch.object(history_progress, "progress_snapshots", side_effect=concurrent):
            result = self.r.history_jobs(self.p)
        self.assertEqual(result["total"], 2)
        self.assertEqual([item["job_id"] for item in result["items"]], [newer, older])
        self.assertEqual([item["counts"]["candidates"] for item in result["items"]], [0, 0])
        self.assertTrue(all(item["coverage"]["review"]["ready_cases"] == 1 for item in result["items"]))
        following = {item["job_id"]: item for item in self.r.history_jobs(self.p)["items"]}
        self.assertEqual(following[older]["counts"]["candidates"], 1)

    def test_standalone_summary_reuses_transactions_and_keeps_one_snapshot(self):
        self.write("README.md", "A documented source.")
        job = self.prepare()
        self.r.history_run(self.p, job)
        other, body = self.parallel_candidate(job)
        row = history_import._job(self.r, self.p, job)
        original, writes = history_progress.progress_snapshots, []
        def concurrent(runtime, rows):
            self.assertTrue(runtime.db.in_transaction)
            if not writes:
                writes.append(other.submit_candidate(self.p, **body))
            return original(runtime, rows)
        with patch.object(history_progress, "progress_snapshots", side_effect=concurrent):
            result = history_import._summary(self.r, row)
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["coverage"]["review"]["ready_cases"], 1)
        self.assertFalse(self.r.db.in_transaction)
        with self.r.transaction():
            borrowed = history_import._summary(self.r, row)
            self.assertTrue(self.r.db.in_transaction)
            self.assertEqual(borrowed["counts"]["candidates"], 1)
        other.history_pause(self.p, job)
        self.assertEqual(history_import._summary(self.r, row)["state"], "paused")


if __name__ == "__main__":
    unittest.main()
