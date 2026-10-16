"""工作索引及渐进读取的隔离 SQLite 回归，不调用宿主或模型。"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.runtime import Runtime, ContractError
from agent_learning.work_memory import WorkMemoryMixin, install_work_memory, validate_work_memory_config


class TestRuntime(Runtime, WorkMemoryMixin):
    pass


class WorkMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = TestRuntime(Path(self.temp.name) / "state", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        with self.r.transaction():
            install_work_memory(self.r)

    def event(self, kind="PostToolUse", *, outcome="succeeded", operation=None, conversation="chat", **payload):
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, kind, payload, outcome=outcome, operation_id=operation)
            projected = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return event, projected

    def write(self, path="src/a.py", **options):
        return self.event(tool_name="write_file", tool_input={"path": path},
                          tool_response={"exit_code": 0, "output": "animation mapping modified"}, **options)

    def test_write_observation_is_searchable_without_becoming_knowledge(self):
        event, created = self.write()
        card = self.r.work_search(self.p, "animation")["items"][0]
        self.assertEqual(card["memory_id"], created["memory_id"])
        self.assertEqual(card["state"], "succeeded")
        self.assertEqual(card["validation_level"], "tool_observed")
        self.assertEqual(card["source_event_refs"], [event["id"]])
        self.assertFalse(card["permission_restored"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.search(self.p, "animation")["items"], [])

    def test_replay_projection_is_idempotent(self):
        event, original = self.write()
        with self.r.transaction():
            replay = self.r._work_capture_event(self.p, event["id"], self.workspace)
        self.assertFalse(replay["indexed"])
        self.assertEqual(original["memory_id"], replay["memory_id"])
        self.assertEqual(self.r.work_memory_status(self.p)["memories"], 1)

    def test_read_and_learning_tool_do_not_create_observations(self):
        _, result = self.event(tool_name="read_file", tool_input={"path": "src/a.py"}, tool_response={"exit_code": 0})
        self.assertIsNone(result)
        _, result = self.event(outcome="failed", tool_name="mcp__agent_learning__work_search", tool_response={"is_error": True})
        self.assertIsNone(result)
        self.assertEqual(self.r.work_memory_status(self.p)["memories"], 0)
        self.assertEqual(self.r.work_memory_status(self.p)["file_relations"], 1)

    def test_pre_targets_link_to_post_with_no_arguments(self):
        self.event("PreToolUse", outcome="unknown", operation="op", tool_name="write_file", tool_input={"path": "src/a.py"})
        _, created = self.event(operation="op", tool_name="write_file", tool_response={"exit_code": 0})
        result = self.r.work_search(self.p, file_path="src/a.py", workspace=self.workspace)
        self.assertEqual(result["items"][0]["memory_id"], created["memory_id"])

    def test_public_report_and_correction_are_not_user_acceptance(self):
        self.event("Stop", last_assistant_message="mapping changed but gameplay not verified")
        self.event("UserPromptSubmit", prompt="不对，动画仍然播放错误")
        self.assertTrue(self.event("UserPromptSubmit", prompt="请继续修复")[1]["indexed"])
        cards = self.r.work_search(self.p)["items"]
        self.assertEqual(len(cards), 3)
        self.assertTrue(all(card["validation_level"] == "reported" and card["state"] == "reported" for card in cards))

    def test_history_import_is_not_automatically_exposed(self):
        _, result = self.event("HistoryImport", source={"path": "notes.md"}, content="private paused history")
        self.assertIsNone(result)
        self.assertEqual(self.r.work_search(self.p)["items"], [])

    def test_project_and_checkout_filter_do_not_leak_other_workspaces(self):
        self.write()
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.assertEqual(self.r.work_search(self.p, file_path="src/a.py", workspace=other)["items"], [])
        project = self.r.project(other)
        self.assertEqual(self.r.work_search(project)["items"], [])

    def test_complete_json_budget_and_evidence_pagination(self):
        event, created = self.event("Stop", last_assistant_message="public explanation " * 1200)
        output = self.r.work_search(self.p, max_chars=512)
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))), 512)
        offset, text = 0, ""
        while True:
            page = self.r.work_read(self.p, created["memory_id"], max_chars=1024, offset=offset, section="evidence")
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":"))), 1024)
            text += page["content"]
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        evidence = json.loads(text)
        self.assertTrue(evidence["projection_only"])
        self.assertEqual(evidence["events"][0]["id"], event["id"])
        self.assertTrue(json.loads(evidence["events"][0]["payload"])["truncated"])

    def test_exposure_deduplication_and_source_change_notifications(self):
        event, created = self.write(operation="op")
        with self.r.transaction():
            self.r.db.execute("INSERT INTO operations(project,id,conversation,state,last_event,observation) VALUES(?,?,?,?,?,?)",
                              (self.p, "op", "chat", "succeeded", event["id"], "succeeded"))
        task = {"id": "task", "revision": 1, "write_version": 1}
        items = self.r.work_context_items(self.p, "chat", "animation", task_state=task, context_epoch=1)
        with self.r.transaction():
            self.r.work_record_exposure(self.p, "chat", items, task_state=task, context_epoch=1)
        self.assertEqual(self.r.work_context_items(self.p, "chat", "animation", task_state=task, context_epoch=1), [])
        self.assertTrue(self.r.work_context_items(self.p, "chat", "animation", task_state=task, context_epoch=2, compact=True))
        self.assertTrue(self.r.work_context_items(self.p, "chat", "animation", task_state=task, context_epoch=1, delivery="ephemeral"))
        with self.r.transaction():
            self.r.db.execute("UPDATE operations SET state='conflict',conflict=1 WHERE project=? AND id='op'", (self.p,))
        changed = self.r.work_exposure_updates(self.p, "chat")
        self.assertEqual(changed[0]["memory_id"], created["memory_id"])
        self.assertEqual(changed[0]["state"], "conflicted")
        self.assertTrue(changed[0]["source_changed"])
        self.assertTrue(self.r.work_exposure_updates(self.p, "chat"))
        with self.r.transaction():
            self.r.work_record_exposure(self.p, "chat", changed, task_state=task, context_epoch=1)
        self.assertEqual(self.r.work_exposure_updates(self.p, "chat"), [])

    def test_tool_only_has_no_recent_project_fallback(self):
        self.write()
        self.assertEqual(self.r.work_context_items(self.p, "chat", workspace=self.workspace,
                         raw={"tool_name": "unknown", "tool_input": {}}, file_only=True), [])
        matched = self.r.work_context_items(self.p, "new", workspace=self.workspace,
                         raw={"tool_name": "read_file", "tool_input": {"path": "src/a.py"}}, file_only=True)
        self.assertEqual(len(matched), 1)

    def test_file_version_changes_are_context_without_revoking_work(self):
        target = self.workspace / "src/a.py"
        target.parent.mkdir()
        target.write_text("old", encoding="utf-8")
        self.write()
        items = self.r.work_context_items(self.p, "chat", workspace=self.workspace,
                    raw={"tool_name": "read_file", "tool_input": {"path": "src/a.py"}}, file_only=True)
        self.assertFalse(items[0]["files"][0]["version_changed"])
        target.write_text("new and longer", encoding="utf-8")
        checked = self.r.work_validate_items(self.p, items, workspace=self.workspace)
        self.assertTrue(checked[0]["files"][0]["version_changed"])
        self.assertEqual(checked[0]["state"], "succeeded")
        self.assertFalse(checked[0]["permission_restored"])

    def test_external_file_change_survives_budget_omission_until_recorded(self):
        target = self.workspace / "src/a.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self.write()
        arguments = {"workspace": self.workspace, "raw": {"tool_name": "read_file", "tool_input": {"path": "src/a.py"}}, "file_only": True}
        first = self.r.work_context_items(self.p, "reader", **arguments)
        self.r.work_record_exposure(self.p, "reader", first)
        self.assertEqual(self.r.work_context_items(self.p, "reader", **arguments), [])
        target.write_text("changed outside recorded tools", encoding="utf-8")
        second = self.r.work_context_items(self.p, "reader", **arguments)
        self.assertTrue(second[0]["file_state_changed"])
        self.assertTrue(second[0]["files"][0]["version_changed"])
        self.assertTrue(self.r.work_context_items(self.p, "reader", **arguments))
        # 无当前文件观察的 MCP 正文读取不能消掉尚未发送的文件提醒。
        read = self.r.work_read(self.p, first[0]["memory_id"])
        self.r.work_record_exposure(self.p, "reader", [read], method="mcp")
        self.assertTrue(self.r.work_context_items(self.p, "reader", **arguments))
        self.r.work_record_exposure(self.p, "reader", second)
        self.assertEqual(self.r.work_context_items(self.p, "reader", **arguments), [])

    def test_mcp_file_preview_and_hook_share_file_observation_baseline(self):
        target = self.workspace / "src/a.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        self.write()
        preview = self.r.work_search(self.p, file_path="src/a.py", workspace=self.workspace)["items"]
        self.r.work_record_exposure(self.p, "reader", preview, method="mcp")
        self.assertEqual(self.r.work_context_items(self.p, "reader", workspace=self.workspace,
                         raw={"tool_name": "read_file", "tool_input": {"path": "src/a.py"}}, file_only=True), [])

    def test_runtime_file_pretool_hook_delivers_external_change_after_budget_returns(self):
        target = self.workspace / "src/a.py"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        _, memory = self.write()
        self.r.task_update(self.p, "reader", "independenttopicwithoutmatch", {"mode": "analysis"})
        self.r.handle_hook({"hook_event_name": "SessionStart", "session_id": "reader", "cwd": str(self.workspace)})
        def read_hook(number):
            result = self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": "reader",
                "cwd": str(self.workspace), "tool_use_id": f"read-{number}", "tool_name": "Read",
                "tool_input": {"file_path": str(target)}})
            return result.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn(memory["memory_id"], read_hook(1))
        target.write_text("outside tools and longer", encoding="utf-8")
        self.r.config["injection_max_chars"] = 128
        small = read_hook(2)
        self.assertLessEqual(len(small), 128)
        self.assertNotIn(memory["memory_id"], small)
        self.r.config["injection_max_chars"] = 2400
        notified = read_hook(3)
        self.assertIn(memory["memory_id"], notified)
        self.assertIn('"file_version_changed":true', notified)
        self.assertNotIn(memory["memory_id"], read_hook(4))

    def test_no_query_does_not_restore_other_task_work(self):
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "Stop", {"last_assistant_message": "old task decision"},
                                    task_id="old-task", task_revision=1)
            self.r._work_capture_event(self.p, event["id"], self.workspace)
        task = {"id": "new-task", "revision": 1, "write_version": 1}
        self.assertEqual(self.r.work_context_items(self.p, "chat", task_state=task), [])

    def test_read_rejects_boolean_revision(self):
        _, created = self.write()
        with self.assertRaises(ContractError):
            self.r.work_read(self.p, created["memory_id"], expected_revision=True)

    def test_missing_source_is_explicit_and_never_reliable(self):
        event, created = self.write()
        with self.r.transaction():
            self.r.db.execute("DELETE FROM events WHERE project=? AND id=?", (self.p, event["id"]))
        self.assertEqual(self.r.work_read(self.p, created["memory_id"])["state"], "missing")

    def test_summary_source_fence_and_no_formal_publication(self):
        event, created = self.write()
        source = self.r.work_source_snapshot(self.p, event["id"])
        result = self.r.record_work_summary(self.p, "summary1", {"title": "阶段工作说明", "summary": "工具成功，业务未验证"},
                                          [source], "chat")
        self.assertEqual(result["validation_level"], "model_reported")
        self.assertEqual(result["state"], "model_reported")
        self.assertEqual(result["type"], "work_summary")
        self.assertEqual(result["files"][0]["relative_path"], "src/a.py")
        self.assertEqual(self.r.record_work_summary(self.p, "summary1", {"title": "阶段工作说明", "summary": "工具成功，业务未验证"},
                         [source], "chat")["memory_id"], result["memory_id"])
        with self.r.transaction():
            self.r.db.execute("UPDATE events SET payload=? WHERE id=?", ('{"changed":true}', event["id"]))
        with self.assertRaises(ContractError):
            self.r.record_work_summary(self.p, "summary2", {"title": "阶段工作说明", "summary": "工具成功，业务未验证"}, [source], "chat")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_rebuild_is_bounded_and_does_not_scan_old_files(self):
        for number in range(3):
            with self.r.transaction():
                self.r._event(self.p, "chat", "Stop", {"cwd": str(self.workspace), "last_assistant_message": f"public report {number}"})
        first = self.r.work_rebuild(self.p, self.workspace, limit=2)
        self.assertEqual(first["scanned"], 2)
        self.assertEqual(first["indexed"], 2)
        second = self.r.work_rebuild(self.p, self.workspace, limit=2, cursor=first["next_cursor"])
        self.assertEqual(second["indexed"], 1)
        self.assertIsNone(second["next_cursor"])
        self.assertFalse(second["files_scanned"])

    def test_rebuild_repairs_derived_index_from_recorded_checkout(self):
        self.write()
        with self.r.transaction():
            self.r.db.execute("DELETE FROM work_memory_fts WHERE project=?", (self.p,))
        self.assertEqual(self.r.work_search(self.p, "animation")["items"], [])
        result = self.r.work_rebuild(self.p, self.workspace)
        self.assertEqual(result["reindexed"], 1)
        self.assertTrue(self.r.work_search(self.p, "animation")["items"])

    def test_rebuild_recovers_admin_summary_index_without_changing_sources(self):
        event, _ = self.write()
        summary = self.r.record_work_summary(self.p, "summary-repair", {
            "title": "阶段摘要", "summary": "uniquesummaryrepairfence 工具成功，业务未验收"},
            [self.r.work_source_snapshot(self.p, event["id"])], "chat")
        before = tuple(self.r.db.execute("SELECT body,source_hash,revision FROM work_memories WHERE project=? AND id=?",
                       (self.p, summary["memory_id"])).fetchone())
        with self.r.transaction():
            self.r.db.execute("DELETE FROM work_memory_fts WHERE project=?", (self.p,))
        self.assertEqual(self.r.work_search(self.p, "uniquesummaryrepairfence")["items"], [])
        self.assertEqual(self.r.work_read(self.p, summary["memory_id"])["validation_level"], "model_reported")
        result = self.r.work_rebuild(self.p, self.workspace)
        self.assertEqual(result["summaries_reindexed"], 1)
        self.assertEqual(self.r.work_search(self.p, "uniquesummaryrepairfence")["items"][0]["memory_id"], summary["memory_id"])
        after = tuple(self.r.db.execute("SELECT body,source_hash,revision FROM work_memories WHERE project=? AND id=?",
                      (self.p, summary["memory_id"])).fetchone())
        self.assertEqual(before, after)

    def test_rebuild_summary_phase_pages_are_bounded_and_checkout_isolated(self):
        event, _ = self.write()
        snapshot = self.r.work_source_snapshot(self.p, event["id"])
        ids = set()
        for number in range(3):
            summary = self.r.record_work_summary(self.p, f"paged-summary-{number}", {
                "title": f"摘要{number}", "summary": "uniquepagedsummaryfence"}, [snapshot], "chat")
            ids.add(summary["memory_id"])
        other = Path(self.temp.name) / "other-checkout"
        other.mkdir()
        other_project = self.r.project(other)
        with self.r.transaction():
            foreign_event, _ = self.r._event(other_project, "other", "Stop", {"last_assistant_message": "other work"})
            self.r._work_capture_event(other_project, foreign_event["id"], other)
        foreign = self.r.record_work_summary(other_project, "other-summary", {"title": "别处摘要", "summary": "foreignsummaryfence"},
                                            [self.r.work_source_snapshot(other_project, foreign_event["id"])], "other")
        # 同一 project 的另一 checkout 也不能进入本次摘要重建。
        with self.r.transaction():
            self.r.db.execute("UPDATE work_memories SET project=? WHERE id=?", (self.p, foreign["memory_id"]))
            self.r.db.execute("DELETE FROM work_memory_fts")
        cursor, summaries = None, 0
        observed_cursors = []
        for _ in range(10):
            page = self.r.work_rebuild(self.p, self.workspace, limit=1, cursor=cursor)
            self.assertLessEqual(page["scanned"], 1)
            self.assertEqual(page["scanned"], page["events_scanned"] + page["summaries_reindexed"])
            summaries += page["summaries_reindexed"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertNotIn(cursor, observed_cursors)
            observed_cursors.append(cursor)
        else:
            self.fail("rebuild did not finish")
        self.assertEqual(summaries, 3)
        self.assertTrue(any(isinstance(json.loads(value), dict) for value in observed_cursors))
        self.assertEqual({item["memory_id"] for item in self.r.work_search(self.p, "uniquepagedsummaryfence")["items"]}, ids)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memory_fts WHERE id=?", (foreign["memory_id"],)).fetchone()[0], 0)

    def test_git_subdirectory_capture_is_found_from_root_and_subdirectory(self):
        (self.workspace / ".git").mkdir()
        source = self.workspace / "src"
        source.mkdir()
        target = source / "a.py"
        target.write_text("old", encoding="utf-8")
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "PostToolUse", {"tool_name": "write_file",
                "tool_input": {"path": "a.py"}, "tool_response": {"exit_code": 0}}, outcome="succeeded")
            result = self.r._work_capture_event(self.p, event["id"], source)
        for cwd, path in ((self.workspace, "src/a.py"), (source, "a.py")):
            found = self.r.work_search(self.p, file_path=path, workspace=cwd)["items"]
            self.assertEqual(found[0]["memory_id"], result["memory_id"])
            self.assertEqual(found[0]["files"][0]["relative_path"], "src/a.py")
            self.assertFalse(found[0]["files"][0]["version_changed"])
        context = self.r.work_context_items(self.p, "reader", workspace=source,
            raw={"tool_name": "read_file", "tool_input": {"path": "a.py"}}, file_only=True)
        self.assertEqual(context[0]["memory_id"], result["memory_id"])
        self.assertFalse(context[0]["files"][0]["version_changed"])

    def test_configuration_validation_rejects_invalid_budgets_and_unknown_settings(self):
        for config in ({"enabled": 1}, {"max_items": True}, {"max_context_chars": 20},
                       {"summary_min_events": 10, "summary_max_events": 2}, {"scan_all_chats": True}):
            with self.assertRaises(ContractError):
                validate_work_memory_config(config)
        self.assertFalse(validate_work_memory_config({})["summary_enabled"])


if __name__ == "__main__":
    unittest.main()
