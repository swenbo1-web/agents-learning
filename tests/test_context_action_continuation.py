"""Action-only follow-ups preserve focus without granting execution authority."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.knowledge_context import _continuation_query
from agent_learning.runtime import Runtime


class ContextActionContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-action-continuation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "workspace"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex")}
        environment.update({key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "workspace"
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = "follow-up"
        self.handle = self.r.register_context(self.p, self.conversation)["context_handle"]

    def context(self, query="", **kwargs):
        return self.r._context(self.p, self.conversation, query=query,
            context_handle=self.handle, workspace=self.workspace, **kwargs)

    def saved_query(self):
        return self.r.db.execute("SELECT last_query FROM contexts WHERE project=? AND conversation=?",
            (self.p, self.conversation)).fetchone()[0]

    def task(self, goal="SQLite lock_timeout recovery"):
        return self.r.task_update(self.p, self.conversation, goal, {"mode": "analysis"})

    def test_explicit_action_only_followups_are_recognized(self):
        for query in ("继续优化", "继续优化，深度优化好", "确认深度优化", "按你说的继续优化",
                      "请继续深入分析并且优化一下", "接着推送安装部署并测试", "确认安装部署",
                      "继续分析", "continue optimizing", "keep improving", "continue to refine and test",
                      "please continue with the optimization", "continue deeply analyzing",
                      "confirm deployment", "continue installing, then test"):
            with self.subTest(query=query):
                self.assertTrue(_continuation_query(query))

    def test_subjects_and_bare_new_actions_are_not_continuations(self):
        for query in ("继续优化SQLite检索", "确认深度优化管理员预算", "继续分析 E_LOCK_TIMEOUT",
                      "继续安装新插件", "按你说的优化 src/cache.py", "确认部署 v2.1",
                      "推送安装本地深度测试", "安装", "部署", "深度优化", "分析新的问题",
                      "continue optimizing the queue", "continue analyzing E_LOCK_TIMEOUT",
                      "confirm deployment v2.1", "install", "please optimize", "deploy the service"):
            with self.subTest(query=query):
                self.assertFalse(_continuation_query(query))

    def test_followups_reuse_focus_and_unchanged_plan_without_requery(self):
        focus = "SQLite lock_timeout recovery"
        self.context(focus)
        with patch.object(self.r, "search", side_effect=AssertionError("unchanged follow-up queried")):
            for query in ("继续优化", "确认深度优化", "keep improving", "continue to refine and test"):
                self.context(query)
                self.assertEqual(self.saved_query(), focus)

    def test_followup_without_focus_never_selects_recent_history(self):
        with self.r.transaction():
            event, _ = self.r._event(self.p, self.conversation, "UserPromptSubmit",
                {"prompt": "继续优化，深度优化好"}, outcome="reported")
            captured = self.r._work_capture_event(self.p, event["id"], self.workspace)
        with patch.object(self.r, "search", side_effect=AssertionError("unfocused search")), \
                patch.object(self.r, "work_context_items", side_effect=AssertionError("unfocused history")):
            text = self.context("继续优化，深度优化好")
        self.assertNotIn(captured["memory_id"], text)
        self.assertEqual(self.saved_query(), "")

    def test_polluted_action_focus_recovers_explicit_task_goal(self):
        task = self.task()
        with self.r.transaction():
            self.r.db.execute("UPDATE contexts SET last_query=? WHERE project=? AND conversation=?",
                ("继续优化", self.p, self.conversation))
        with patch.object(self.r, "search", return_value={"items": []}) as search:
            self.context("确认深度优化")
        self.assertEqual(search.call_args.args[1], task["goal"])

    def test_specific_new_subject_replaces_old_focus(self):
        self.context("SQLite lock_timeout recovery")
        query = "继续优化Python memory leak"
        with patch.object(self.r, "search", return_value={"items": []}) as search:
            self.context(query)
        self.assertEqual(search.call_args.args[1], query)
        self.assertEqual(self.saved_query(), query)

    def test_bare_deployment_action_starts_its_own_query(self):
        self.context("SQLite lock_timeout recovery")
        with patch.object(self.r, "search", return_value={"items": []}) as search:
            self.context("部署")
        self.assertEqual(search.call_args.args[1], "部署")
        self.assertEqual(self.saved_query(), "部署")

    def test_new_task_cannot_inherit_previous_task_query(self):
        old = self.task()
        self.context(old["goal"])
        current = self.r.task_update(self.p, self.conversation, "Python memory leak", new_task=True)
        with patch.object(self.r, "search", return_value={"items": []}) as search:
            self.context("继续优化")
        self.assertEqual(search.call_args.args[1], current["goal"])
        self.assertEqual(self.saved_query(), current["goal"])
        self.assertNotEqual(old["id"], current["id"])

    def test_generic_task_goal_does_not_become_a_search_subject(self):
        self.task("继续优化")
        with patch.object(self.r, "search", side_effect=AssertionError("generic task queried")), \
                patch.object(self.r, "work_context_items", side_effect=AssertionError("generic task history")):
            self.context("确认深度优化")
        self.assertEqual(self.saved_query(), "")

    def test_compaction_rechecks_saved_focus_instead_of_generic_action(self):
        focus = "SQLite lock_timeout recovery"
        self.context(focus)
        with patch.object(self.r, "search", return_value={"items": []}) as search:
            self.context("继续优化", compact=True)
        self.assertEqual(search.call_args.args[1], focus)
        self.assertEqual(self.saved_query(), focus)

    def test_missing_native_input_does_not_infer_action_continuation(self):
        focus = "SQLite lock_timeout recovery"
        self.context(focus)
        with patch.object(self.r, "search", side_effect=AssertionError("missing input reused stale focus")):
            self.context(_query_available=False)
        self.assertEqual(self.saved_query(), focus)

    def test_followup_does_not_resume_delivered_task_or_extend_permission(self):
        self.task()
        self.r.task_update(self.p, self.conversation, "SQLite lock_timeout recovery", status="delivered")
        before = self.r.task(self.p, self.conversation)
        self.context("继续安装部署")
        after = self.r.task(self.p, self.conversation)
        self.assertEqual(after["status"], "delivered")
        for field in ("id", "revision", "write_version", "execution_epoch", "scope"):
            self.assertEqual(before[field], after[field])


if __name__ == "__main__":
    unittest.main()
