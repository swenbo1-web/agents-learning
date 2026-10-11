"""模块四迁移与来源版本真实 SQLite 检查，不运行宿主或模型。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.context_storage import ContextStorageMixin
from agent_learning.knowledge_index import INDEX_VERSION
from agent_learning.runtime import Runtime, encode


class StorageRuntime(Runtime, ContextStorageMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._migrate_context_storage()


class Module4StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = StorageRuntime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "检查来源变化", {"mode": "analysis"})

    def hook(self, kind, **fields):
        return self.r.handle_hook(dict(hook_event_name=kind, session_id="chat", cwd=str(self.workspace), **fields))

    def publish(self, summary="source", content="compiler guidance", refs=None, project=None, conversation="chat"):
        project = project or self.p
        problem = self.r.feedback(project, conversation, summary, "operation", event_refs=refs)
        packet = self.r.review_packet(project, problem["id"])
        candidate = self.r.submit_candidate(project, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler reference", content=content, category="operation", applicability="compiler environments",
            limitations="verify current environment", validation_level="reported",
            evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(project, candidate["candidate_id"]), problem

    def version(self, knowledge, project=None):
        return self.r.db.execute("SELECT version FROM knowledge_source_versions WHERE project=? AND knowledge_id=? "
                                 "AND revision=?", (project or self.p, knowledge["knowledge_id"], knowledge["revision"])).fetchone()[0]

    def epoch(self, project=None):
        return self.r.db.execute("SELECT source_epoch FROM projects WHERE id=?", (project or self.p,)).fetchone()[0]

    def downgrade_projection(self):
        for row in self.r.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'context_source_%'").fetchall():
            self.r.db.execute(f"DROP TRIGGER {row[0]}")
        for table in ("context_exposures", "context_faults", "knowledge_source_versions"):
            self.r.db.execute(f"DROP TABLE {table}")
        self.r.db.execute("PRAGMA user_version=5")

    def test_migration_preserves_parent_child_history_and_old_baseline(self):
        knowledge, _ = self.publish()
        ident = knowledge["knowledge_id"] + "@1"
        baseline = {"health": "valid", "current_tool_success": False, "problem_changed": False}
        self.r.db.execute("INSERT INTO contexts(project,conversation,injected,shown,source_states) VALUES(?,?,?,?,?)",
                          (self.p, "chat", encode([ident, "older@1"]), encode([ident, "invalid", "bad@x"]),
                           encode([[ident, baseline]])))
        self.r.db.execute("INSERT INTO subagent_contexts(project,conversation,agent,injected,shown,source_states) VALUES(?,?,?,?,?,?)",
                          (self.p, "chat", "child", encode([ident]), "[]", "{}"))
        self.downgrade_projection()
        self.r._migrate_context_storage()
        rows = self.r.db.execute("SELECT * FROM context_exposures ORDER BY agent,knowledge_id").fetchall()
        self.assertEqual(len(rows), 3)
        known = next(row for row in rows if row["agent"] == "" and row["knowledge_id"] == knowledge["knowledge_id"])
        self.assertEqual(json.loads(known["state"]), baseline)
        self.assertTrue(all(row["source_version"] == -1 for row in rows))
        child = next(row for row in rows if row["agent"] == "child")
        self.assertEqual(json.loads(child["state"]), {})
        self.assertEqual(self.version(knowledge), 0)
        # 单独调用第四模块迁移只推进到6，完整Runtime另运行案例迁移到7。
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 6)
        self.r._migrate_context_storage()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 3)

    def test_migration_failure_rolls_back_projection_and_version(self):
        self.downgrade_projection()
        with patch.object(self.r, "_install_context_source_triggers", side_effect=sqlite3.OperationalError("synthetic migration failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.r._migrate_context_storage()
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE name='context_exposures'").fetchone())
        self.assertFalse(self.r.db.in_transaction)

    def test_schema6_checks_index_version_without_replaying_migration(self):
        self.r.db.execute("UPDATE knowledge_index_state SET version=-1")
        with patch.object(self.r, "_rebuild_knowledge_index", wraps=self.r._rebuild_knowledge_index) as rebuild:
            self.r._migrate_context_storage()
            rebuild.assert_called_once()
            self.r._migrate_context_storage()
            rebuild.assert_called_once()
        self.assertEqual(self.r.db.execute("SELECT version FROM knowledge_index_state").fetchone()[0], INDEX_VERSION)

    def test_parent_exposure_uniqueness_and_nonnull_agent(self):
        self.r.db.execute("INSERT INTO context_exposures VALUES(?,?,?,?,?,?,?,?)", (self.p, "chat", "", "knowledge", 1, "{}", -1, 0))
        with self.assertRaises(sqlite3.IntegrityError):
            self.r.db.execute("INSERT INTO context_exposures VALUES(?,?,?,?,?,?,?,?)", (self.p, "chat", "", "knowledge", 1, "{}", -1, 1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.r.db.execute("INSERT INTO context_exposures VALUES(?,?,?,?,?,?,?,?)", (self.p, "chat", None, "knowledge", 1, "{}", -1, 1))

    def test_problem_revision_only_dirties_related_project_and_real_change(self):
        knowledge, problem = self.publish()
        other = self.r.project(Path(self.temp.name) / "other")
        self.r.task_update(other, "other-chat", "other project", {"mode": "analysis"})
        unrelated, _ = self.publish(project=other, conversation="other-chat")
        before = self.version(knowledge), self.epoch(), self.version(unrelated, other), self.epoch(other)
        self.r.db.execute("UPDATE problems SET revision=revision WHERE id=?", (problem["id"],))
        self.assertEqual((self.version(knowledge), self.epoch()), before[:2])
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        self.assertEqual((self.version(knowledge), self.epoch()), (before[0] + 1, before[1] + 1))
        self.assertEqual((self.version(unrelated, other), self.epoch(other)), before[2:])

    def test_operation_health_change_tracks_all_referenced_revisions_and_ignores_state(self):
        self.hook("PreToolUse", tool_use_id="compile", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="compile", tool_name="Bash", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        knowledge, _ = self.publish(refs=[event])
        operation = self.r.db.execute("SELECT operation FROM events WHERE id=?", (event,)).fetchone()[0]
        before = self.version(knowledge), self.epoch()
        self.r.db.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation,))
        self.assertEqual((self.version(knowledge), self.epoch()), before)
        self.r.db.execute("UPDATE operations SET conflict=1 WHERE id=?", (operation,))
        self.assertEqual((self.version(knowledge), self.epoch()), (before[0] + 1, before[1] + 1))
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["source_health"], "conflicted")
        self.r.db.execute("UPDATE operations SET conflict=1 WHERE id=?", (operation,))
        self.assertEqual(self.version(knowledge), before[0] + 1)

    def test_event_outcome_and_deletion_dirty_legacy_fallback_evidence(self):
        knowledge, _ = self.publish()
        source = self.r.db.execute("SELECT * FROM knowledge_sources").fetchone()
        event = json.loads(source["evidence_refs"])[0]
        self.r.db.execute("DELETE FROM knowledge_sources")
        before = self.version(knowledge)
        self.r.db.execute("UPDATE events SET outcome='failed' WHERE id=?", (event,))
        self.assertEqual(self.version(knowledge), before + 1)
        self.r.db.execute("DELETE FROM events WHERE id=?", (event,))
        self.assertEqual(self.version(knowledge), before + 2)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["source_health"], "missing")

    def test_problem_event_link_change_only_dirties_referenced_evidence(self):
        knowledge, problem = self.publish()
        ref = json.loads(self.r.db.execute("SELECT evidence_refs FROM knowledge_sources").fetchone()[0])[0]
        before = self.version(knowledge)
        self.r.db.execute("DELETE FROM problem_events WHERE problem=? AND event=?", (problem["id"], ref))
        self.assertEqual(self.version(knowledge), before + 1)
        self.r.db.execute("INSERT INTO problem_events(problem,event) VALUES(?,?)", (problem["id"], ref))
        self.assertEqual(self.version(knowledge), before + 2)
        unrelated = self.r.record_event(self.p, "chat", "Feedback", {"summary": "unrelated"})["id"]
        self.r.db.execute("INSERT INTO problem_events(problem,event) VALUES(?,?)", (problem["id"], unrelated))
        self.assertEqual(self.version(knowledge), before + 2)

    def test_new_duplicate_source_dirties_revision_without_new_body(self):
        knowledge, _ = self.publish("source one")
        before = self.version(knowledge), self.epoch()
        second, _ = self.publish("source two")
        self.assertEqual(second["knowledge_id"], knowledge["knowledge_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual((self.version(knowledge), self.epoch()), (before[0] + 1, before[1] + 1))


if __name__ == "__main__":
    unittest.main()
