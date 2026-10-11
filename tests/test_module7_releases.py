"""成熟度与知识发布批次真实 SQLite 检查，不调用模型或远端服务。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent_learning.knowledge_releases import KnowledgeReleaseMixin, install_releases
from agent_learning.runtime import ContractError, Runtime, encode


ReleaseRuntime = Runtime if issubclass(Runtime, KnowledgeReleaseMixin) else type(
    "ReleaseRuntime", (KnowledgeReleaseMixin, Runtime), {})


class Module7ReleasesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module7-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.r = ReleaseRuntime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        install_releases(self.r)
        self.p = self.r.project(self.workspace)
        self.count = 0

    def source(self, chat="chat-a", content="Adjust compiler include path", category="operation",
               kind="solution", level="tool_success", target=None):
        self.count += 1
        self.r.task_update(self.p, chat, "Check compiler remedy")
        operation = uuid.uuid4().hex
        self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": chat,
            "cwd": str(self.workspace), "tool_use_id": operation, "tool_name": "Bash"})
        self.r.handle_hook({"hook_event_name": "PostToolUse", "session_id": chat,
            "cwd": str(self.workspace), "tool_use_id": operation, "tool_name": "Bash",
            "tool_response": {"exit_code": 0}})
        event = self.r.db.execute("SELECT id FROM events WHERE project=? AND conversation=? "
            "AND type='PostToolUse' ORDER BY created DESC,rowid DESC LIMIT 1", (self.p, chat)).fetchone()[0]
        problem = self.r.feedback(self.p, chat, f"Independent observation {self.count}",
                                  category=category, event_refs=[event])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title="Compiler include remedy", content=content,
            category=category, knowledge_kind=kind, applicability="Ubuntu gcc", limitations="Observed tasks only",
            evidence_refs=[event], validation_level=level)
        published = self.r.publish(self.p, candidate["candidate_id"],
            **({"knowledge_id": target["knowledge_id"], "expected_revision": target["revision"]} if target else {}))
        return {**published, "problem_id": problem["id"], "operation": operation, "chat": chat}

    def independent(self, **kwargs):
        first = self.source("chat-a", **kwargs)
        self.source("chat-b", **kwargs)
        return first

    def maturity(self, knowledge):
        return self.r.knowledge_maturity(self.p, knowledge["knowledge_id"], knowledge["revision"])

    def release(self):
        prepared = self.r.prepare_knowledge_release(self.p)
        return self.r.commit_knowledge_release(self.p, prepared["release_id"])

    def feedback(self, knowledge):
        self.r.task_update(self.p, "counter", "Check counterexample")
        return self.r.feedback(self.p, "counter", "Counterexample observed", category="operation",
            knowledge_id=knowledge["knowledge_id"], knowledge_revision=knowledge["revision"],
            feedback_kind="counterexample")

    def test_migration_is_idempotent_and_preserves_schema_and_existing_ids(self):
        knowledge = self.source()
        before = self.r.db.execute("PRAGMA user_version").fetchone()[0]
        install_releases(self.r)
        install_releases(self.r)
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], before)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 1)
        self.assertEqual(self.maturity(knowledge)["maturity"], "candidate")

    def test_two_independent_tasks_can_become_stable(self):
        knowledge = self.independent()
        self.assertEqual(self.maturity(knowledge)["independent_validation_count"], 2)
        self.assertTrue(self.maturity(knowledge)["eligible"])
        result = self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, request_id="stable-1")
        self.assertTrue(result["changed"])
        self.assertEqual(self.maturity(knowledge)["maturity"], "stable")

    def test_two_cases_from_same_task_do_not_count_twice(self):
        knowledge = self.source("chat-a")
        self.source("chat-a")
        evaluation = self.maturity(knowledge)
        self.assertEqual(evaluation["independent_validation_count"], 1)
        self.assertIn("two_independent_tasks_required", evaluation["reasons"])

    def test_new_revision_of_same_task_does_not_count_as_independent(self):
        knowledge = self.source("chat-a")
        self.r.task_update(self.p, "chat-a", "Updated compiler task")
        self.source("chat-a")
        self.assertEqual(self.maturity(knowledge)["independent_validation_count"], 1)

    def test_professional_and_pattern_require_specialized_rules(self):
        for category, kind in (("professional", "solution"), ("operation", "pattern")):
            with self.subTest(category=category, kind=kind):
                knowledge = self.independent(category=category, kind=kind)
                self.assertIn("specialized_validation_rule_required", self.maturity(knowledge)["reasons"])
                with self.assertRaises(ContractError):
                    self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)

    def test_reported_sources_cannot_become_stable(self):
        knowledge = self.independent(level="reported")
        self.assertIn("observed_tool_success_required", self.maturity(knowledge)["reasons"])

    def test_counterexample_blocks_promotion_even_after_revoked_disposition(self):
        knowledge = self.independent()
        problem = self.feedback(knowledge)
        self.assertIn("counterexample_disposition_requires_review", self.maturity(knowledge)["reasons"])
        self.r.db.execute("UPDATE knowledge_feedback SET disposition='revoked' WHERE project=? AND problem_id=?",
                          (self.p, problem["id"]))
        self.assertIn("counterexample_disposition_requires_review", self.maturity(knowledge)["reasons"])

    def test_source_conflict_invalidates_stable_certification(self):
        knowledge = self.independent()
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        self.r.handle_hook({"hook_event_name": "PostToolUse", "session_id": knowledge["chat"],
            "cwd": str(self.workspace), "tool_use_id": knowledge["operation"], "tool_name": "Bash",
            "tool_response": {"exit_code": 1}})
        evaluation = self.maturity(knowledge)
        self.assertEqual(evaluation["maturity"], "candidate")
        self.assertFalse(evaluation["eligible"])
        self.assertEqual(self.r.db.execute("SELECT state FROM knowledge_maturity").fetchone()[0], "stable")

    def test_source_case_change_invalidates_stable(self):
        knowledge = self.independent()
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (knowledge["problem_id"],))
        self.assertEqual(self.maturity(knowledge)["maturity"], "candidate")
        self.assertFalse(self.maturity(knowledge)["eligible"])

    def test_new_supporting_source_requires_recertification(self):
        knowledge = self.independent()
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        self.source("chat-c")
        self.assertEqual(self.maturity(knowledge)["maturity"], "candidate")
        self.assertTrue(self.maturity(knowledge)["eligible"])
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        self.assertEqual(self.maturity(knowledge)["maturity"], "stable")

    def test_promotion_receipt_replays_after_source_change_and_rejects_different_request(self):
        knowledge = self.independent()
        first = self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, request_id="idem")
        self.feedback(knowledge)
        self.assertEqual(first, self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, request_id="idem"))
        self.assertEqual(self.maturity(knowledge)["maturity"], "candidate")
        with self.assertRaises(ContractError):
            self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, request_id="idem", reason="Different")

    def test_repeated_stable_promotion_without_request_has_no_version_changes(self):
        knowledge = self.independent()
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        before = self.r.db.execute("SELECT generation FROM projects WHERE id=?", (self.p,)).fetchone()[0]
        result = self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        self.assertFalse(result["changed"])
        self.assertEqual(self.r.db.execute("SELECT generation FROM projects WHERE id=?", (self.p,)).fetchone()[0], before)

    def test_promotion_action_receipt_is_atomic_with_certification(self):
        knowledge = self.independent()
        before = self.r.db.execute("SELECT generation FROM projects WHERE id=?", (self.p,)).fetchone()[0]
        with patch("agent_learning.knowledge_lifecycle.record_action", side_effect=RuntimeError("receipt failure")):
            with self.assertRaises(RuntimeError):
                self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, request_id="atomic-promotion")
        self.assertEqual(self.maturity(knowledge)["maturity"], "candidate")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_maturity_receipts").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT generation FROM projects WHERE id=?", (self.p,)).fetchone()[0], before)
        result = self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, request_id="atomic-promotion")
        row = self.r.db.execute("SELECT action,response FROM knowledge_actions WHERE project=? AND request_id=?",
                                (self.p, "atomic-promotion")).fetchone()
        self.assertEqual(row["action"], "promote")
        self.assertEqual(json.loads(row["response"]), result)

    def test_epoch_and_revision_are_checked(self):
        knowledge = self.independent()
        with self.assertRaises(ContractError):
            self.r.promote_knowledge(self.p, knowledge["knowledge_id"], True)
        with self.assertRaises(ContractError):
            self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 2)
        epoch = self.r.host_epoch()
        self.r.account_changed("release-epoch-test")
        with self.assertRaises(ContractError):
            self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1, expected_epoch=epoch)

    def test_empty_project_does_not_create_release(self):
        self.assertEqual(self.r.prepare_knowledge_release(self.p)["state"], "unchanged")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0], 0)

    def test_prepare_commit_and_retry_are_immutable(self):
        self.source()
        first = self.release()
        self.assertEqual(first, self.r.commit_knowledge_release(self.p, first["release_id"]))
        self.assertEqual(self.r.prepare_knowledge_release(self.p)["state"], "unchanged")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.r.db.execute("UPDATE knowledge_releases SET reason='changed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.r.db.execute("DELETE FROM knowledge_releases")

    def test_prepare_repeated_before_commit_returns_same_release(self):
        self.source()
        first = self.r.prepare_knowledge_release(self.p)
        self.assertEqual(first, self.r.prepare_knowledge_release(self.p))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0], 1)

    def test_prepare_after_maturity_change_creates_new_release(self):
        knowledge = self.independent()
        first = self.release()
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        second = self.release()
        self.assertNotEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(self.r.read_knowledge_release(self.p, second["release_id"])["items"][0]["maturity"], "stable")

    def test_source_change_after_prepare_prevents_commit(self):
        knowledge = self.source()
        prepared = self.r.prepare_knowledge_release(self.p)
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (knowledge["problem_id"],))
        with self.assertRaises(ContractError):
            self.r.commit_knowledge_release(self.p, prepared["release_id"])
        self.assertIsNone(self.r._release_head(self.p))

    def test_new_knowledge_after_prepare_prevents_partial_commit(self):
        self.source()
        prepared = self.r.prepare_knowledge_release(self.p)
        self.source("chat-b", content="Different remedy")
        with self.assertRaises(ContractError):
            self.r.commit_knowledge_release(self.p, prepared["release_id"])

    def test_revoke_after_prepare_prevents_commit(self):
        knowledge = self.source()
        prepared = self.r.prepare_knowledge_release(self.p)
        self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=knowledge["revision"])
        with self.assertRaises(ContractError):
            self.r.commit_knowledge_release(self.p, prepared["release_id"])

    def test_promotion_after_prepare_prevents_commit(self):
        knowledge = self.independent()
        prepared = self.r.prepare_knowledge_release(self.p)
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        with self.assertRaises(ContractError):
            self.r.commit_knowledge_release(self.p, prepared["release_id"])

    def test_parent_cas_rejects_older_preparation(self):
        self.source()
        old = self.r.prepare_knowledge_release(self.p)
        self.source("chat-b", content="Different remedy")
        newer = self.release()
        with self.assertRaises(ContractError):
            self.r.commit_knowledge_release(self.p, old["release_id"])
        self.assertEqual(self.r._release_head(self.p), newer["release_id"])

    def test_release_max_items_does_not_silently_truncate(self):
        self.source()
        self.source("chat-b", content="Different remedy")
        with self.assertRaises(ContractError):
            self.r.prepare_knowledge_release(self.p, max_items=1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0], 0)

    def test_manifest_does_not_duplicate_large_body_or_source_evidence(self):
        knowledge = self.source(content="compiler evidence " * 1000)
        released = self.release()
        raw = self.r.db.execute("SELECT manifest FROM knowledge_releases WHERE project=? AND id=?",
                                (self.p, released["release_id"])).fetchone()[0]
        compact = json.loads(raw)["items"][0]["snapshot"]
        self.assertNotIn("body", compact)
        self.assertNotIn("sources", compact)
        self.assertIn("body_digest", compact)
        self.assertLess(len(raw), 4000)
        self.assertGreater(len(self.r.read(self.p, knowledge["knowledge_id"])["content"]), 10000)

    def test_release_diff_records_added_source_changed_and_removed(self):
        knowledge = self.source()
        first = self.release()
        self.assertEqual(first["diff_counts"], {"added": 1, "updated": 0, "removed": 0})
        self.source("chat-b")
        changed = self.release()
        self.assertEqual(changed["diff_counts"], {"added": 0, "updated": 1, "removed": 0})
        self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=1)
        removed = self.release()
        self.assertEqual(removed["diff_counts"], {"added": 0, "updated": 0, "removed": 1})
        self.assertEqual(self.r.read_knowledge_release(self.p, removed["release_id"])["diff"]["removed"],
                         [knowledge["knowledge_id"]])

    def test_daily_release_no_change_creates_no_rows_or_admin_calls(self):
        self.source()
        first = self.r.daily_knowledge_release(self.p)
        calls = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        rows = self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0]
        again = self.r.daily_knowledge_release(self.p)
        self.assertEqual(again["state"], "unchanged")
        self.assertEqual(again["release_id"], first["release_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0], rows)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], calls)

    def test_daily_release_rejects_intermediate_change_without_head_switch(self):
        knowledge = self.source()
        original = self.r.commit_knowledge_release
        def intervening_change(project, release_id, **kwargs):
            self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (knowledge["problem_id"],))
            return original(project, release_id, **kwargs)
        with patch.object(self.r, "commit_knowledge_release", side_effect=intervening_change) as commit:
            with self.assertRaises(ContractError):
                self.r.daily_knowledge_release(self.p)
        self.assertEqual(commit.call_count, 1)
        self.assertIsNone(self.r._release_head(self.p))

    def test_release_list_and_read_are_budgeted_and_pageable(self):
        for index in range(7):
            self.source(f"chat-{index}", content=f"Remedy {index}")
            self.release()
        page = self.r.list_knowledge_releases(self.p, limit=2, max_chars=2048)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["next_offset"], 2)
        head = self.r._release_head(self.p)
        read = self.r.read_knowledge_release(self.p, head, limit=2, max_chars=2048)
        self.assertEqual(len(read["items"]), 2)
        self.assertEqual(read["next_offset"], 2)
        self.assertLessEqual(len(encode(read)), 2048)
        self.assertNotIn('"content":', encode(read))

    def test_project_isolation(self):
        self.source()
        first = self.release()
        other = self.r.project(Path(self.temp.name) / "other")
        self.assertEqual(self.r.list_knowledge_releases(other)["items"], [])
        with self.assertRaises(ContractError):
            self.r.commit_knowledge_release(other, first["release_id"])
        with self.assertRaises(ContractError):
            self.r.read_knowledge_release(other, first["release_id"])

    def test_rollback_returns_target_subset_without_changing_active(self):
        first_knowledge = self.source()
        first = self.release()
        new_knowledge = self.source("chat-b", content="Different remedy")
        head = self.release()
        result = self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"], request_id="rollback-1")
        self.assertTrue(result["changed"])
        self.assertEqual(result["item_count"], 1)
        self.assertFalse(result["active_knowledge_changed"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active WHERE project=?", (self.p,)).fetchone()[0], 2)
        self.assertEqual(self.r.read(self.p, new_knowledge["knowledge_id"])["revision"], 1)
        self.assertEqual(self.r.read_knowledge_release(self.p, result["release_id"])["items"][0]["knowledge_id"], first_knowledge["knowledge_id"])
        self.assertEqual(result, self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"], request_id="rollback-1"))
        # 普通后续发布重新使用当前全部 active，而非把回滚清单当作检索过滤。
        later = self.release()
        self.assertEqual(later["item_count"], 2)

    def test_rollback_requires_parent_cas_and_matching_request(self):
        self.source()
        first = self.release()
        self.source("chat-b", content="Different remedy")
        head = self.release()
        with self.assertRaises(ContractError):
            self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=first["release_id"])
        result = self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"], request_id="rollback-id")
        with self.assertRaises(ContractError):
            self.r.rollback_knowledge_release(self.p, head["release_id"], expected_head=head["release_id"], request_id="rollback-id")
        self.assertEqual(self.r._release_head(self.p), result["release_id"])

    def test_rollback_cannot_revive_revoked_or_replaced_knowledge(self):
        knowledge = self.source()
        first = self.release()
        self.source("chat-b", content="Different remedy")
        head = self.release()
        self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=knowledge["revision"])
        with self.assertRaises(ContractError):
            self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"])
        self.assertEqual(self.r._release_head(self.p), head["release_id"])

    def test_rollback_rejects_counterexample_or_source_change(self):
        knowledge = self.source()
        first = self.release()
        self.source("chat-b", content="Different remedy")
        head = self.release()
        self.feedback(knowledge)
        with self.assertRaises(ContractError):
            self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"])

    def test_rollback_cannot_revert_new_body_revision(self):
        knowledge = self.source()
        first = self.release()
        newer = self.source("chat-b", content="Corrected include directory", target=knowledge)
        head = self.release()
        with self.assertRaises(ContractError):
            self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], newer["revision"])

    def test_rollback_rejects_changed_source_health(self):
        knowledge = self.source()
        first = self.release()
        self.source("chat-b", content="Different remedy")
        head = self.release()
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (knowledge["problem_id"],))
        with self.assertRaises(ContractError):
            self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=head["release_id"])

    def test_rollback_to_current_identical_release_is_unchanged(self):
        self.source()
        first = self.release()
        result = self.r.rollback_knowledge_release(self.p, first["release_id"], expected_head=first["release_id"], request_id="same-head")
        self.assertFalse(result["changed"])
        self.assertEqual(self.r._release_head(self.p), first["release_id"])

    def test_argument_bounds_are_enforced(self):
        for kwargs in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"max_chars": 128}):
            with self.assertRaises(ContractError):
                self.r.list_knowledge_releases(self.p, **kwargs)
        with self.assertRaises(ContractError):
            self.r.prepare_knowledge_release(self.p, max_items=True)
        with self.assertRaises(ContractError):
            self.r.prepare_knowledge_release("unknown")


if __name__ == "__main__":
    unittest.main()
