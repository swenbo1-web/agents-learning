"""Continuation, delta coverage and read budgets use isolated synthetic data."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_status
from agent_learning.admin_recovery import history_recovery_page
from agent_learning.history_sources import scan_sources
from agent_learning.knowledge_browser import _read_api
from agent_learning.runtime import ContractError, Runtime, encode


class HistoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "home", config={})
        self.addCleanup(self.r.close)
        self.project = self.r.project(self.workspace)
        self.write("README.md", "Project design and build instructions.")

    def write(self, name, body):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def archive(self, **kwargs):
        return self.r.history_archive(self.project, self.workspace, **kwargs)

    def test_second_click_does_not_scan_or_rewrite_the_unfinished_batch(self):
        original = self.archive()["job"]
        self.write("new.md", "New source should wait for an explicit delta scan.")
        with patch("agent_learning.history_sources.scan_sources", side_effect=AssertionError("must continue")), \
                patch.object(self.r, "history_run", side_effect=AssertionError("no implicit restart")):
            status = self.archive()
        self.assertEqual(status["job"]["job_id"], original["job_id"])
        self.assertEqual(status["preparation"]["action"], "continue_existing")
        self.assertFalse(status["preparation"]["scanned"])
        self.assertEqual(status["job"]["updated"], original["updated"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM history_jobs").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_incremental_requires_ending_or_deferring_main_review(self):
        job = self.archive()["job"]["job_id"]
        self.write("new.md", "A new independently selected source.")
        with patch("agent_learning.history_sources.scan_sources", side_effect=AssertionError("guard before scan")):
            with self.assertRaisesRegex(ContractError, "unfinished history"):
                self.r.history_prepare(self.project, self.workspace, mode="incremental")
        self.r.history_defer(self.project, job, "Keep unresolved source for later")
        delta = self.archive(mode="incremental")["job"]
        self.assertNotEqual(delta["job_id"], job)
        self.assertEqual(delta["counts"]["cases"], 1)
        self.assertEqual(delta["changes"]["context_only_sources"], 1)
        self.assertEqual(self.r.history_status(self.project, job)["job"]["counts"]["cases"], 1)

    def test_cross_host_prepare_uses_same_pending_head(self):
        job = self.archive()["job"]["job_id"]
        for host in ("antigravity", "dsh"):
            with closing(Runtime(self.r.home, config={}, host=host)) as other:
                with patch("agent_learning.history_sources.scan_sources", side_effect=AssertionError("no host duplicate")):
                    self.assertEqual(other.history_prepare(self.project, self.workspace)["job_id"], job)

    def test_guard_rechecks_after_scan_before_inserting_another_job(self):
        original = scan_sources
        def competing(*args, **kwargs):
            with closing(Runtime(self.r.home, config={}, host="antigravity")) as other:
                with patch("agent_learning.history_sources.scan_sources", original):
                    other.history_prepare(self.project, self.workspace)
            return original(*args, **kwargs)
        with patch("agent_learning.history_sources.scan_sources", competing):
            result = self.r.history_prepare(self.project, self.workspace)
        self.assertEqual(result["preparation"]["action"], "continue_existing")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM history_jobs").fetchone()[0], 1)

    def test_pause_is_not_silently_undone_by_archive_button(self):
        job = self.archive()["job"]["job_id"]
        self.r.history_pause(self.project, job)
        status = self.archive()
        self.assertEqual(status["job"]["state"], "paused")
        self.assertEqual(status["job"]["last_result"]["reason"], "user_paused")

    def test_deferral_preserves_paused_access_and_blocks_other_run(self):
        job = self.archive()["job"]["job_id"]
        problem = self.r.history_cases(self.project, job)["items"][0]["problem_id"]
        self.r.history_pause(self.project, job)
        self.r.history_defer(self.project, job, "Select another scope explicitly")
        with self.assertRaises(ContractError):
            self.r.review_packet(self.project, problem)
        self.write("new.md", "New task material.")
        delta = self.archive(mode="incremental")["job"]["job_id"]
        with self.assertRaisesRegex(ContractError, "unfinished history"):
            self.r.history_run(self.project, job)
        self.assertEqual(self.r.history_status(self.project)["job"]["job_id"], delta)
        page = {item["id"]:item for item in history_recovery_page(self.r,self.project)["items"]}
        self.assertEqual(page[job]["blocked_by_batch"],delta)
        self.assertFalse(page[job]["can_review"])

    def test_waiting_evidence_does_not_block_new_main_work(self):
        job = self.archive()["job"]["job_id"]
        problem = self.r.history_cases(self.project, job)["items"][0]["problem_id"]
        self.r.db.execute("INSERT INTO review_states VALUES(?,?,?,?,?,?,?)",
            (self.project, problem, 1, "synthetic", "awaiting_evidence", "Need verification", time.time()))
        columns = [row[1] for row in self.r.db.execute("PRAGMA table_info(review_receipts)")]
        row = dict(project=self.project, problem=problem, revision=1, review_signature="synthetic",
            evidence_signature="fixture", call_id=None, decision="needs_evidence", reason="Need verification",
            response="{}", created=time.time(), dependency_signature="", dependencies="[]", duplicate_of=None)
        self.r.db.execute("INSERT INTO review_receipts(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                          [row[column] for column in columns])
        progress = self.r.history_status(self.project, job)["job"]
        self.assertTrue(progress["workflow"]["first_pass_complete"])
        self.assertFalse(progress["workflow"]["blocks_new_batch"])
        self.write("new.md", "New material after a settled evidence request.")
        self.assertNotEqual(self.archive()["job"]["job_id"], job)

    def test_no_changes_reuses_prior_job_after_explicit_deferral(self):
        job = self.archive()["job"]["job_id"]
        self.r.history_defer(self.project, job, "Keep the same source for later")
        result = self.archive(mode="incremental")
        self.assertEqual(result["preparation"]["action"], "no_changes")
        self.assertEqual(result["job"]["job_id"], job)

    def test_pending_candidate_approval_does_not_block_new_main_review(self):
        job = self.archive()["job"]["job_id"]
        problem = self.r.history_cases(self.project, job)["items"][0]["problem_id"]
        packet = self.r.review_packet(self.project, problem)
        self.r.submit_candidate(self.project, problem_id=problem, problem_revision=1, title="Documented design",
            content="Project design and build instructions.", category="project", knowledge_kind="fact",
            applicability="The selected source version", limitations="Reported historical text",
            evidence_refs=[event["id"] for event in packet["events"]], validation_level="reported")
        status = self.r.history_status(self.project, job)["job"]
        self.assertEqual(status["coverage"]["publication"]["awaiting_approval"], 1)
        self.assertFalse(status["workflow"]["blocks_new_batch"])
        self.write("new.md", "A later source does not approve the existing candidate.")
        self.assertNotEqual(self.archive()["job"]["job_id"], job)
        self.assertEqual(self.r.db.execute("SELECT status FROM candidates").fetchone()[0], "candidate")

    def test_current_reserved_request_blocks_defer_and_rescan_across_hosts(self):
        job = self.archive()["job"]["job_id"]
        now = time.time()
        self.r.db.execute("INSERT INTO history_review_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.project,self.r._session_scope(),self.r.session_epoch(),"pending-request","digest",job,
             encode({"expected_governance_epoch":self.r.host_epoch()}),"reserved",now,now,now+120,"{}"))
        with self.assertRaisesRegex(ContractError,"active history request"):
            self.r.history_defer(self.project,job,"Do not race the reserved model review")
        with closing(Runtime(self.r.home,config={},host="dsh")) as other:
            result=other.history_prepare(self.project,self.workspace)
        self.assertEqual(result["preparation"]["next_action"],"wait")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM history_jobs").fetchone()[0],1)

    def test_remaining_scan_prioritizes_budget_omissions(self):
        self.write("README.md", "A" * 24)
        self.write("later.md", "B" * 24)
        self.write("third.md", "C" * 24)
        first = self.archive(scan_options={"max_total_bytes": 24})["job"]
        self.assertEqual(first["counts"]["sources"], 1)
        self.r.history_defer(self.project, first["job_id"], "Explicitly continue limited coverage")
        following = self.archive(mode="remaining")["job"]
        sources = self.r.history_source_report(self.project, following["job_id"])["items"]
        self.assertEqual([item["relative_path"] for item in sources], ["later.md"])
        self.assertEqual(following["counts"]["cases"], 1)
        self.r.history_defer(self.project, following["job_id"], "Continue the remaining original scope")
        third = self.archive(mode="remaining")["job"]
        sources = self.r.history_source_report(self.project, third["job_id"])["items"]
        self.assertEqual([item["relative_path"] for item in sources], ["third.md"])
        self.assertEqual(third["coverage"]["pending_count"], 0)

    def test_plugin_wiring_changes_do_not_create_project_content_changes(self):
        self.write(".codex/config.toml", '[user]\nvalue="keep"\n[mcp_servers.agent_learning]\ncommand="old"\n')
        self.write(".codex/environments/agent-learning.toml", "# Agent Learning managed project action; update through codex-action.\nversion=1\n")
        self.write(".codex/environments/agent-learning.action.json", json.dumps({"kind":"agent-learning-codex-action","schema_version":1}))
        first = scan_sources(self.workspace)
        selected = {s["relative_path"]:s for s in first["sources"]}
        self.assertNotIn(".codex/environments/agent-learning.toml", selected)
        self.assertEqual(selected[".codex/config.toml"]["coverage"], "partial")
        self.assertIn('value="keep"', selected[".codex/config.toml"]["content"])
        self.write(".codex/config.toml", '[user]\nvalue="keep"\n[mcp_servers.agent_learning]\ncommand="new"\n')
        current = {s["relative_path"]:s for s in scan_sources(self.workspace)["sources"]}
        self.assertEqual(selected[".codex/config.toml"]["content_hash"], current[".codex/config.toml"]["content_hash"])

    def test_nonmanaged_same_name_is_kept(self):
        self.write(".codex/environments/agent-learning.toml", 'name="user authored"\n')
        self.assertIn(".codex/environments/agent-learning.toml", [s["relative_path"] for s in scan_sources(self.workspace)["sources"]])

    def test_unchanged_source_is_context_without_becoming_a_new_primary_case(self):
        from agent_learning.history_context import enrich_history_packet, link_history_evidence
        self.write("README.md", "SQLite WAL transaction rollback requires journal recovery.")
        older = self.archive()["job"]["job_id"]
        self.r.history_defer(self.project, older, "Retain old source while reviewing new documentation")
        self.write("new.md", "SQLite WAL transaction rollback documents journal recovery.")
        delta = self.archive(mode="incremental")["job"]
        job = delta["job_id"]
        self.assertEqual(delta["counts"]["cases"], 1)
        self.r.db.execute("UPDATE history_jobs SET state='reviewing',owner='synthetic-owner',review_enabled=1,lease_until=? WHERE id=?",
                          (time.time()+120, job))
        primary = self.r.history_cases(self.project, job)["items"][0]
        packet = self.r.review_packet(self.project, primary["problem_id"])
        enriched = enrich_history_packet(self.r,self.project,packet,job)
        self.assertEqual(len(enriched["related_history"]),1)
        related = enriched["related_history"][0]
        self.assertNotEqual(related["problem_id"], primary["problem_id"])
        refs = [packet["events"][0]["id"],related["events"][0]["id"]]
        link_history_evidence(self.r,self.project,enriched,refs,job)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM history_case_links WHERE project=? AND job_id=?",
                                          (self.project,job)).fetchone()[0],1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0],0)

    def test_dsh_auto_archive_uses_continuation_without_restart(self):
        spec = importlib.util.spec_from_file_location("isolated_dsh_history_tool", Path(__file__).resolve().parents[1] / "hooks/dsh_tool.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job = self.archive()["job"]["job_id"]
        with patch.object(self.r, "history_run", side_effect=AssertionError("no restart")):
            result = module._handle_learning_history(self.r, self.project, str(self.workspace), {"action":"auto","confirm":True})
        self.assertEqual(result["job"]["job_id"], job)

    def test_large_overlapping_batches_fit_original_read_budget(self):
        job = self.archive()["job"]["job_id"]
        source = self.r.db.execute("SELECT material_id FROM history_job_sources WHERE job_id=?", (job,)).fetchone()[0]
        problem_ids = ["synthetic-case-" + str(i) for i in range(8000)]
        now = time.time()
        with self.r.transaction():
            self.r.db.executemany("INSERT INTO problems(id,project,conversation,revision,summary,category,status,created) VALUES(?,?,?,1,?,'project','pending',?)",
                [(identity,self.project,"history:"+source,"Frozen synthetic history",now) for identity in problem_ids])
            self.r.db.executemany("INSERT INTO history_units VALUES(?,?,?,?,?,?)",
                [(identity,self.project,source,i+1,'{"parts":1}',identity) for i,identity in enumerate(problem_ids)])
            self.r.db.executemany("INSERT INTO history_case_links VALUES(?,?,?,?)",
                [(self.project,identity,job,identity) for identity in problem_ids])
            for index in range(2):
                new_job = "overlap-" + str(index)
                self.r.db.execute("INSERT INTO history_jobs SELECT ?,project,workspace,plan,signature,state,host_epoch,review_enabled,max_calls,remaining_calls,owner,lease_until,created+?,updated,last_result FROM history_jobs WHERE id=?", (new_job,index+1,job))
                self.r.db.execute("INSERT INTO history_job_sources SELECT ?,material_id,position,materialized FROM history_job_sources WHERE job_id=?", (new_job,job))
                self.r.db.execute("INSERT INTO history_case_links SELECT project,problem_id,?,unit_id FROM history_case_links WHERE job_id=?", (new_job,job))
        for operation in (lambda:control_status(self.r,self.project,include_history=False),
                          lambda:history_recovery_page(self.r,self.project),
                          lambda:_read_api(self.r.db,self.project,"/api/stats",{})):
            with self.subTest(operation=operation):
                ticks=[0]
                def budget():
                    ticks[0]+=1
                    return ticks[0]>4000
                self.r.db.set_progress_handler(budget,1000)
                try:
                    self.r.db.execute("BEGIN")
                    result=operation()
                    self.assertTrue(result)
                finally:
                    self.r.db.set_progress_handler(None,0)
                    self.r.db.rollback()


if __name__ == "__main__":
    unittest.main()
