"""采用归属、幂等和动态观测回归；仅使用临时 SQLite 和本地 Hook 输入。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime, digest


class Module8EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module8-effects-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "project"
        self.work.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.work)
        self.r.task_update(self.p, "chat", "Use compiler guidance")
        self.context = self.r.register_context(self.p, "chat")
        self.knowledge = self.publish()

    def hook(self, kind, operation="operation", chat="chat", agent=None, **fields):
        raw = {"hook_event_name": kind, "session_id": chat, "cwd": str(self.work),
               "tool_use_id": operation, "tool_name": "Bash", **fields}
        if agent:
            raw["agent_id"] = agent
        return self.r.handle_hook(raw)

    def operation_id(self, operation="operation", chat="chat", agent=None):
        return digest([chat, agent, operation] if agent else [chat, operation])

    def publish(self, title="compiler solution"):
        self.r.task_update(self.p, "source", "source evidence")
        self.hook("PreToolUse", "source-" + title, chat="source")
        self.hook("PostToolUse", "source-" + title, chat="source", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE conversation='source' AND type='PostToolUse' "
                                 "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        problem = self.r.feedback(self.p, "source", title, category="operation", event_refs=[event])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=title + " guidance", applicability="local compiler", limitations="tool observations",
            category="operation", validation_level="tool_success", evidence_refs=[event])
        return self.r.publish(self.p, candidate["candidate_id"])

    def expose(self, context=None, knowledge=None, method="mcp"):
        context = context or self.context
        knowledge = knowledge or self.knowledge
        item = self.r.read(self.p, knowledge["knowledge_id"])
        if method == "mcp":
            self.r.record_exposure(self.p, context["context_handle"], [item])
        else:
            with self.r.transaction():
                self.r._record_exposure(self.p, context["conversation_id"], context["agent_id"], [item], injected=True)

    def use(self, context=None, knowledge=None, **values):
        context = context or self.context
        knowledge = knowledge or self.knowledge
        task = self.r.session_context(self.p, context["context_handle"])["task"]
        arguments = {"context_handle": context["context_handle"], "knowledge_id": knowledge["knowledge_id"],
                     "knowledge_revision": knowledge["revision"], "expected_task_id": task["id"],
                     "expected_task_revision": task["revision"], "expected_write_version": task["write_version"]}
        arguments.update(values)
        return self.r.knowledge_use(self.p, **arguments)

    def test_exposure_is_not_adoption_and_repeated_scope_is_deduplicated(self):
        self.expose()
        self.expose()
        self.expose(method="hook")
        effects = self.r.learning_effects(self.p)
        self.assertEqual(effects["reported_adoptions"], 0)
        self.assertEqual(effects["exposures"]["mcp"], 1)
        self.assertEqual(effects["exposures"]["hook"], 1)

    def test_reported_use_without_operation_has_no_tool_success(self):
        self.expose()
        receipt = self.use()
        self.assertEqual(receipt["status"], "reported")
        self.assertEqual(receipt["operation_observation"]["outcome"], "unlinked")
        self.assertFalse(receipt["operation_observation"]["tool_success_observed"])
        self.assertEqual(self.r.learning_effects(self.p)["reported_adoptions"], 1)

    def test_unexposed_knowledge_cannot_be_declared(self):
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use()

    def test_other_conversation_exposure_cannot_be_borrowed(self):
        self.expose()
        self.r.task_update(self.p, "other", "same project")
        other = self.r.register_context(self.p, "other")
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use(context=other)

    def test_other_project_handle_cannot_be_borrowed(self):
        self.expose()
        other_work = Path(self.temp.name) / "otherproject"
        other_work.mkdir()
        other_project = self.r.project(other_work)
        task = self.r.task(self.p, "chat")
        with self.assertRaisesRegex(ContractError, "context_mismatch"):
            self.r.knowledge_use(other_project, self.context["context_handle"], self.knowledge["knowledge_id"], 1,
                                 task["id"], task["revision"], task["write_version"])

    def test_exact_task_version_required(self):
        self.expose()
        task = self.r.task(self.p, "chat")
        with self.assertRaisesRegex(ContractError, "stale task"):
            self.use(expected_write_version=task["write_version"] + 1)
        with self.assertRaisesRegex(ContractError, "stale task"):
            self.use(expected_task_revision=task["revision"] + 1)
        with self.assertRaisesRegex(ContractError, "stale task"):
            self.use(expected_task_id="other-task")

    def test_task_change_requires_new_exposure_and_preserves_history(self):
        self.expose()
        original = self.use()
        self.r.task_update(self.p, "chat", "new goal", new_task=True)
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use()
        self.expose()
        newer = self.use()
        self.assertNotEqual(original["task_id"], newer["task_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM learning_exposures").fetchone()[0], 2)
        self.assertEqual(self.r.learning_effects(self.p)["reported_adoptions"], 2)

    def test_write_version_change_cannot_reuse_exposure(self):
        self.expose()
        self.r.task_update(self.p, "chat", "Use compiler guidance", status="paused")
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use()

    def test_account_switch_invalidates_old_handle_and_old_exposure(self):
        self.expose()
        self.r.account_changed("evaluation-switch")
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.use()
        fresh = self.r.register_context(self.p, "chat")
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use(context=fresh)
        self.expose(context=fresh)
        self.assertEqual(self.use(context=fresh)["status"], "reported")
        self.assertEqual(self.r.learning_effects(self.p)["exposures"]["mcp"], 2)

    def test_child_cannot_borrow_parent_exposure_or_sibling_exposure(self):
        self.expose()
        first = self.r.register_context(self.p, "chat", "child-one")
        second = self.r.register_context(self.p, "chat", "child-two")
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use(context=first)
        self.expose(context=first)
        receipt = self.use(context=first)
        self.assertEqual(receipt["agent_id"], "child-one")
        self.assertIsNotNone(receipt["assignment_id"])
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use(context=second)

    def test_stale_child_assignment_rejects_use_even_with_new_exposure(self):
        child = self.r.register_context(self.p, "chat", "child")
        self.expose(context=child)
        self.r.task_update(self.p, "chat", "changed parent contract")
        self.expose(context=child)
        with self.assertRaisesRegex(ContractError, "assignment_stale"):
            self.use(context=child)

    def test_idempotency_replay_and_different_body_rejection(self):
        self.expose()
        first = self.use(idempotency_key="same")
        self.assertEqual(first["adoption_id"], self.use(idempotency_key="same")["adoption_id"])
        second_knowledge = self.publish("other compiler solution")
        self.expose(knowledge=second_knowledge)
        with self.assertRaisesRegex(ContractError, "different knowledge use"):
            self.use(knowledge=second_knowledge, idempotency_key="same")
        self.assertEqual(self.r.learning_effects(self.p)["reported_adoptions"], 1)

    def test_no_explicit_key_replays_the_exact_body(self):
        self.expose()
        self.assertEqual(self.use()["adoption_id"], self.use()["adoption_id"])

    def test_idempotency_key_cannot_be_shared_by_agents(self):
        self.expose()
        self.use(idempotency_key="shared-key")
        child = self.r.register_context(self.p, "chat", "child")
        self.expose(context=child)
        with self.assertRaisesRegex(ContractError, "different knowledge use"):
            self.use(context=child, idempotency_key="shared-key")

    def test_revoke_rejects_new_declaration_but_replay_reports_current_state(self):
        self.expose()
        first = self.use(idempotency_key="before-revoke")
        self.r.revoke(self.p, self.knowledge["knowledge_id"], expected_revision=1)
        with self.assertRaisesRegex(ContractError, "not currently active"):
            self.use(idempotency_key="after-revoke")
        replay = self.use(idempotency_key="before-revoke")
        self.assertEqual(replay["adoption_id"], first["adoption_id"])
        self.assertFalse(replay["knowledge_state"]["active"])

    def test_linked_success_is_recomputed_after_conflicting_post(self):
        self.expose()
        self.hook("PreToolUse")
        self.hook("PostToolUse", tool_response={"exit_code": 0}, event_id="post-success")
        first = self.use(operation_id=self.operation_id(), idempotency_key="tool-observation")
        self.assertTrue(first["operation_observation"]["tool_success_observed"])
        self.hook("PostToolUse", tool_response={"exit_code": 1}, event_id="post-conflict")
        replay = self.use(operation_id=self.operation_id(), idempotency_key="tool-observation")
        self.assertEqual(replay["operation_observation"]["outcome"], "conflict")
        self.assertFalse(replay["operation_observation"]["tool_success_observed"])
        self.assertEqual(self.r.learning_effects(self.p)["current_observation_sample"]["operation_outcomes"], {"conflict": 1})

    def test_denied_operation_remains_observation_not_success(self):
        self.r.config["enforce_task_policy"] = True
        self.expose()
        self.hook("PreToolUse")
        receipt = self.use(operation_id=self.operation_id())
        self.assertEqual(receipt["operation_observation"]["outcome"], "denied")
        self.assertFalse(receipt["operation_observation"]["tool_success_observed"])

    def test_operation_without_pre_cannot_be_linked(self):
        self.expose()
        self.hook("PostToolUse", tool_response={"exit_code": 0})
        with self.assertRaisesRegex(ContractError, "operation_scope_mismatch|operation_pre_required"):
            self.use(operation_id=self.operation_id())

    def test_operation_before_exposure_cannot_be_linked(self):
        self.hook("PreToolUse")
        self.hook("PostToolUse", tool_response={"exit_code": 0})
        self.expose()
        with self.assertRaisesRegex(ContractError, "exposure_after_operation"):
            self.use(operation_id=self.operation_id())

    def test_operation_from_other_task_cannot_be_linked(self):
        self.expose()
        self.hook("PreToolUse")
        old_operation = self.operation_id()
        self.r.task_update(self.p, "chat", "new task", new_task=True)
        self.expose()
        with self.assertRaisesRegex(ContractError, "operation_scope_mismatch"):
            self.use(operation_id=old_operation)

    def test_child_operation_matches_assignment_and_cannot_borrow_parent(self):
        child = self.r.register_context(self.p, "chat", "child")
        self.expose(context=child)
        self.hook("PreToolUse")
        with self.assertRaisesRegex(ContractError, "operation_scope_mismatch"):
            self.use(context=child, operation_id=self.operation_id())
        self.hook("PreToolUse", agent="child")
        self.hook("PostToolUse", agent="child", tool_response={"exit_code": 0})
        self.assertTrue(self.use(context=child, operation_id=self.operation_id(agent="child"))["operation_observation"]["tool_success_observed"])

    def test_knowledge_source_conflict_is_recomputed(self):
        self.expose()
        first = self.use(idempotency_key="source-state")
        self.assertEqual(first["knowledge_state"]["source_health"], "valid")
        self.hook("PostToolUse", "source-compiler solution", chat="source", tool_response={"exit_code": 1})
        replay = self.use(idempotency_key="source-state")
        self.assertEqual(replay["knowledge_state"]["source_health"], "conflicted")
        self.assertEqual(replay["status"], "reported")
        self.assertEqual(self.r.learning_effects(self.p)["current_observation_sample"]["knowledge_source_health"], {"conflicted": 1})

    def test_time_window_and_invalid_days(self):
        self.expose()
        self.use()
        with patch("agent_learning.learning_evaluation.time.time", return_value=0):
            effects = self.r.learning_effects(self.p, days=1)
        self.assertEqual(effects["reported_adoptions"], 0)
        self.assertEqual(effects["exposures"]["mcp"], 0)
        for invalid in (0, -1, 366, True, 1.5, "30"):
            with self.assertRaises(ContractError):
                self.r.learning_effects(self.p, invalid)

    def test_pending_counterexample_is_reported_without_promoting_to_success(self):
        self.expose()
        first = self.use(idempotency_key="counterexample")
        self.r.feedback(self.p, "chat", "Contrary compiler observation", category="operation",
                        knowledge_id=self.knowledge["knowledge_id"], knowledge_revision=1,
                        feedback_kind="counterexample")
        receipt = self.use(idempotency_key="counterexample")
        self.assertEqual(receipt["adoption_id"], first["adoption_id"])
        self.assertGreater(receipt["knowledge_state"]["pending_counterexample_count"], 0)
        self.assertEqual(receipt["status"], "reported")
        self.assertEqual(self.r.learning_effects(self.p)["current_observation_sample"]["pending_counterexamples"], 1)

    def test_missing_pre_after_adoption_removes_success_observation(self):
        self.expose()
        self.hook("PreToolUse")
        self.hook("PostToolUse", tool_response={"exit_code": 0})
        self.use(operation_id=self.operation_id(), idempotency_key="pre-removed")
        with self.r.transaction():
            self.r.db.execute("DELETE FROM events WHERE project=? AND operation=? AND type='PreToolUse'",
                              (self.p, self.operation_id()))
        receipt = self.use(operation_id=self.operation_id(), idempotency_key="pre-removed")
        self.assertEqual(receipt["operation_observation"]["outcome"], "unbound")
        self.assertFalse(receipt["operation_observation"]["tool_success_observed"])

    def test_effect_sample_is_bounded_and_explicitly_truncated(self):
        self.expose()
        for index in range(205):
            self.use(idempotency_key="item-" + str(index))
        effects = self.r.learning_effects(self.p)
        self.assertEqual(effects["reported_adoptions"], 205)
        sample = effects["current_observation_sample"]
        self.assertEqual(sample["size"], 200)
        self.assertTrue(sample["truncated"])
        self.assertEqual(sample["operation_outcomes"], {"unlinked": 200})

    def test_taskless_exposure_is_not_assigned_to_any_future_task(self):
        context = self.r.register_context(self.p, "taskless")
        self.expose(context=context)
        self.r.task_update(self.p, "taskless", "new task after exposure")
        with self.assertRaisesRegex(ContractError, "task_exposure_required"):
            self.use(context=context)

    def test_internal_exposure_does_not_create_nested_transaction(self):
        with self.r.transaction():
            item = self.r.read(self.p, self.knowledge["knowledge_id"])
            inserted = self.r.record_task_exposure(self.p, "chat", None, [item], "hook")
            self.assertEqual(inserted, 1)
        with self.assertRaisesRegex(ContractError, "caller's transaction"):
            self.r.record_task_exposure(self.p, "chat", None, [item], "hook")

    def test_invalid_types_and_revision_are_rejected(self):
        self.expose()
        for values in ({"knowledge_revision": True}, {"expected_write_version": 0},
                       {"operation_id": ""}, {"idempotency_key": "x" * 201}, {"expected_epoch": True}):
            with self.assertRaises(ContractError):
                self.use(**values)
        with self.assertRaisesRegex(ContractError, "not currently active"):
            self.use(knowledge_revision=2)

    def test_existing_exposure_rows_survive_runtime_reopen(self):
        self.expose()
        self.use()
        self.r.close()
        reopened = Runtime(self.home)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.learning_effects(self.p)["reported_adoptions"], 1)
        self.assertEqual(reopened.learning_effects(self.p)["exposures"]["mcp"], 1)


if __name__ == "__main__":
    unittest.main()
