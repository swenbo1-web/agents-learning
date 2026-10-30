"""A retired formal claim cannot regain automatic authority as its work source.

All evidence is synthetic. Original work history remains readable; governance
is applied to equivalent material, not every claim citing the same event.
"""
import json
import unittest

from agent_learning.admin_governance import execute_governance, propose_governance
from agent_learning.runtime import Runtime
from agent_learning.mcp import Server
from tests import test_context_dedup as fixtures


class WorkPublicationRiskTests(unittest.TestCase):
    OLD = "结论：replayguard 重放账本时可以省略校验。\n限制：公开陈述，尚未独立验证。"
    NEW = "结论：replayguard 重放账本必须核对幂等身份。\n限制：公开陈述，尚未独立验证。"
    OTHER = "结论：replayguard 诊断日志应保留稳定事件编号。\n限制：这不是重放校验结论。"

    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.serial = 0

    @property
    def r(self):
        return self.f.r

    @property
    def p(self):
        return self.f.p

    def profile(self, host):
        runtime = Runtime(self.f.root / ("profile-" + host), host=host, config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(runtime.close)
        self.f.r = runtime
        self.f.p = runtime.project(self.f.workspace)

    def material(self, *, content=None, source_content=None, file=False):
        content = content or self.OLD
        source = self.f.source(source_content or content, capture=True, file=file)
        publication, _ = self.f.published(source, title="replayguard replay boundary", content=content)
        return publication, source

    def dispute(self, publication):
        self.serial += 1
        conversation = self.r.host_conversation("counterexample-" + str(self.serial))
        return self.r.feedback(self.p, conversation, "replayguard 缺少校验造成重复记账",
            knowledge_id=publication["knowledge_id"], knowledge_revision=publication["revision"],
            feedback_kind="counterexample")

    def read_work(self, source, **kwargs):
        return self.r.work_read(self.p, source["memory"]["memory_id"], workspace=self.f.workspace, **kwargs)

    def context(self, *, conversation=None, query="replayguard", compact=False, raw=None, tool_only=False):
        self.serial += 1
        conversation = conversation or self.r.host_conversation("consumer-" + str(self.serial))
        if self.r.task(self.p, conversation) is None:
            self.r.task_update(self.p, conversation, "replayguard independent task", {"mode": "analysis"})
        handle = self.r.register_context(self.p, conversation)["context_handle"]
        return self.r._context(self.p, conversation, query=query, compact=compact,
            context_handle=handle, workspace=self.f.workspace, raw=raw, tool_only=tool_only)

    def assert_no_old_advice(self, text, memory_id):
        for card in self.f.work_cards(text):
            if card["memory_id"] == memory_id:
                self.assertNotIn("可以省略校验", card.get("summary", ""))
                self.assertNotIn("可以省略校验", card.get("title", ""))

    def test_three_profiles_fresh_consumer_cannot_reinject_disputed_or_revoked_source(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                self.profile(host)
                publication, source = self.material()
                self.dispute(publication)
                disputed = self.context()
                self.assertEqual(self.f.formal_cards(disputed), [])
                self.assert_no_old_advice(disputed, source["memory"]["memory_id"])
                self.r.revoke(self.p, publication["knowledge_id"], expected_revision=1)
                revoked = self.context(compact=True)
                self.assertEqual(self.f.formal_cards(revoked), [])
                self.assert_no_old_advice(revoked, source["memory"]["memory_id"])

    def test_manual_history_retains_original_text_and_named_governance_risk(self):
        publication, source = self.material()
        self.dispute(publication)
        result = self.read_work(source)
        self.assertEqual(result["content"], self.OLD)
        self.assertEqual(result["validation_level"], "reported")
        self.assertFalse(result.get("automatic_eligible", True))
        self.assertTrue(result.get("published_knowledge_risk"))
        self.assertEqual(result["published_knowledge_risk"][0]["knowledge_id"], publication["knowledge_id"])
        self.assertEqual(result["published_knowledge_risk"][0]["revision"], 1)
        self.assertTrue(result["published_knowledge_risk"][0]["reason"])

    def test_revoked_manual_history_remains_readable_with_risk(self):
        publication, source = self.material()
        self.r.revoke(self.p, publication["knowledge_id"], expected_revision=1)
        result = self.read_work(source)
        self.assertEqual(result["content"], self.OLD)
        self.assertFalse(result.get("automatic_eligible", True))
        self.assertTrue(result.get("published_knowledge_risk"))

    def test_superseded_revision_does_not_reenter_as_current_work_advice(self):
        publication, source = self.material()
        replacement_source = self.f.source(self.NEW, capture=True)
        candidate, _ = self.f.candidate(replacement_source, title="replayguard corrected replay boundary", content=self.NEW)
        self.r.publish(self.p, candidate["candidate_id"], knowledge_id=publication["knowledge_id"], expected_revision=1)
        output = self.context(compact=True)
        self.assert_no_old_advice(output, source["memory"]["memory_id"])
        old = self.read_work(source)
        current = self.read_work(replacement_source)
        self.assertTrue(old.get("published_knowledge_risk"))
        self.assertFalse(old.get("automatic_eligible", True))
        self.assertFalse(current.get("published_knowledge_risk"))
        self.assertTrue(current.get("automatic_eligible", True))

    def test_explicit_restore_recovers_work_eligibility_and_source_version(self):
        publication, source = self.material()
        before = self.read_work(source)
        self.r.revoke(self.p, publication["knowledge_id"], expected_revision=1)
        retired = self.read_work(source)
        self.assertNotEqual(before["source_version"], retired["source_version"])
        self.assertFalse(retired.get("automatic_eligible", True))
        self.r.restore(self.p, publication["knowledge_id"], expected_revision=1)
        restored = self.read_work(source)
        self.assertTrue(restored.get("automatic_eligible", True))
        self.assertFalse(restored.get("published_knowledge_risk"))
        self.assertNotEqual(retired["source_version"], restored["source_version"])

    def test_explicit_counterexample_dismissal_recovers_eligibility(self):
        publication, source = self.material()
        problem = self.dispute(publication)
        retired = self.read_work(source)
        self.assertFalse(retired.get("automatic_eligible", True))
        # Synthetic independent inspection, bound to the actual concern case.
        event, _ = self.r._event(self.p, problem["conversation"], "PostToolUse",
            {"tool_name": "verify_replayguard", "tool_response": {"exit_code": 0}},
            task_id=problem["task_id"], task_revision=problem["task_revision"], outcome="succeeded")
        with self.r.transaction():
            self.r._attach_case_evidence(problem["id"], [(event["id"], "observation")])
        problem = self.r.db.execute("SELECT * FROM problems WHERE project=? AND id=?", (self.p, problem["id"])).fetchone()
        proposal = propose_governance(self.r, self.p, problem["id"], problem["revision"], "dismiss_counterexample",
            knowledge_id=publication["knowledge_id"], expected_revision=1,
            reason="Independent replay check contradicts this synthetic counterexample", evidence_refs=[event["id"]])
        execute_governance(self.r, self.p, proposal["proposal_id"])
        recovered = self.read_work(source)
        self.assertTrue(recovered.get("automatic_eligible", True))
        self.assertFalse(recovered.get("published_knowledge_risk"))

    def test_existing_work_exposure_gets_a_neutral_update_once(self):
        source = self.f.source(self.OLD, capture=True)
        memory = source["memory"]["memory_id"]
        conversation = self.r.host_conversation("already-exposed-reader")
        first = self.context(conversation=conversation)
        self.assertIn(memory, first)
        publication, _ = self.f.published(source, title="replayguard replay boundary", content=self.OLD)
        # Give the consumer the publication once before changing its governance.
        self.context(conversation=conversation)
        self.dispute(publication)
        update = self.context(conversation=conversation)
        self.assert_no_old_advice(update, memory)
        cards = [card for card in self.f.work_cards(update) if card["memory_id"] == memory]
        self.assertTrue(cards)
        self.assertEqual(cards[0].get("reference_role"), "source_update")
        self.assertTrue(cards[0].get("read_required"))
        again = self.context(conversation=conversation)
        self.assertNotIn(memory, [card["memory_id"] for card in self.f.work_cards(again)])

    def test_tool_file_route_cannot_bypass_revocation(self):
        publication, source = self.material(file=True)
        self.r.revoke(self.p, publication["knowledge_id"], expected_revision=1)
        output = self.context(query="", tool_only=True, raw={"tool_name": "read_file",
            "tool_input": {"path": str(self.f.workspace / "ledger.py")}})
        self.assert_no_old_advice(output, source["memory"]["memory_id"])

    def test_same_task_handoff_cannot_restore_revoked_claim(self):
        publication, source = self.material()
        self.r.revoke(self.p, publication["knowledge_id"], expected_revision=1)
        output = self.context(conversation=source["conversation"], query="", compact=True)
        self.assert_no_old_advice(output, source["memory"]["memory_id"])

    def test_unrelated_claim_from_same_source_event_is_not_blocked(self):
        publication, source = self.material(source_content=self.OLD + "\n\n" + self.OTHER)
        task = self.r.task(self.p, source["conversation"])
        snapshot = self.r.work_source_snapshot(self.p, source["event"]["id"])
        related = self.r.record_work_summary(self.p, "synthetic-old-claim", {"title": "replayguard replay boundary", "summary": self.OLD},
            [snapshot], source["conversation"], task["id"], task["revision"])
        other = self.r.record_work_summary(self.p, "synthetic-other-claim", {"title": "replayguard diagnostic identity", "summary": self.OTHER},
            [snapshot], source["conversation"], task["id"], task["revision"])
        self.dispute(publication)
        blocked = self.r.work_read(self.p, related["memory_id"], workspace=self.f.workspace)
        unrelated = self.r.work_read(self.p, other["memory_id"], workspace=self.f.workspace)
        self.assertFalse(blocked.get("automatic_eligible", True))
        self.assertTrue(blocked.get("published_knowledge_risk"))
        self.assertTrue(unrelated.get("automatic_eligible", True))
        self.assertFalse(unrelated.get("published_knowledge_risk"))
        self.assertEqual(unrelated["content"], self.OTHER)

    def test_same_text_in_an_unrelated_source_is_not_retired_globally(self):
        publication, _ = self.material()
        unrelated = self.f.source(self.OLD, capture=True)
        self.dispute(publication)
        result = self.read_work(unrelated)
        self.assertFalse(result.get("published_knowledge_risk"))
        self.assertTrue(result.get("automatic_eligible", True))

    def test_same_source_complete_conclusion_without_its_limitations_is_still_retired(self):
        publication, source = self.material(source_content=self.OLD + "\n\n" + self.OTHER)
        task = self.r.task(self.p, source["conversation"])
        snapshot = self.r.work_source_snapshot(self.p, source["event"]["id"])
        conclusion = self.OLD.split("\n", 1)[0]
        old_summary = self.r.record_work_summary(self.p, "synthetic-short-conclusion",
            {"title": "replayguard replay conclusion", "summary": conclusion},
            [snapshot], source["conversation"], task["id"], task["revision"])
        other_summary = self.r.record_work_summary(self.p, "synthetic-independent-conclusion",
            {"title": "replayguard diagnostic identity", "summary": self.OTHER},
            [snapshot], source["conversation"], task["id"], task["revision"])
        self.dispute(publication)
        blocked = self.r.work_read(self.p, old_summary["memory_id"], workspace=self.f.workspace)
        independent = self.r.work_read(self.p, other_summary["memory_id"], workspace=self.f.workspace)
        self.assertEqual(blocked["content"], conclusion)
        self.assertFalse(blocked.get("automatic_eligible", True))
        self.assertTrue(blocked.get("published_knowledge_risk"))
        self.assertTrue(independent.get("automatic_eligible", True))
        self.assertFalse(independent.get("published_knowledge_risk"))

    def test_complete_published_sentence_before_same_line_source_limit_is_retired(self):
        conclusion = self.OLD.split("\n", 1)[0]
        inline_source = self.OLD.replace("\n", "")
        publication, source = self.material(content=conclusion, source_content=inline_source)
        self.dispute(publication)
        result = self.read_work(source)
        self.assertEqual(result["content"], inline_source)
        self.assertFalse(result.get("automatic_eligible", True))
        self.assertTrue(result.get("published_knowledge_risk"))
        self.assert_no_old_advice(self.context(), source["memory"]["memory_id"])

    def test_an_uncited_event_in_the_same_source_case_is_not_blocked(self):
        publication, source = self.material()
        task = self.r.task(self.p, source["conversation"])
        event, _ = self.r._event(self.p, source["conversation"], "Stop", {"last_assistant_message": self.OLD},
            task_id=task["id"], task_revision=task["revision"], outcome="reported")
        with self.r.transaction():
            memory = self.r._work_capture_event(self.p, event["id"], self.f.workspace, raw={"last_assistant_message": self.OLD})
            self.r._attach_case_evidence(json.loads(self.r.db.execute("SELECT body FROM knowledge WHERE project=? AND id=? AND revision=1",
                (self.p, publication["knowledge_id"])).fetchone()[0])["problem_id"], [(event["id"], "observation")])
        self.dispute(publication)
        item = self.r.work_read(self.p, memory["memory_id"], workspace=self.f.workspace)
        self.assertFalse(item.get("published_knowledge_risk"))

    def test_other_project_governance_cannot_contaminate_local_source(self):
        _, source = self.material()
        foreign = self.f.root / "foreign-project"
        foreign.mkdir()
        local_project = self.f.p
        local_workspace = self.f.workspace
        self.f.p = self.r.project(foreign)
        self.f.workspace = foreign
        publication, _ = self.material()
        self.dispute(publication)
        self.f.p = local_project
        self.f.workspace = local_workspace
        item = self.read_work(source)
        self.assertFalse(item.get("published_knowledge_risk"))
        self.assertTrue(item.get("automatic_eligible", True))

    def test_fresh_source_version_does_not_change_without_governance_update(self):
        _, source = self.material()
        first = self.read_work(source)
        second = self.read_work(source)
        self.assertEqual(first["source_version"], second["source_version"])
        self.assertFalse(second.get("published_knowledge_risk"))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_mcp_completed_section_receipt_retains_publication_risk(self):
        publication, source = self.material()
        self.dispute(publication)
        server = Server(self.r, self.p, context_handle=self.f.handle, workspace=self.f.workspace)
        args = {"memory_id": source["memory"]["memory_id"], "section": "summary", "max_chars": 10000}
        first = server.call("work_memory_read", args)
        self.assertEqual(first["content"], self.OLD)
        receipt = server.call("work_memory_read", dict(args, max_chars=1024))
        self.assertTrue(receipt["already_read"])
        self.assertTrue(receipt["published_knowledge_risk"])
        self.assertFalse(receipt["automatic_eligible"])
        self.assertFalse(receipt["model_read_confirmed"])

    def test_mcp_prior_healthy_read_cannot_hide_new_governance_risk(self):
        publication, source = self.material()
        server = Server(self.r, self.p, context_handle=self.f.handle, workspace=self.f.workspace)
        args = {"memory_id": source["memory"]["memory_id"], "section": "summary", "max_chars": 10000}
        first = server.call("work_memory_read", args)
        self.dispute(publication)
        changed = server.call("work_memory_read", args)
        self.assertNotEqual(first["source_version"], changed["source_version"])
        self.assertTrue(changed["published_knowledge_risk"])
        self.assertFalse(changed.get("already_read", False))
        self.assertEqual(changed["content"], self.OLD)

    def test_publication_lookup_uses_event_then_case_index_without_project_scan(self):
        publication, source = self.material()
        self.dispute(publication)
        queries = []
        self.r.db.set_trace_callback(lambda sql: queries.append(sql) if sql.startswith("WITH related AS") else None)
        try:
            self.read_work(source)
        finally:
            self.r.db.set_trace_callback(None)
        self.assertTrue(queries)
        plan = [row["detail"] for row in self.r.db.execute("EXPLAIN QUERY PLAN " + queries[0])]
        self.assertTrue(any("problem_events_event" in step and "event=?" in step for step in plan))
        self.assertTrue(any("knowledge_sources_problem" in step and "problem_id=?" in step for step in plan))

    def test_statement_period_is_not_a_prefix_match_inside_another_file_name(self):
        from agent_learning.work_publication_risk import _same_material
        self.assertFalse(_same_material("Conclusion: replayguard loads package.json safely.",
                                        "Conclusion: replayguard loads package."))


if __name__ == "__main__":
    unittest.main()
