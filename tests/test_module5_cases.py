"""第五模块真实临时 SQLite：反馈重试、证据生命周期与知识反例版本。"""
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_learning.case_store import CaseStoreMixin
from agent_learning.runtime import ContractError, Runtime


if issubclass(Runtime, CaseStoreMixin):
    CaseRuntime = Runtime
else:
    class CaseRuntime(CaseStoreMixin, Runtime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._migrate_case_store()


class Module5CaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = CaseRuntime(self.home)
        self.addCleanup(lambda: self.r.close())
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "诊断读取路径")
        self.handle = self.r.register_context(self.p, "chat")["context_handle"]

    def args(self, **kwargs):
        body = dict(summary="读取失败", category="operation", resolution="使用正确路径",
                    context_handle=self.handle, expected_task_id=self.task["id"],
                    expected_task_revision=self.task["revision"])
        body.update(kwargs)
        return body

    def feedback(self, **kwargs):
        return self.r.feedback(self.p, "chat", **self.args(**kwargs))

    def event(self, outcome="failed", agent=None, task=None):
        task = task or self.task
        return self.r.record_event(self.p, "chat", "PostToolUse", {"tool_name": "Read", "tool_response": {"exit_code": 0 if outcome == "succeeded" else 1}},
                                   task_id=task["id"], task_revision=task["revision"], agent_id=agent,
                                   task_write_version=task["write_version"], execution_epoch=task["execution_epoch"], outcome=outcome)

    def publish(self, title="路径检查", conversation="chat"):
        problem = self.r.feedback(self.p, conversation, title + " source", category="operation")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
                                            title=title, content="读取正确路径 " + title, category="operation", applicability="路径错误",
                                            limitations="仅报告，未验收", evidence_refs=refs, validation_level="reported")
        return self.r.publish(self.p, candidate["candidate_id"])

    def test_feedback_replay_returns_first_receipt_without_new_evidence(self):
        first = self.feedback(idempotency_key="request-a")
        second = self.feedback(idempotency_key="request-a")
        self.assertEqual(first, second)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Feedback'").fetchone()[0], 1)
        self.assertEqual(second["revision"], 1)

    def test_feedback_same_key_different_body_rejected_without_side_effect(self):
        self.feedback(idempotency_key="request-a")
        with self.assertRaisesRegex(ContractError, "different feedback"):
            self.feedback(idempotency_key="request-a", resolution="另一个处理方法")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM feedback_receipts").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Feedback'").fetchone()[0], 1)

    def test_same_body_new_key_and_no_key_are_real_repeated_occurrences(self):
        first = self.feedback(idempotency_key="request-a")
        second = self.feedback(idempotency_key="request-b")
        third = self.feedback()
        fourth = self.feedback()
        self.assertEqual({first["id"], second["id"], third["id"], fourth["id"]}, {first["id"]})
        self.assertEqual([first["revision"], second["revision"], third["revision"], fourth["revision"]], [1, 2, 3, 4])

    def test_replay_after_task_changed_returns_original_but_new_request_is_stale(self):
        saved = self.args(idempotency_key="request-a")
        first = self.r.feedback(self.p, "chat", **saved)
        self.r.task_update(self.p, "chat", "另一项任务", new_task=True)
        self.assertEqual(self.r.feedback(self.p, "chat", **saved), first)
        with self.assertRaisesRegex(ContractError, "stale feedback task identity"):
            self.r.feedback(self.p, "chat", **dict(saved, idempotency_key="request-b"))

    def test_receipts_are_separate_for_agents_and_account_epochs(self):
        handles = [self.r.register_context(self.p, "chat", name)["context_handle"] for name in ("child-a", "child-b")]
        for handle in handles:
            self.r.feedback(self.p, "chat", "同一报告", context_handle=handle, idempotency_key="same-key")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM feedback_receipts").fetchone()[0], 2)
        first = self.feedback(idempotency_key="same-key")
        self.r.account_changed("new-account")
        with self.assertRaises(ContractError):
            self.feedback(idempotency_key="same-key")
        self.handle = self.r.register_context(self.p, "chat")["context_handle"]
        second = self.feedback(idempotency_key="same-key")
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM feedback_receipts").fetchone()[0], 4)

    def test_invalid_feedback_keys_are_rejected(self):
        for key in ("", " ", "x" * 201, 123):
            with self.subTest(key=str(key)[:20]), self.assertRaises(ContractError):
                self.feedback(idempotency_key=key)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Feedback'").fetchone()[0], 0)

    def test_absent_or_other_project_knowledge_is_rejected_atomically(self):
        other_workspace = Path(self.temp.name) / "other"
        other_workspace.mkdir()
        other = self.r.project(other_workspace)
        self.r.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (other, "other-knowledge", 1, "{}", 1))
        self.r.db.execute("INSERT INTO active VALUES(?,?,?)", (other, "other-knowledge", 1))
        for ident in ("missing", "other-knowledge"):
            with self.subTest(ident=ident), self.assertRaisesRegex(ContractError, "existing project knowledge revision"):
                self.feedback(knowledge_id=ident)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Feedback'").fetchone()[0], 0)

    def test_implicit_current_revision_is_saved_and_counterexample_is_visible(self):
        k = self.publish()
        before = self.r.read(self.p, k["knowledge_id"])
        problem = self.feedback(knowledge_id=k["knowledge_id"], idempotency_key="counterexample-a")
        after = self.r.read(self.p, k["knowledge_id"])
        self.assertEqual(problem["knowledge_revision"], k["revision"])
        state = self.r._knowledge_feedback_state(self.p, k["knowledge_id"], k["revision"])
        self.assertEqual(state, {"pending_counterexample_count": 1, "feedback_status": "pending_counterexample"})
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertEqual(after["source_health"], before["source_health"])
        self.assertEqual(after["problem_changed"], before["problem_changed"])
        replay = self.feedback(knowledge_id=k["knowledge_id"], idempotency_key="counterexample-a")
        self.assertEqual(replay, problem)
        self.assertEqual(self.r.read(self.p, k["knowledge_id"])["source_version"], after["source_version"])

    def test_explicit_historical_revision_does_not_warn_current_revision(self):
        k = self.publish()
        row = self.r.db.execute("SELECT body FROM knowledge WHERE project=? AND id=? AND revision=?",
                                (self.p, k["knowledge_id"], k["revision"])).fetchone()
        with self.r.transaction():
            self.r.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (self.p, k["knowledge_id"], 2, row[0], 2))
            self.r.db.execute("UPDATE active SET revision=2 WHERE project=? AND id=?", (self.p, k["knowledge_id"]))
        problem = self.feedback(knowledge_id=k["knowledge_id"], knowledge_revision=1)
        self.assertEqual(problem["knowledge_revision"], 1)
        self.assertEqual(self.r._knowledge_feedback_state(self.p, k["knowledge_id"], 1)["pending_counterexample_count"], 1)
        self.assertEqual(self.r._knowledge_feedback_state(self.p, k["knowledge_id"], 2)["pending_counterexample_count"], 0)

    def test_revoked_knowledge_requires_explicit_historical_revision(self):
        k = self.publish()
        self.r.revoke(self.p, k["knowledge_id"], expected_revision=self.r.read(self.p, k["knowledge_id"])["revision"])
        with self.assertRaises(ContractError):
            self.feedback(knowledge_id=k["knowledge_id"])
        accepted = self.feedback(knowledge_id=k["knowledge_id"], knowledge_revision=k["revision"])
        self.assertEqual(accepted["knowledge_revision"], k["revision"])

    def test_observation_link_does_not_create_counterexample_and_invalid_kind_rejected(self):
        k = self.publish()
        self.feedback(knowledge_id=k["knowledge_id"], feedback_kind="observation")
        self.assertEqual(self.r._knowledge_feedback_state(self.p, k["knowledge_id"], k["revision"])["feedback_status"], "none")
        with self.assertRaises(ContractError):
            self.feedback(feedback_kind="counterexample")
        with self.assertRaises(ContractError):
            self.feedback(feedback_kind="resolved")

    def test_reviewed_case_keeps_unresolved_counterexample_and_dirty_source(self):
        k = self.publish()
        problem = self.feedback(knowledge_id=k["knowledge_id"])
        before = self.r.read(self.p, k["knowledge_id"])["source_version"]
        with self.r.transaction():
            self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (problem["id"],))
        reviewed = self.r.read(self.p, k["knowledge_id"])
        self.assertGreater(reviewed["source_version"], before)
        self.assertEqual(self.r._knowledge_feedback_state(self.p, k["knowledge_id"], k["revision"])["pending_counterexample_count"], 1)
        self.feedback(knowledge_id=k["knowledge_id"])
        self.assertEqual(self.r._knowledge_feedback_state(self.p, k["knowledge_id"], k["revision"])["pending_counterexample_count"], 1)
        self.assertGreater(self.r.read(self.p, k["knowledge_id"])["source_version"], reviewed["source_version"])

    def test_case_batch_revision_once_duplicate_stable_and_role_change_is_material(self):
        first, second = self.event(), self.event()
        problem = self.r._problem(self.p, "chat", "same failure", "operation", [first["id"], second["id"]],
                                  evidence_roles={first["id"]: "failure", second["id"]: "attempt"})
        self.assertEqual(problem["revision"], 1)
        replay = self.r._attach_case_evidence(problem["id"], [(first["id"], "failure"), (second["id"], "attempt")])
        self.assertEqual(replay["revision"], 1)
        changed = self.r._attach_case_evidence(problem["id"], [(second["id"], "failure")])
        self.assertEqual(changed["revision"], 2)

    def test_reviewed_case_reopens_on_real_evidence_without_claiming_resolved(self):
        failed = self.event()
        problem = self.r._problem(self.p, "chat", "failure", "operation", [failed["id"]], evidence_roles={failed["id"]: "failure"})
        self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (problem["id"],))
        success = self.event("succeeded")
        updated = self.r._attach_case_evidence(problem["id"], [(success["id"], "potential_recovery")])
        self.assertEqual((updated["status"], updated["resolution_status"], updated["revision"]), ("pending", "recovery_observed", 2))
        later = self.event()
        failed_again = self.r._attach_case_evidence(problem["id"], [(later["id"], "failure")])
        self.assertEqual(failed_again["resolution_status"], "open")
        with self.assertRaises(ContractError):
            self.r._attach_case_evidence(problem["id"], [], resolution_status="resolved")

    def test_case_attachment_rejects_other_task_and_stale_revision_atomically(self):
        failed = self.event()
        problem = self.r._problem(self.p, "chat", "failure", "operation", [failed["id"]])
        another = self.r.task_update(self.p, "chat", "another", new_task=True)
        unrelated = self.event(task=another)
        with self.assertRaises(ContractError):
            self.r._attach_case_evidence(problem["id"], [(unrelated["id"], "failure")])
        with self.assertRaisesRegex(ContractError, "problem changed"):
            self.r._attach_case_evidence(problem["id"], [], expected_revision=0)
        self.assertEqual(self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()[0], 1)

    def test_explicit_attachment_can_include_cross_agent_evidence_without_auto_matching(self):
        first = self.event(agent="a")
        second = self.event("succeeded", agent="b")
        problem = self.r._problem(self.p, "chat", "joint verification", "operation", [first["id"]],
                                  evidence_roles={first["id"]: "failure"})
        updated = self.r._attach_case_evidence(problem["id"], [(second["id"], "observation")])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["resolution_status"], "open")

    def test_existing_roles_are_preserved_when_problem_called_without_role_overrides(self):
        failed = self.event()
        a = self.r._problem(self.p, "chat", "failure", "operation", [failed["id"]], evidence_roles={failed["id"]: "failure"})
        b = self.r._problem(self.p, "chat", "failure", "operation", [failed["id"]])
        self.assertEqual(a, b)
        self.assertEqual(self.r.db.execute("SELECT role FROM problem_events WHERE problem=?", (a["id"],)).fetchone()[0], "failure")

    def test_schema6_migration_keeps_case_ids_and_does_not_guess_knowledge_revision(self):
        k = self.publish()
        legacy = self.feedback(knowledge_id=k["knowledge_id"])
        event_refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (legacy["id"],))]
        self.r.close()
        with contextlib.closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db, db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'case_feedback_%'").fetchall():
                db.execute(f'DROP TRIGGER "{row[0]}"')
            for table in ("knowledge_feedback", "feedback_receipts", "delivery_rounds"):
                db.execute(f"DROP TABLE {table}")
            db.execute("ALTER TABLE problems DROP COLUMN knowledge_revision")
            db.execute("ALTER TABLE problems DROP COLUMN resolution_status")
            db.execute("ALTER TABLE problem_events DROP COLUMN role")
            db.execute("ALTER TABLE events DROP COLUMN tool_input_digest")
            db.execute("PRAGMA user_version=6")
        self.r = CaseRuntime(self.home)
        migrated = dict(self.r.db.execute("SELECT * FROM problems WHERE id=?", (legacy["id"],)).fetchone())
        self.assertEqual(migrated["id"], legacy["id"])
        self.assertEqual(migrated["revision"], legacy["revision"])
        self.assertEqual(migrated["resolution_status"], "open")
        self.assertIsNone(migrated["knowledge_revision"])
        self.assertEqual([row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (legacy["id"],))], event_refs)
        self.assertEqual(self.r.db.execute("SELECT role FROM problem_events WHERE problem=?", (legacy["id"],)).fetchone()[0], "observation")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_feedback").fetchone()[0], 0)
        self.assertIsNone(self.r.db.execute("SELECT tool_input_digest FROM events WHERE id=?", (event_refs[0],)).fetchone()[0])
        self.r.close()
        self.r = CaseRuntime(self.home)
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 17)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problems WHERE id=?", (legacy["id"],)).fetchone()[0], 1)

    def test_migration_backfills_only_proven_legacy_delivery_round(self):
        delivered = self.r.task_update(self.p, "chat", self.task["goal"], status="delivered")
        self.r.handle_hook(dict(hook_event_name="Stop", cwd=str(self.workspace), session_id="chat", turn_id="first",
                                last_assistant_message="首次交付（未验收）"))
        original = dict(self.r.db.execute("SELECT * FROM delivery_rounds WHERE project=?", (self.p,)).fetchone())
        with self.r.transaction():
            self.r.db.execute("DELETE FROM delivery_rounds")
            self.r.db.execute("PRAGMA user_version=6")
        self.r._migrate_case_store()
        restored = dict(self.r.db.execute("SELECT * FROM delivery_rounds WHERE project=?", (self.p,)).fetchone())
        # 旧库没有轮次插入时点；恢复身份必须精确，时间使用真实Stop来源时点。
        self.assertEqual({k: v for k, v in restored.items() if k != "created"},
                         {k: v for k, v in original.items() if k != "created"})
        self.assertEqual(restored["created"], self.r.db.execute("SELECT created FROM events WHERE id=?",
                                                               (restored["event_id"],)).fetchone()[0])
        self.assertEqual(restored["write_version"], delivered["write_version"])
        self.r.handle_hook(dict(hook_event_name="Stop", cwd=str(self.workspace), session_id="chat", turn_id="second",
                                last_assistant_message="同一交付轮次的补充说明"))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM delivery_rounds").fetchone()[0], 1)
        with self.r.transaction():
            self.r.db.execute("DELETE FROM delivery_rounds")
            self.r.db.execute("UPDATE events SET task_write_version=NULL WHERE id=?", (original["event_id"],))
            self.r.db.execute("PRAGMA user_version=6")
        self.r._migrate_case_store()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM delivery_rounds").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
