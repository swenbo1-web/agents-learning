"""Task registration advice is bounded; existing task/source identities stay fixed."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime


NOTICE = "尚无活动任务"


class TaskRegistrationTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("ALP_TEST_ROOT", Path(__file__).resolve().parents[1]
                                  / "build" / "local" / "task-registration1449"))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="registration-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("project", "tmp", "home", "runtime-releases", "codex"):
            (self.root / name).mkdir()
        self.workspace = self.root / "project"
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(config),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1",
            **{key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}}
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        tempdir = patch.object(tempfile, "tempdir", str(self.root / "tmp"))
        tempdir.start()
        self.addCleanup(tempdir.stop)

    def runtime(self, host="codex"):
        runtime = Runtime(self.root / "home" / host, host=host, config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "work_memory": {"enabled": False}})
        self.addCleanup(runtime.close)
        project = runtime.project(self.workspace)
        conversation = runtime.host_conversation("reader")
        handle = runtime.register_context(project, conversation)["context_handle"]
        return runtime, project, conversation, handle

    def context(self, runtime, project, conversation, handle, query="Inspect the project", **kwargs):
        return runtime._context(project, conversation, query=query, context_handle=handle,
                                workspace=self.workspace, **kwargs)

    def update(self, runtime, project, conversation, task, **changes):
        return runtime.task_update(project, conversation, task["goal"], task["scope"],
            expected_task_id=task["id"], expected_write_version=task["write_version"], **changes)

    def test_three_hosts_remind_once_without_creating_a_task(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                r, p, c, h = self.runtime(host)
                first = self.context(r, p, c, h)
                self.assertIn(NOTICE, first)
                self.assertIn("普通问答无需建任务", first)
                self.assertIsNone(r.task(p, c))
                for query in ("继续", "Inspect another file", "Why does this work?"):
                    self.assertNotIn(NOTICE, self.context(r, p, c, h, query))
                self.assertNotIn(NOTICE, self.context(r, p, c, None, tool_only=True))
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM task_snapshots").fetchone()[0], 0)

    def test_active_task_no_advice_then_one_notice_per_inactive_boundary(self):
        r, p, c, h = self.runtime()
        first = r.task_update(p, c, "Inspect the approved files")
        self.assertNotIn(NOTICE, self.context(r, p, c, h))
        done = self.update(r, p, c, first, status="delivered")
        self.assertIn(NOTICE, self.context(r, p, c, h, "继续推进"))
        self.assertEqual(r.task(p, c), done)
        self.assertNotIn(NOTICE, self.context(r, p, c, h, "继续推进"))
        # Result-only bookkeeping cannot cause repeated advice for this boundary.
        amended = r.task_update(p, c, done["goal"], {"result": "Observed report"},
            expected_task_id=done["id"], expected_write_version=done["write_version"])
        self.assertNotIn(NOTICE, self.context(r, p, c, h))
        new = r.task_update(p, c, "Inspect new authorized work", new_task=True,
            expected_task_id=amended["id"], expected_write_version=amended["write_version"])
        self.assertNotEqual(new["id"], done["id"])
        self.assertNotIn(NOTICE, self.context(r, p, c, h))
        self.update(r, p, c, new, status="delivered")
        self.assertIn(NOTICE, self.context(r, p, c, h))

    def test_small_budget_defers_complete_notice_until_tool_context(self):
        r, p, c, h = self.runtime()
        r.config["injection_max_chars"] = 128
        tiny = self.context(r, p, c, h)
        self.assertLessEqual(len(tiny), 128)
        self.assertNotIn(NOTICE, tiny)
        full = self.context(r, p, c, None, tool_only=True)
        self.assertIn(NOTICE, full)
        self.assertTrue(full.endswith("登记前事件不自动归入新阶段。"))
        self.assertLessEqual(len(full), 128)
        self.assertNotIn(NOTICE, self.context(r, p, c, None, tool_only=True))

    def test_compaction_and_host_epoch_restore_advice_once(self):
        r, p, c, h = self.runtime()
        self.assertIn(NOTICE, self.context(r, p, c, h))
        self.assertIn(NOTICE, self.context(r, p, c, h, compact=True))
        self.assertNotIn(NOTICE, self.context(r, p, c, h))
        r.scope_account_changed("test-account-boundary")
        h = r.register_context(p, c)["context_handle"]
        self.assertIn(NOTICE, self.context(r, p, c, h))
        self.assertNotIn(NOTICE, self.context(r, p, c, h))

    def test_resumed_same_contract_has_a_new_inactive_boundary(self):
        r, p, c, h = self.runtime()
        first = r.task_update(p, c, "Inspect one continuing task")
        done = self.update(r, p, c, first, status="delivered")
        self.assertIn(NOTICE, self.context(r, p, c, h))
        resumed = self.update(r, p, c, done, resume=True, status="active")
        self.assertNotIn(NOTICE, self.context(r, p, c, h))
        self.update(r, p, c, resumed, status="delivered")
        self.assertIn(NOTICE, self.context(r, p, c, h))
        self.assertNotIn(NOTICE, self.context(r, p, c, h))

    def test_critical_notifications_use_budget_before_advice(self):
        r, p, c, h = self.runtime()
        r.config["injection_max_chars"] = 128
        critical = "[来源变更]" + "风险" * 60
        with patch.object(r, "_notifications", return_value=[critical]):
            self.assertEqual(self.context(r, p, c, None, tool_only=True), critical)
        state = r.db.execute("SELECT task_notice_key FROM contexts WHERE project=? AND conversation=?", (p, c)).fetchone()
        self.assertIsNone(state[0])
        self.assertIn(NOTICE, self.context(r, p, c, None, tool_only=True))

    def test_reopening_runtime_keeps_advice_deduplication(self):
        r, p, c, h = self.runtime()
        self.assertIn(NOTICE, self.context(r, p, c, h))
        reopened = Runtime(r.home, host="codex", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "work_memory": {"enabled": False}})
        self.addCleanup(reopened.close)
        self.assertNotIn(NOTICE, self.context(reopened, p, c, h))

    def test_child_never_receives_parent_registration_advice(self):
        r, p, c, h = self.runtime()
        r.task_update(p, c, "Inspect parent task")
        child = r.register_context(p, c, "child")["context_handle"]
        parent = r.task(p, c)
        self.update(r, p, c, parent, status="delivered")
        child_text = self.context(r, p, c, child, agent_id="child")
        self.assertNotIn(NOTICE, child_text)
        self.assertIn(NOTICE, self.context(r, p, c, h))

    def test_advice_does_not_rebind_pre_registration_observations(self):
        r, p, c, h = self.runtime()
        old = r.task_update(p, c, "Inspect old work")
        done = self.update(r, p, c, old, status="delivered")
        before = r._event(p, c, "PostToolUse", {"tool_name": "Read"}, outcome="reported",
                          task_id=old["id"], task_revision=old["revision"])[0]
        self.assertIn(NOTICE, self.context(r, p, c, h, "继续推进"))
        new = r.task_update(p, c, "New actual work", new_task=True,
            expected_task_id=done["id"], expected_write_version=done["write_version"])
        saved = r.db.execute("SELECT task_id,task_revision FROM events WHERE id=?", (before["id"],)).fetchone()
        self.assertEqual((saved["task_id"], saved["task_revision"]), (old["id"], old["revision"]))
        self.assertNotEqual(saved["task_id"], new["id"])

    def test_legacy_schema_adds_optional_column_without_changing_task_or_version(self):
        r, p, c, h = self.runtime()
        task = r.task_update(p, c, "Inspect existing database")
        self.context(r, p, c, h)
        schema = r.db.execute("PRAGMA user_version").fetchone()[0]
        # The optional derived field must also repair an already-current database.
        r.db.execute("ALTER TABLE contexts DROP COLUMN task_notice_key")
        r._migrate_context_storage()
        self.assertEqual(r.db.execute("PRAGMA user_version").fetchone()[0], schema)
        self.assertEqual(r.task(p, c), task)
        self.assertIsNone(r.db.execute("SELECT task_notice_key FROM contexts WHERE project=? AND conversation=?", (p, c)).fetchone()[0])
        self.update(r, p, c, task, status="paused")
        self.assertIn(NOTICE, self.context(r, p, c, h))


if __name__ == "__main__":
    unittest.main()
