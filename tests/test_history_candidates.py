"""历史多候选与跨文档回执的隔离集成检查；模型为内存替身。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import HISTORY_RESPONSE_SCHEMA, RESPONSE_SCHEMA
from agent_learning.admin_decisions import get_reviews
from agent_learning.runtime import Runtime
from tests.test_module6_queue import fake_reviewer


def history_reviewer(transform):
    class Reviewer(fake_reviewer("candidate")):
        def communicate(self, data=None, timeout=None):
            packet = json.loads(data.decode("utf-8").split("<untrusted_evidence>", 1)[1]
                                .rsplit("</untrusted_evidence>", 1)[0])
            result = super().communicate(data, timeout)
            response = json.loads(self.output.read_text(encoding="utf-8"))
            response = transform(packet, response)
            self.output.write_text(json.dumps(response), encoding="utf-8")
            return result
    return Reviewer


def multiple(packet, response, count=3):
    bodies = []
    for index in range(count):
        body = copy.deepcopy(response["candidate"])
        body.update(title="Independent fact " + str(index), content="Documented fact " + str(index))
        bodies.append(body)
    return dict(response, candidate=None, candidates=bodies, proposal=None, missing_evidence=[])


class HistoryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-history-candidates-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("RenderingPipeline ShaderCache project architecture.", encoding="utf-8")
        self.r = Runtime(Path(self.temp.name) / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)

    def run_history(self, transform=multiple, job=None, max_calls=1):
        if job is None:
            job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        with patch("agent_learning.admin.subprocess.Popen", side_effect=history_reviewer(transform)) as model:
            result = self.r.history_run(self.p, job, allow_review=True, max_calls=max_calls)
        return job, result["job"], model.call_count

    def test_three_candidates_are_one_call_and_separately_approvable(self):
        self.r.config["admin"]["auto_publish"] = True
        job, result, calls = self.run_history()
        self.assertEqual(calls, 1)
        self.assertEqual(result["counts"]["calls"], 1)
        self.assertEqual(result["counts"]["candidates"], 3)
        self.assertEqual(result["counts"]["candidate_cases"], 1)
        self.assertEqual(result["counts"]["reviewed_cases"], 1)
        self.assertEqual(result["counts"]["published_candidates"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0], 3)
        review = get_reviews(self.r, self.p)["items"][0]
        self.assertEqual(len(review["candidate_ids"]), 3)
        self.assertEqual(review["candidate_count"], 3)
        self.assertEqual(review["candidate_id"], review["candidate_ids"][0])
        self.assertEqual(result["last_result"]["reviews"][0]["candidate_ids"], review["candidate_ids"])
        from agent_learning.candidate_review import prepare_review, commit_review
        plan = prepare_review(self.r, self.p, review["candidate_ids"][:2], "approve")
        receipt = commit_review(self.r, self.p, plan, "history-batch-approval")
        self.assertEqual(len(receipt["results"]), 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 2)
        # 重做显式批次不再调用模型，也不把第三条自动批准。
        _, repeated, calls = self.run_history(job=job)
        self.assertEqual(calls, 0)
        self.assertEqual(repeated["counts"]["candidates"], 3)
        self.assertEqual(repeated["counts"]["published_candidates"], 2)

    def test_second_invalid_candidate_rolls_back_entire_batch(self):
        def invalid(packet, response):
            result = multiple(packet, response)
            result["candidates"][1]["evidence_refs"] = ["outside-event"]
            return result
        _, result, calls = self.run_history(invalid)
        self.assertEqual(calls, 1)
        self.assertEqual(result["last_result"]["reason"], "failed")
        for table in ("candidates", "candidate_identity", "governance_proposals", "review_receipts"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)

    def test_batch_refuses_more_than_five_and_ambiguous_single(self):
        _, result, _ = self.run_history(lambda packet, response: multiple(packet, response, 6))
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["last_result"]["reason"], "failed")

    def test_single_and_batch_cannot_both_be_present(self):
        def ambiguous(packet, response):
            result = multiple(packet, response)
            result["candidate"] = response["candidate"]
            return result
        _, result, _ = self.run_history(ambiguous)
        self.assertEqual(result["counts"]["candidates"], 0)

    def test_historical_candidate_cannot_claim_tool_success(self):
        def upgraded(packet, response):
            result = multiple(packet, response)
            result["candidates"][1]["validation_level"] = "tool_success"
            return result
        _, result, _ = self.run_history(upgraded)
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["last_result"]["reason"], "failed")

    def test_total_response_budget_applies_to_whole_batch(self):
        def oversized(packet, response):
            result = multiple(packet, response)
            for body in result["candidates"]:
                body["content"] = "x" * 14000
            return result
        _, result, _ = self.run_history(oversized)
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["last_result"]["reason"], "failed")

    def test_new_schema_is_only_for_history(self):
        self.assertNotIn("candidates", RESPONSE_SCHEMA["properties"])
        self.assertEqual(HISTORY_RESPONSE_SCHEMA["properties"]["candidates"]["maxItems"], 5)
        self.assertIn("candidates", HISTORY_RESPONSE_SCHEMA["required"])
        from agent_learning.admin import admin_once
        self.r.config["admin"]["enabled"] = True
        self.r.task_update(self.p, "ordinary", "Ordinary current task")
        self.r.feedback(self.p, "ordinary", "Problem in normal task")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=history_reviewer(multiple)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_cancel_during_multi_response_preserves_fence(self):
        def cancelled(packet, response):
            self.r.history_cancel(self.p, job)
            return multiple(packet, response)
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        _, result, _ = self.run_history(cancelled, job=job)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["counts"]["candidates"], 0)

    def test_account_change_during_multi_response_rolls_back(self):
        def changed_account(packet, response):
            self.r.account_changed()
            return multiple(packet, response)
        _, result, calls = self.run_history(changed_account)
        self.assertEqual(calls, 1)
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["last_result"]["reason"], "host_epoch_changed")
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0], 0)

    def test_related_sources_are_citable_without_counting_as_reviewed(self):
        (self.workspace / "architecture.md").write_text("RenderingPipeline ShaderCache uses explicit cache revisions.", encoding="utf-8")
        packets = []
        def with_context(packet, response):
            packets.append(packet)
            result = multiple(packet, response)
            extra = packet["related_history"][0]["events"][0]["id"]
            result["candidates"][0]["evidence_refs"].append(extra)
            return result
        _, result, calls = self.run_history(with_context)
        self.assertEqual(calls, 1)
        self.assertEqual(result["counts"]["candidates"], 3)
        self.assertEqual(result["counts"]["cases"], 2)
        self.assertEqual(result["counts"]["reviewed_cases"], 1)
        self.assertEqual(result["counts"]["untouched_cases"], 1)
        refs = self.r.db.execute("SELECT COUNT(*) FROM problem_events WHERE role='history_related'").fetchone()[0]
        self.assertEqual(refs, 1)
        candidate = json.loads(self.r.db.execute("SELECT body FROM candidates ORDER BY created LIMIT 1").fetchone()[0])
        self.r._validate_sources(self.p, candidate)
        from agent_learning.candidate_review import prepare_review, commit_review
        candidate_id = self.r.db.execute("SELECT id FROM candidates ORDER BY created LIMIT 1").fetchone()[0]
        plan = prepare_review(self.r, self.p, [candidate_id], "approve")
        self.assertTrue(plan["ready"])
        approved = commit_review(self.r, self.p, plan, "cross-document-approval")
        knowledge = self.r.read(self.p, approved["results"][0]["knowledge_id"])
        self.assertEqual(knowledge["validation_level"], "reported")
        self.assertEqual(len(knowledge["sources"][0]["evidence"]), 2)
        # 关联来源回查不会回流主证据包制造新的审核身份。
        from agent_learning.admin import _fingerprint, _history_packet
        history_job = result["job_id"]
        self.r.db.execute("UPDATE history_jobs SET state='reviewing',review_enabled=1,owner='test',lease_until=? WHERE id=?",
                          (__import__("time").time() + 30, history_job))
        rebuilt = _history_packet(self.r, self.p, packets[0]["problem"]["id"], history_job)
        self.assertEqual(_fingerprint(rebuilt), _fingerprint(packets[0]))
        self.assertEqual(len(rebuilt["events"]), len(packets[0]["events"]))

    def test_changed_related_snapshot_rejects_entire_response(self):
        (self.workspace / "architecture.md").write_text("RenderingPipeline ShaderCache explicit cache revisions.", encoding="utf-8")
        def changed(packet, response):
            event = packet["related_history"][0]["events"][0]["id"]
            self.r.db.execute("UPDATE events SET payload=? WHERE project=? AND id=?",
                              (json.dumps({"content": "Changed archive evidence"}), self.p, event))
            return multiple(packet, response)
        _, result, _ = self.run_history(changed)
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["last_result"]["reason"], "failed")

    def test_secondary_candidate_origin_is_in_review_dependencies(self):
        _, result, _ = self.run_history()
        self.assertEqual(result["counts"]["candidates"], 3)
        # 用同样的可检索标题读取其他问题的比较上下文。
        self.r.task_update(self.p, "lookup", "Independent fact")
        problem = self.r.feedback(self.p, "lookup", "Independent fact documented")
        packet = self.r.review_packet(self.p, problem["id"])
        ids = {row["id"] for row in self.r.db.execute("SELECT id FROM candidates")}
        related = [item for item in packet["related_candidates"] if item["candidate_id"] in ids]
        self.assertEqual(len(related), 3)
        self.assertTrue(all(item.get("origin_evidence_signature") for item in related))
        from agent_learning.admin_decisions import dependencies
        evidence = related[0]["origin_evidence_signature"]
        self.assertEqual(dependencies(packet, evidence)["related_candidates"], [])


if __name__ == "__main__":
    unittest.main()
