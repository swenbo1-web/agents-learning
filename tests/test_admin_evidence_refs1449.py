"""Canonical administrator refs remain scoped to the visible frozen case."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.runtime import Runtime, encode
from tests import test_history_candidates as history_fixture
from tests import test_history_response_recovery as history_recovery_fixture
from tests import test_module6_queue as queue_fixture
from tests import test_phase_review as phase_fixture
from tests import test_work_summary_balanced as summary_fixture


def qualified(packet, ref):
    return f"{packet['problem']['id']}@{packet['problem']['revision']}:{ref}"


def transform_reviewer(base, transform):
    class Reviewer(base):
        def communicate(self, data=None, timeout=None):
            result = super().communicate(data, timeout)
            packet = json.loads(data.decode("utf-8").split("<untrusted_evidence>", 1)[1]
                                .rsplit("</untrusted_evidence>", 1)[0])
            response = json.loads(self.output.read_text(encoding="utf-8"))
            transform(packet, response)
            self.output.write_text(encode(response), encoding="utf-8")
            return result
    return Reviewer


class AdminEvidenceRefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="evidence-refs1449-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(enabled=True, command=[sys.executable], timeout_seconds=5,
                                      max_input_chars=16000, max_calls_per_day=100, auto_publish=False)
        self.r.task_update(self.p, "chat", "Inspect a supplied project convention")
        self.problem = self.r.feedback(self.p, "chat", "Project convention uses a transaction journal", category="project")

    def run_review(self, transform):
        seen = {}
        def apply(packet, response):
            transform(packet, response["candidate"])
            seen.update(packet=deepcopy(packet), response=deepcopy(response))
        with patch("agent_learning.admin.subprocess.Popen",
                   transform_reviewer(queue_fixture.fake_reviewer("candidate"), apply)):
            result = admin_once(self.r, self.p, self.problem["id"])
        return result, seen

    def assert_rejected(self, transform):
        result, _ = self.run_review(transform)
        self.assertEqual(result["status"], "failed", result)
        for table in ("candidates", "governance_proposals", "review_receipts"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)
        return result

    def test_raw_refs_remain_accepted(self):
        result, seen = self.run_review(lambda packet, body: None)
        self.assertEqual(result["status"], "candidate", result)
        body = json.loads(self.r.db.execute("SELECT body FROM candidates").fetchone()[0])
        self.assertEqual(body["evidence_refs"], sorted(seen["response"]["candidate"]["evidence_refs"]))

    def test_exact_current_case_qualification_is_canonicalized_without_rewriting_reply(self):
        def transform(packet, body):
            body["evidence_refs"] = [qualified(packet, ref) for ref in body["evidence_refs"]]
        result, seen = self.run_review(transform)
        self.assertEqual(result["status"], "candidate", result)
        body = json.loads(self.r.db.execute("SELECT body FROM candidates").fetchone()[0])
        self.assertEqual(body["evidence_refs"], sorted(e["id"] for e in seen["packet"]["events"]))
        archived = json.loads(self.r.db.execute("SELECT response FROM admin_response_records").fetchone()[0])
        self.assertEqual(archived, seen["response"])
        proposal_refs = json.loads(self.r.db.execute("SELECT evidence_refs FROM governance_proposals").fetchone()[0])
        self.assertEqual(proposal_refs, body["evidence_refs"])

    def test_raw_and_qualified_same_event_do_not_manufacture_extra_evidence(self):
        def transform(packet, body):
            body["evidence_refs"] += [qualified(packet, ref) for ref in body["evidence_refs"]]
        result, seen = self.run_review(transform)
        self.assertEqual(result["status"], "candidate", result)
        body = json.loads(self.r.db.execute("SELECT body FROM candidates").fetchone()[0])
        self.assertEqual(body["evidence_refs"], sorted({e["id"] for e in seen["packet"]["events"]}))

    def test_wrong_problem_qualification_is_rejected(self):
        self.assert_rejected(lambda packet, body: body.update(evidence_refs=[
            f"foreign-case@{packet['problem']['revision']}:{body['evidence_refs'][0]}"]))

    def test_wrong_revision_qualification_is_rejected(self):
        self.assert_rejected(lambda packet, body: body.update(evidence_refs=[
            f"{packet['problem']['id']}@{packet['problem']['revision'] + 1}:{body['evidence_refs'][0]}"]))

    def test_noncanonical_revision_spelling_is_rejected(self):
        self.assert_rejected(lambda packet, body: body.update(evidence_refs=[
            f"{packet['problem']['id']}@0{packet['problem']['revision']}:{body['evidence_refs'][0]}"]))

    def test_matching_prefix_cannot_authorize_a_nonvisible_event(self):
        event, _ = self.r._event(self.p, "unrelated", "Stop", {"last_assistant_message": "Outside this packet"})
        self.assert_rejected(lambda packet, body: body.update(evidence_refs=[qualified(packet, event["id"])]))

    def test_qualified_ref_does_not_repair_wrong_candidate_identity(self):
        def transform(packet, body):
            body["evidence_refs"] = [qualified(packet, ref) for ref in body["evidence_refs"]]
            body["problem_id"] = "foreign-case"
        self.assert_rejected(transform)

    def test_qualification_does_not_bypass_changed_frozen_revision(self):
        def transform(packet, body):
            body["evidence_refs"] = [qualified(packet, ref) for ref in body["evidence_refs"]]
            self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (packet["problem"]["id"],))
        self.assert_rejected(transform)

    def test_qualified_reported_source_cannot_claim_tool_success(self):
        def transform(packet, body):
            body["evidence_refs"] = [qualified(packet, ref) for ref in body["evidence_refs"]]
            body["validation_level"] = "tool_success"
        self.assert_rejected(transform)

    def test_schema_guidance_preserves_packet_and_signature_at_the_character_boundary(self):
        from agent_learning import review_packet as prompts
        from agent_learning.admin import _fingerprint
        from agent_learning.admin_decisions import review_signature
        from agent_learning.mcp import CANDIDATE
        # Even presentation-only prose changes budget selection if it enters
        # review_instruction. Keep format guidance solely in the output schema.
        self.assertEqual(prompts.CONTENT_LAYOUT_PREFIX, "candidate正文先写有据“结论：”，保留条件限制。\n")
        described = CANDIDATE["properties"]["evidence_refs"]
        self.assertIn("raw events[].id", described["description"])
        self.assertIn("problem_revision", described["description"])
        old_schema = {key: value for key, value in described.items() if key != "description"}
        packet = self.r.review_packet(self.p, self.problem["id"])
        cfg = self.r.admin_effective_config(self.p)
        boundary = len(prompts.review_instruction(packet))
        self.assertFalse(packet["incomplete"])
        self.assertGreater(boundary, 1000)
        for budget in (boundary - 1, boundary, boundary + 1):
            with self.subTest(budget=budget):
                current = self.r.review_packet(self.p, self.problem["id"], max_chars=budget)
                with patch.dict(CANDIDATE["properties"], evidence_refs=old_schema):
                    previous = self.r.review_packet(self.p, self.problem["id"], max_chars=budget)
                self.assertEqual(previous, current)
                self.assertEqual(prompts.review_instruction(previous), prompts.review_instruction(current))
                self.assertEqual(review_signature(previous, cfg, _fingerprint(previous)),
                                 review_signature(current, cfg, _fingerprint(current)))

    def test_truncated_packet_does_not_authorize_even_known_omitted_sources(self):
        for index in range(24):
            event, _ = self.r._event(self.p, "chat", "CaseFeedback", {
                "summary": f"Independent supplied observation {index}", "details": "Detail " * 700},
                outcome="reported")
            self.r.db.execute("INSERT INTO problem_events VALUES(?,?,'observation')", (self.problem["id"], event["id"]))
        packet = self.r.review_packet(self.p, self.problem["id"])
        self.assertTrue(packet["incomplete"])
        visible = {event["id"] for event in packet["events"]}
        all_ids = {row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (self.problem["id"],))}
        self.assertTrue(all_ids - visible)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_once(self.r, self.p, self.problem["id"])
        model.assert_not_called()
        self.assertEqual(result["status"], "needs_evidence", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)


class PhaseEvidenceRefTests(unittest.TestCase):
    setUp = phase_fixture.PhaseReviewTests.setUp
    phase = phase_fixture.PhaseReviewTests.phase

    def run_review(self, transform):
        self.phase("one", "First sourced project convention")
        self.phase("two", "Separate architecture convention")
        with patch("agent_learning.admin.subprocess.Popen", transform_reviewer(phase_fixture.reviewer(), transform)):
            return admin_once(self.r, self.p)

    def test_each_case_multiple_candidates_accept_only_its_own_qualification(self):
        def transform(packet, response):
            items = {item["problem"]["id"]: item for item in packet["items"]}
            for value in response["items"]:
                for body in value["candidates"]:
                    body["evidence_refs"] = [qualified(items[value["problem_id"]], ref) for ref in body["evidence_refs"]]
        result = self.run_review(transform)
        self.assertEqual(result["status"], "candidate", result)
        self.assertGreaterEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 4)
        for row in self.r.db.execute("SELECT body FROM candidates"):
            self.assertTrue(all("@" not in ref for ref in json.loads(row[0])["evidence_refs"]))

    def test_qualified_sibling_evidence_rolls_back_preceding_valid_candidates(self):
        def transform(packet, response):
            first = packet["items"][0]
            last = response["items"][-1]["candidates"][-1]
            last["evidence_refs"] = [qualified(first, first["events"][0]["id"])]
        result = self.run_review(transform)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0], 0)

    def test_sibling_id_cannot_be_disguised_with_current_case_qualification(self):
        def transform(packet, response):
            first, last = packet["items"][0], packet["items"][-1]
            self.assertNotEqual(first["problem"]["id"], last["problem"]["id"])
            response["items"][-1]["candidates"][-1]["evidence_refs"] = [qualified(last, first["events"][0]["id"])]
        result = self.run_review(transform)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)


class HistoryEvidenceRefTests(unittest.TestCase):
    setUp = history_fixture.HistoryCandidateTests.setUp
    run_history = history_fixture.HistoryCandidateTests.run_history

    def test_primary_qualified_refs_work_for_single_history_candidate(self):
        def transform(packet, response):
            response["candidate"]["evidence_refs"] = [qualified(packet, ref) for ref in response["candidate"]["evidence_refs"]]
            return response
        _, result, calls = self.run_history(transform)
        self.assertEqual(calls, 1)
        self.assertEqual(result["counts"]["candidates"], 1, result)
        self.assertEqual(result["counts"]["published_candidates"], 0)

    def test_primary_qualified_refs_work_for_multiple_history_candidates(self):
        def transform(packet, response):
            response = history_fixture.multiple(packet, response)
            for body in response["candidates"]:
                body["evidence_refs"] = [qualified(packet, ref) for ref in body["evidence_refs"]]
            return response
        _, result, _ = self.run_history(transform)
        self.assertEqual(result["counts"]["candidates"], 3, result)

    def test_primary_qualification_preserves_raw_related_history_authorization(self):
        (self.workspace / "related.md").write_text("RenderingPipeline ShaderCache preserves shader architecture.", encoding="utf-8")
        seen = []
        def transform(packet, response):
            related = packet["related_history"][0]["events"][0]["id"]
            seen.append(related)
            response["candidate"]["evidence_refs"] = [qualified(packet, ref) for ref in response["candidate"]["evidence_refs"]] + [related]
            return response
        _, result, _ = self.run_history(transform)
        self.assertEqual(result["counts"]["candidates"], 1, result)
        refs = json.loads(self.r.db.execute("SELECT body FROM candidates").fetchone()[0])["evidence_refs"]
        self.assertIn(seen[0], refs)

    def test_qualification_never_reinterprets_related_source_as_primary(self):
        (self.workspace / "related.md").write_text("RenderingPipeline ShaderCache preserves shader architecture.", encoding="utf-8")
        def transform(packet, response):
            related = packet["related_history"][0]["events"][0]["id"]
            response["candidate"]["evidence_refs"] += [qualified(packet, related)]
            return response
        _, result, _ = self.run_history(transform)
        self.assertEqual(result["counts"]["candidates"], 0, result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problem_events WHERE role='history_related'").fetchone()[0], 0)

    def test_qualified_history_still_cannot_claim_tool_success(self):
        def transform(packet, response):
            response["candidate"]["evidence_refs"] = [qualified(packet, ref) for ref in response["candidate"]["evidence_refs"]]
            response["candidate"]["validation_level"] = "tool_success"
            return response
        _, result, _ = self.run_history(transform)
        self.assertEqual(result["counts"]["candidates"], 0, result)


class SummaryEvidenceRefTests(unittest.TestCase):
    setUp = summary_fixture.BalancedWorkSummaryTests.setUp
    capture = summary_fixture.BalancedWorkSummaryTests.capture
    tool = summary_fixture.BalancedWorkSummaryTests.tool
    queue = summary_fixture.BalancedWorkSummaryTests.queue

    def test_qualified_summary_refs_remain_reported_and_do_not_create_formal_knowledge(self):
        self.capture("UserPromptSubmit", text="Optical calibration requires physical validation.")
        self.tool()
        self.capture(text="Optical calibration update is still awaiting physical validation.")
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        def transform(packet, response):
            response["candidate"]["evidence_refs"] = [qualified(packet, ref) for ref in response["candidate"]["evidence_refs"]]
        with patch("agent_learning.admin.subprocess.Popen", transform_reviewer(queue_fixture.fake_reviewer("candidate"), transform)):
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertEqual(result["status"], "candidate", result)
        self.assertEqual(result["purpose"], "work_summary")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)


class HistoryRecoveryEvidenceRefTests(unittest.TestCase):
    setUp = history_recovery_fixture.HistoryResponseRecoveryTests.setUp
    failed = history_recovery_fixture.HistoryResponseRecoveryTests.failed
    recover = history_recovery_fixture.HistoryResponseRecoveryTests.recover
    candidate_ids = history_recovery_fixture.HistoryResponseRecoveryTests.candidate_ids

    def test_explicit_history_local_recovery_matches_canonical_body_without_changing_archive_or_call(self):
        def transform(packet, response):
            response = history_fixture.multiple(packet, response)
            for body in response["candidates"]:
                body["evidence_refs"] = [qualified(packet, ref) for ref in body["evidence_refs"]]
            return response
        job, call = self.failed(transform=transform)
        archive = dict(self.r.db.execute("SELECT * FROM admin_response_records WHERE call_id=?", (call,)).fetchone())
        paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
        job_before = dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())
        observations = [tuple(row) for row in self.r.db.execute("SELECT * FROM admin_usage_observations")]
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.recover(call)
        model.assert_not_called()
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(len(self.candidate_ids()), 3)
        after = dict(self.r.db.execute("SELECT * FROM admin_response_records WHERE call_id=?", (call,)).fetchone())
        for key in ("response", "response_digest", "reserved", "reserved_digest"):
            self.assertEqual(after[key], archive[key], key)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone()), paid)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone()), job_before)
        self.assertEqual([tuple(row) for row in self.r.db.execute("SELECT * FROM admin_usage_observations")], observations)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_wrong_qualification_still_blocks_history_local_recovery(self):
        def transform(packet, response):
            response = history_fixture.multiple(packet, response)
            response["candidates"][-1]["evidence_refs"] = [
                f"{packet['problem']['id']}@{packet['problem']['revision'] + 1}:{packet['events'][0]['id']}"]
            return response
        _, call = self.failed(transform=transform)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.recover(call)
        model.assert_not_called()
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(self.candidate_ids(), [])


if __name__ == "__main__":
    unittest.main()
