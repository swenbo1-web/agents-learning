"""Long-input scheduling restores scoped public handoffs, never broad recall.

All observations and databases are synthetic and remain below build/local.
These checks do not invoke a host, model, administrator or real knowledge store.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references
from agent_learning.candidate_review import commit_review, prepare_review
from agent_learning.knowledge_index import normalized_query_plan
from agent_learning.runtime import Runtime


class ContextSchedulingTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "context-scheduling-tests"
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("tmp", "home", "codex", "runtime-releases"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex")}
        environment.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = "reader"
        self.r.task_update(self.p, "reader", "preserve ledger revision ordering", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]

    @staticmethod
    def huge_query():
        return "compiler " + " " * 65536 + " E_CRITICAL"

    @staticmethod
    def many_identities():
        return " ".join(f"src/file{index}.py" for index in range(129))

    def record(self, text="HANDOFF_FENCE: preserve revision ordering; acceptance pending.", *,
               conversation=None, workspace=None, project=None, task=None, agent=None,
               assignment=None, kind="Stop", completion="complete", truncated=False):
        workspace, project = workspace or self.workspace, project or self.p
        conversation = conversation or self.conversation
        task = task if task is not None else self.r.task(project, conversation)
        payload = ({"prompt": text} if kind == "UserPromptSubmit" else
                   {"last_assistant_message": text, "completion": completion,
                    "last_assistant_message_truncated": truncated})
        with self.r.transaction():
            event, _ = self.r._event(project, conversation, kind, payload,
                task_id=task["id"] if task else None,
                task_revision=task["revision"] if task else None,
                task_write_version=task.get("write_version") if task else None,
                agent_id=agent, assignment_id=assignment, outcome="reported")
            created = self.r._work_capture_event(project, event["id"], workspace, raw=payload)
        return created["memory_id"], event["id"]

    def context(self, query=None, **kwargs):
        return self.r._context(self.p, self.conversation, query=self.huge_query() if query is None else query,
            context_handle=kwargs.pop("context_handle", self.handle),
            workspace=kwargs.pop("workspace", self.workspace), **kwargs)

    @staticmethod
    def cards(text):
        return [json.loads(line[len("[工作历史] "):]) for line in text.splitlines()
                if line.startswith("[工作历史] ")]

    def test_real_compaction_recovers_handoff_with_incomplete_long_query_identity(self):
        ident, _ = self.record()
        self.assertFalse(normalized_query_plan(self.huge_query())["automatic_safe"])
        text = self.context(compact=True)
        self.assertIn(ident, text)
        card = next(item for item in self.cards(text) if item["memory_id"] == ident)
        self.assertEqual(card["state"], "reported")
        self.assertEqual(card["validation_level"], "reported")
        self.assertEqual(card["reference_role"], "task_handoff")
        self.assertTrue(card["read_required"])
        self.assertLessEqual(len(text), self.r.config["injection_max_chars"])
        self.assertIn("非正式解法/执行授权", text)

    def test_real_compaction_recovers_handoff_when_identity_count_overflows(self):
        ident, _ = self.record()
        query = self.many_identities()
        plan = normalized_query_plan(query)
        self.assertTrue(plan["query_diagnostics"]["identity_limit_exceeded"])
        self.assertFalse(plan["automatic_safe"])
        self.assertIn(ident, self.context(query, compact=True))

    def test_long_query_does_not_enable_unrelated_handoff_without_compaction(self):
        ident, _ = self.record()
        self.assertNotIn(ident, self.context())
        self.assertNotIn(ident, self.context("继续"))

    def test_short_new_topic_does_not_restore_unrelated_handoff(self):
        ident, _ = self.record()
        self.assertNotIn(ident, self.context("unrelatedtopicwithoutoverlap"))

    def test_recovered_handoff_is_not_repeated_on_plain_continuation(self):
        ident, _ = self.record()
        self.assertIn(ident, self.context(compact=True))
        self.assertNotIn(ident, self.context("继续"))
        self.assertNotIn(ident, self.context("continue"))

    def test_unsafe_compaction_does_not_recall_another_conversation(self):
        self.r.task_update(self.p, "other", "preserve ledger revision ordering", {"mode": "analysis"})
        other, _ = self.record(conversation="other")
        current, _ = self.record()
        text = self.context(compact=True)
        self.assertIn(current, text)
        self.assertNotIn(other, text)

    def test_unsafe_compaction_does_not_recall_another_checkout(self):
        sibling = self.root / "sibling"
        sibling.mkdir()
        other, _ = self.record(workspace=sibling)
        self.assertNotIn(other, self.context(compact=True))

    def test_unsafe_compaction_does_not_recall_another_project(self):
        other_workspace = self.root / "other-project"
        other_workspace.mkdir()
        other_project = self.r.project(other_workspace)
        self.r.task_update(other_project, "other-reader", "preserve ledger revision ordering", {"mode": "analysis"})
        other, _ = self.record(project=other_project, workspace=other_workspace, conversation="other-reader")
        self.assertNotIn(other, self.context(compact=True))

    def test_unsafe_compaction_does_not_recall_old_task(self):
        old, _ = self.record()
        self.r.task_update(self.p, "reader", "new task unrelated to the old phase", new_task=True)
        current, _ = self.record(text="CURRENT_HANDOFF: next phase is still unverified.")
        text = self.context(compact=True)
        self.assertIn(current, text)
        self.assertNotIn(old, text)

    def test_unsafe_compaction_does_not_recall_old_task_revision(self):
        old, _ = self.record()
        task = self.r.task(self.p, "reader")
        self.r.task_update(self.p, "reader", task["goal"], {"mode": "implementation"})
        current, _ = self.record(text="CURRENT_REVISION_HANDOFF: keep implementation scope explicit.")
        text = self.context(compact=True)
        self.assertIn(current, text)
        self.assertNotIn(old, text)

    def test_partial_public_reply_is_not_a_complete_handoff(self):
        ident, _ = self.record(completion="partial")
        self.assertNotIn(ident, self.context(compact=True))

    def test_user_request_does_not_become_phase_handoff(self):
        ident, _ = self.record(kind="UserPromptSubmit")
        self.assertNotIn(ident, self.context(compact=True))

    def test_parent_compaction_does_not_recall_child_public_reply(self):
        handle = self.r.register_context(self.p, "reader", "child")["context_handle"]
        assigned = self.r._bind_assignment(self.p, "reader", "child", handle)
        child, _ = self.record(agent="child", assignment=assigned["id"])
        self.assertNotIn(child, self.context(compact=True))

    def test_child_compaction_restores_only_the_current_fixed_assignment(self):
        handle = self.r.register_context(self.p, "reader", "child")["context_handle"]
        assigned = self.r._bind_assignment(self.p, "reader", "child", handle)
        other, _ = self.record(agent="child", assignment="unrelated-assignment")
        current, _ = self.record(agent="child", assignment=assigned["id"])
        text = self.context(compact=True, agent_id="child", context_handle=handle)
        self.assertIn(current, text)
        self.assertNotIn(other, text)

    def test_unbound_task_cannot_restore_a_handoff(self):
        ident, _ = self.record(conversation="empty", task={})
        handle = self.r.register_context(self.p, "empty")["context_handle"]
        text = self.r._context(self.p, "empty", query=self.huge_query(), compact=True,
            context_handle=handle, workspace=self.workspace)
        self.assertNotIn(ident, text)

    def test_unsafe_compaction_restores_at_most_one_public_handoff(self):
        identities = {self.record(text=f"PHASE_{index}: public handoff remains reported.")[0]
                      for index in range(5)}
        text = self.context(compact=True)
        shown = [card for card in self.cards(text) if card["memory_id"] in identities]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["reference_role"], "task_handoff")

    def test_healthy_fourth_handoff_survives_three_newer_unhealthy_records(self):
        healthy, _ = self.record(text="HEALTHY_PHASE: preserve exact ledger invariants.")
        self.r.db.execute("UPDATE work_memories SET created=1 WHERE id=?", (healthy,))
        unhealthy = set()
        for index in range(3):
            ident, _ = self.record(text=f"STALE_PHASE_{index}: this source changed after capture.")
            unhealthy.add(ident)
            self.r.db.execute("UPDATE work_memories SET created=? WHERE id=?", (index + 2, ident))
            self.r.db.execute("UPDATE work_memory_sources SET source_hash='changed-source' WHERE memory_id=?", (ident,))
        text = self.context(compact=True)
        self.assertIn(healthy, text)
        self.assertFalse(unhealthy & {card["memory_id"] for card in self.cards(text)})

    def test_handoff_source_checks_do_not_cross_twenty_candidate_boundary(self):
        healthy, _ = self.record(text="OUTSIDE_SCAN: old healthy handoff is beyond this bounded scan.")
        self.r.db.execute("UPDATE work_memories SET created=1 WHERE id=?", (healthy,))
        for index in range(20):
            ident, _ = self.record(text=f"STALE_BOUNDARY_PHASE_{index}: source metadata is no longer current.")
            self.r.db.execute("UPDATE work_memories SET created=? WHERE id=?", (index + 2, ident))
            self.r.db.execute("UPDATE work_memory_sources SET source_hash='changed-source' WHERE memory_id=?", (ident,))
        with patch.object(self.r, "_work_item", wraps=self.r._work_item) as source_check:
            text = self.context(compact=True)
        self.assertNotIn(healthy, text)
        self.assertEqual(self.cards(text), [])
        self.assertLessEqual(source_check.call_count, 20)

    def test_newer_public_handoff_precedes_older_generated_work_summary(self):
        older = self.derived_handoff()
        newer, _ = self.record(text="LATEST_PUBLIC: latest observed phase handoff remains reported.")
        self.r.db.execute("UPDATE work_memories SET created=1 WHERE id=?", (older,))
        self.r.db.execute("UPDATE work_memories SET created=2 WHERE id=?", (newer,))
        text = self.context(compact=True)
        handoffs = [card for card in self.cards(text) if card.get("reference_role") == "task_handoff"]
        self.assertEqual([card["memory_id"] for card in handoffs], [newer])
        self.assertNotIn(older, text)

    def matched_phase_records(self):
        matches = []
        for index in range(3):
            ident, _ = self.record(text=f"matchingtopic: observed result {index}.")
            self.r.db.execute("UPDATE work_memories SET created=? WHERE id=?", (index + 1, ident))
            matches.append(ident)
        handoff, _ = self.record(text="UNRELATED_HANDOFF: next phase is pending.")
        self.r.db.execute("UPDATE work_memories SET created=10 WHERE id=?", (handoff,))
        return list(reversed(matches)), handoff

    def test_safe_compaction_keeps_current_match_first_and_reserves_handoff_slot(self):
        matches, handoff = self.matched_phase_records()
        items = self.r.work_context_items(self.p, "reader", "matchingtopic", compact=True,
            workspace=self.workspace, task_state=self.r.task(self.p, "reader"), limit=3)
        self.assertEqual([item["memory_id"] for item in items], [matches[0], handoff, matches[1]])
        self.assertEqual(items[1]["reference_role"], "task_handoff")
        text = self.context("matchingtopic", compact=True)
        cards = self.cards(text)
        self.assertEqual(cards[0]["memory_id"], matches[0])
        self.assertIn(handoff, [card["memory_id"] for card in cards])

    def test_safe_compaction_single_item_budget_prioritizes_current_match(self):
        matches, handoff = self.matched_phase_records()
        self.r.config["work_memory"]["max_items"] = 1
        text = self.context("matchingtopic", compact=True)
        self.assertEqual([card["memory_id"] for card in self.cards(text)], [matches[0]])
        self.assertNotIn(handoff, text)

    def test_safe_compaction_single_item_budget_uses_handoff_when_query_has_no_match(self):
        _, handoff = self.matched_phase_records()
        self.r.config["work_memory"]["max_items"] = 1
        text = self.context("absenttopicwithoutoverlap", compact=True)
        self.assertEqual([card["memory_id"] for card in self.cards(text)], [handoff])
        self.assertEqual(self.cards(text)[0]["reference_role"], "task_handoff")

    def test_handoff_only_does_not_run_broad_query_or_file_recall(self):
        ident, _ = self.record()
        original = self.r._work_search_rows
        def bounded(project, query, **kwargs):
            self.assertEqual(query, "")
            self.assertTrue(kwargs.get("handoff"))
            self.assertEqual(kwargs.get("conversation"), "reader")
            self.assertEqual(kwargs.get("task_state", {}).get("id"), self.r.task(self.p, "reader")["id"])
            return original(project, query, **kwargs)
        with patch.object(self.r, "_work_search_rows", side_effect=bounded):
            items = self.r.work_context_items(self.p, "reader", self.huge_query(), handoff_only=True,
                compact=True, task_state=self.r.task(self.p, "reader"), workspace=self.workspace)
        self.assertEqual([item["memory_id"] for item in items], [ident])

    def test_unrendered_handoff_is_not_acknowledged_with_tiny_budget(self):
        ident, _ = self.record()
        self.r.config["injection_max_chars"] = 128
        self.assertNotIn(ident, self.context(compact=True))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)
        self.r.config["injection_max_chars"] = 2400
        self.assertIn(ident, self.context(compact=True))

    def test_source_missing_handoff_is_not_restored(self):
        ident, _ = self.record()
        self.r.db.execute("UPDATE work_memory_sources SET event_id='missing-source' WHERE memory_id=?", (ident,))
        self.assertNotIn(ident, self.context(compact=True))

    def test_changed_source_hash_handoff_is_not_restored(self):
        ident, _ = self.record()
        self.r.db.execute("UPDATE work_memory_sources SET source_hash='changed-source' WHERE memory_id=?", (ident,))
        self.assertNotIn(ident, self.context(compact=True))

    def test_truncated_public_handoff_is_not_restored(self):
        ident, _ = self.record(truncated=True)
        self.assertNotIn(ident, self.context(compact=True))

    def derived_handoff(self):
        task = self.r.task(self.p, "reader")
        payload = {"tool_name": "run_public_tests", "tool_response": {"exit_code": 0, "stdout": "ledger tests passed"}}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "reader", "PostToolUse", payload,
                task_id=task["id"], task_revision=task["revision"], operation_id="handoff-source", outcome="succeeded")
            self.r.db.execute("INSERT INTO operations(project,id,conversation,state,last_event,observation) "
                "VALUES(?,?,?,?,?,?)", (self.p, "handoff-source", "reader", "succeeded", event["id"], "succeeded"))
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        summary = self.r.record_work_summary(self.p, "derived-handoff", {
            "title": "DERIVED_HANDOFF", "summary": "DERIVED_HANDOFF: ledger checks need independent acceptance."},
            [self.r.work_source_snapshot(self.p, event["id"])], "reader",
            task_id=task["id"], task_revision=task["revision"])
        return summary["memory_id"]

    def test_conflicted_source_does_not_restore_derived_handoff(self):
        ident = self.derived_handoff()
        self.r.db.execute("UPDATE operations SET conflict=1,state='conflict' WHERE project=? AND id='handoff-source'", (self.p,))
        self.assertNotIn(ident, self.context(compact=True))

    def test_denied_source_does_not_restore_derived_handoff(self):
        ident = self.derived_handoff()
        self.r.db.execute("UPDATE operations SET decision='denied' WHERE project=? AND id='handoff-source'", (self.p,))
        self.assertNotIn(ident, self.context(compact=True))

    def test_source_change_after_selection_is_checked_before_rendering(self):
        ident, _ = self.record()
        original = self.r.work_validate_items
        def changed(project, items, workspace=None, **kwargs):
            self.r.db.execute("UPDATE work_memory_sources SET source_hash='racing-source' WHERE memory_id=?", (ident,))
            return original(project, items, workspace=workspace, **kwargs)
        with patch.object(self.r, "work_validate_items", side_effect=changed):
            text = self.context(compact=True)
        self.assertNotIn(ident, text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)

    def candidate(self, title="compiler", content="FORMAL_GUIDANCE_FENCE: compiler compatibility requires verification."):
        conversation = "knowledge-source-" + str(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
        task = self.r.task_update(self.p, conversation, title + " source", {"mode": "analysis"})
        event, _ = self.r._event(self.p, conversation, "Stop", {"last_assistant_message": content},
            task_id=task["id"], task_revision=task["revision"], outcome="reported")
        problem = self.r.feedback(self.p, conversation, title + " recorded source", category="operation",
            event_refs=[event["id"]])
        return self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content, category="operation", applicability="Recorded compiler environment",
            limitations="Reported reference only", validation_level="reported", evidence_refs=[event["id"]])

    def test_unsafe_handoff_does_not_relax_formal_or_candidate_query_gate(self):
        formal = self.r.publish(self.p, self.candidate()["candidate_id"])
        candidate = self.candidate(title="compiler unpublished", content="UNPUBLISHED_FENCE: compiler setup")
        policy = candidate_references.policy(self.r, self.p)
        candidate_references.configure(self.r, self.p, True, policy["version"])
        ident, _ = self.record()
        with patch("agent_learning.candidate_references.search", side_effect=AssertionError("unsafe candidate query")):
            text = self.context(compact=True)
        self.assertIn(ident, text)
        self.assertNotIn(formal["knowledge_id"], text)
        self.assertNotIn(candidate["candidate_id"], text)
        self.assertNotIn("FORMAL_GUIDANCE_FENCE", text)
        self.assertFalse(normalized_query_plan(self.r.db.execute(
            "SELECT last_query FROM contexts WHERE conversation='reader'").fetchone()[0])["automatic_safe"])

    def test_revocation_notice_survives_unsafe_handoff_without_old_formal_body(self):
        formal = self.r.publish(self.p, self.candidate()["candidate_id"])
        self.assertIn(formal["knowledge_id"], self.context("compiler"))
        self.r.revoke(self.p, formal["knowledge_id"], expected_revision=formal["revision"])
        ident, _ = self.record()
        text = self.context(compact=True)
        self.assertIn(formal["knowledge_id"], text)
        self.assertIn("已撤销", text)
        self.assertNotIn("FORMAL_GUIDANCE_FENCE", text)
        self.assertIn(ident, text)

    def test_rejected_candidate_lineage_does_not_restore_public_handoff(self):
        candidate = self.candidate()["candidate_id"]
        policy = candidate_references.policy(self.r, self.p)
        candidate_references.configure(self.r, self.p, True, policy["version"])
        reference = candidate_references.assess(self.r, self.p, candidate)
        ident, event = self.record()
        candidate_references.record_lineage(self.r, self.p, event, [{
            "candidate_id": candidate, "reference_version": reference["reference_version"]}], "possible_influence")
        plan = prepare_review(self.r, self.p, [candidate], "reject", "Synthetic source is not accepted")
        commit_review(self.r, self.p, plan, "reject-handoff-source")
        self.assertNotIn(ident, self.context(compact=True))

    def test_antigravity_ephemeral_invocation_is_not_real_compaction(self):
        self.r = Runtime(self.root / "ag-home", host="antigravity", config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = self.r.host_conversation("reader")
        self.r.task_update(self.p, self.conversation, "preserve ledger revision ordering", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, self.conversation)["context_handle"]
        ident, _ = self.record()
        self.assertNotIn(ident, self.context(delivery="ephemeral"))
        self.assertIn(ident, self.context(compact=True, delivery="ephemeral"))


if __name__ == "__main__":
    unittest.main()
