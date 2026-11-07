"""Explicit file roots, immutable repair receipts and bounded filter metadata."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime, ContractError
from agent_learning.work_files import (checkout_key, file_links_from_payload, current_file_version,
                                       knowledge_file_observation)
from agent_learning.work_memory import _read_file_check
from agent_learning.work_file_diagnostics import observe_event, summary
from agent_learning.work_recovery import projection_links, register, restore
from agent_learning.workspace_links import file_workspace_roots
from agent_learning.work_notice_delivery import (FileNoticeGate, file_notice, file_state_key,
                                                 visible_exposures, visible_file_keys)


class LinkedWorkFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.primary = self.base / "primary"
        self.linked = self.base / "linked"
        self.outside = self.base / "outside"
        for root in (self.primary, self.linked, self.outside):
            root.mkdir()
            (root / "same.py").write_text("original", encoding="utf-8")
        self.home = self.base / "state"
        initial = Runtime(self.home, config={})
        self.project = initial.project(self.primary)
        self.other_project = initial.project(self.linked)
        initial.close()
        self.config = {"linked_workspaces": {self.project: [str(self.linked)]}}
        self.r = Runtime(self.home, config=self.config)
        self.addCleanup(lambda: self.r.close())

    def event(self, path=None, *, raw=None, kind="PostToolUse", operation=None, conversation="chat", safe=False):
        raw = raw or {"tool_name": "write_file", "tool_input": {"path": path},
                      "tool_response": {"exit_code": 0, "output": "Updated parser transaction boundary correctly"}}
        with self.r.transaction():
            event, _ = self.r._event(self.project, conversation, kind, raw,
                outcome="succeeded" if kind == "PostToolUse" else "unknown", operation_id=operation)
            result = (self.r._work_capture_safely(self.project, event["id"], self.primary, raw=raw) if safe
                      else self.r._work_capture_event(self.project, event["id"], self.primary, raw=raw))
        return dict(event), result

    def test_same_relative_name_has_distinct_checkout_and_precise_search(self):
        primary_event, primary = self.event("same.py")
        linked_event, linked = self.event(str(self.linked / "same.py"))
        self.assertNotEqual(primary_event["id"], linked_event["id"])
        links = self.r.db.execute("SELECT checkout_key,relative_path FROM work_file_links").fetchall()
        self.assertEqual({row["checkout_key"] for row in links}, {checkout_key(self.primary), checkout_key(self.linked)})
        for path, expected in (("same.py", primary), (str(self.linked / "same.py"), linked)):
            result = self.r.work_search(self.project, file_path=path, workspace=self.primary)
            self.assertEqual([item["memory_id"] for item in result["items"]], [expected["memory_id"]])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM project_paths WHERE project=?",
                                           (self.project,)).fetchone()[0], 1)
        self.assertEqual(self.r.work_search(self.other_project)["items"], [])

    def test_multiroot_patch_keeps_both_identical_relative_paths(self):
        roots = file_workspace_roots(self.r, self.project, self.primary)
        text = "*** Begin Patch\n*** Update File: same.py\n*** Update File: " + str(self.linked / "same.py") + "\n*** End Patch"
        links = file_links_from_payload("apply_patch", text, self.primary, roots=roots)
        self.assertEqual(len(links), 2)
        self.assertEqual({link["checkout_key"] for link in links}, {root["checkout_key"] for root in roots})

    def test_unconfigured_root_fails_closed_without_reading_another_project(self):
        self.r.config["linked_workspaces"] = {}
        _, result = self.event(str(self.linked / "same.py"))
        self.assertIsNone(result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links").fetchone()[0], 0)
        with self.assertRaises(ContractError):
            self.r.work_search(self.project, file_path=str(self.linked / "same.py"), workspace=self.primary)

    def test_read_check_and_knowledge_observation_use_linked_root(self):
        event, result = self.event(str(self.linked / "same.py"))
        check = _read_file_check(self.r, self.project, result["memory_id"], self.primary)
        self.assertTrue(check["complete"])
        evidence = {"evidence": [{"event_id": event["id"]}]}
        initial = knowledge_file_observation(self.r, self.project, evidence, self.primary)
        self.assertFalse(initial["changed"])
        (self.linked / "same.py").write_text("modified linked metadata", encoding="utf-8")
        self.assertNotEqual(check, _read_file_check(self.r, self.project, result["memory_id"], self.primary))
        self.assertTrue(knowledge_file_observation(self.r, self.project, evidence, self.primary)["changed"])
        row = self.r.db.execute("SELECT * FROM work_memories WHERE id=?", (result["memory_id"],)).fetchone()
        self.assertTrue(self.r._work_item(self.project, row, workspace=self.primary)["files"][0]["version_changed"])

    def test_revocation_never_stats_old_linked_file(self):
        event, result = self.event(str(self.linked / "same.py"))
        self.r.config["linked_workspaces"] = {}
        original_stat = Path.stat
        def guarded(path, *args, **kwargs):
            if path == self.linked or self.linked in path.parents:
                raise AssertionError("revoked root was inspected")
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, "stat", guarded):
            check = _read_file_check(self.r, self.project, result["memory_id"], self.primary)
            self.assertFalse(check["complete"])
            observation = knowledge_file_observation(self.r, self.project,
                {"evidence": [{"event_id": event["id"]}]}, self.primary)
            self.assertFalse(observation["changed"])
            self.assertTrue(observation["unverified"])
            roots = file_workspace_roots(self.r, self.project, self.primary)
            self.assertEqual(current_file_version("same.py", self.primary,
                file_checkout=checkout_key(self.outside), roots=roots)["status"], "outside_workspace")

    def test_post_without_args_inherits_linked_pre_with_same_identity(self):
        self.event(str(self.linked / "same.py"), kind="PreToolUse", operation="operation")
        event, result = self.event(raw={"tool_name": "write_file", "tool_response": {"exit_code": 0}}, operation="operation")
        self.assertIsNotNone(result)
        key = self.r.db.execute("SELECT checkout_key FROM work_file_links WHERE event_id=?", (event["id"],)).fetchone()[0]
        self.assertEqual(key, checkout_key(self.linked))

    def test_post_cannot_borrow_pre_across_conversations_or_rejected_args(self):
        self.event(str(self.linked / "same.py"), kind="PreToolUse", operation="operation")
        event, _ = self.event(raw={"tool_name": "write_file", "tool_response": {"exit_code": 0}},
                              operation="operation", conversation="different-chat")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links WHERE event_id=?", (event["id"],)).fetchone()[0], 0)

    def test_post_inheritance_requires_earlier_pre_of_the_same_tool(self):
        self.event(str(self.linked / "same.py"), operation="post-only")
        event, _ = self.event(raw={"tool_name": "write_file", "tool_response": {"exit_code": 0}}, operation="post-only")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links WHERE event_id=?", (event["id"],)).fetchone()[0], 0)
        early, _ = self.event(raw={"tool_name": "write_file", "tool_response": {"exit_code": 0}}, operation="late-pre")
        self.event(str(self.linked / "same.py"), kind="PreToolUse", operation="late-pre")
        with self.r.transaction():
            self.assertEqual(self.r._work_file_projection(self.project, early, self.primary), [])
        self.event(str(self.linked / "same.py"), kind="PreToolUse", operation="wrong-tool")
        event, _ = self.event(raw={"tool_name": "edit_file", "tool_response": {"exit_code": 0}}, operation="wrong-tool")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links WHERE event_id=?", (event["id"],)).fetchone()[0], 0)
        event, _ = self.event(str(self.outside / "same.py"), operation="operation")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links WHERE event_id=?", (event["id"],)).fetchone()[0], 0)

    def test_pretool_context_finds_linked_history_and_revoke_removes_it(self):
        _, result = self.event(str(self.linked / "same.py"))
        raw = {"tool_name": "read_file", "tool_input": {"path": str(self.linked / "same.py")}}
        items = self.r.work_context_items(self.project, "next-chat", raw=raw, workspace=self.primary, file_only=True)
        self.assertEqual([item["memory_id"] for item in items], [result["memory_id"]])
        self.r.config["linked_workspaces"] = {}
        self.assertEqual(self.r.work_context_items(self.project, "next-chat", raw=raw,
                         workspace=self.primary, file_only=True), [])

    def test_checkout_qualified_focus_preserves_exposed_linked_file_recheck(self):
        target = self.linked / "zzz.py"
        target.write_text("first", encoding="utf-8")
        paths = [str(self.primary / f"p{index}.py") for index in range(4)] + [str(target)]
        text = "*** Begin Patch\n" + "".join("*** Update File: " + path + "\n" for path in paths) + "*** End Patch"
        _, created = self.event(raw={"tool_name": "apply_patch", "tool_input": text,
                                   "tool_response": {"exit_code": 0}})
        row = self.r.db.execute("SELECT * FROM work_memories WHERE id=?", (created["memory_id"],)).fetchone()
        focus = checkout_key(self.linked) + ":zzz.py"
        item = self.r._work_item(self.project, row, workspace=self.primary, file_paths=[focus])
        self.assertEqual(item["files"][0]["checkout_key"], checkout_key(self.linked))
        self.assertEqual(item["files"][0]["relative_path"], "zzz.py")
        with self.r.transaction():
            self.r.work_record_exposure(self.project, "reader", [item])
        target.write_text("changed linked target", encoding="utf-8")
        updates = self.r.work_exposure_updates(self.project, "reader", workspace=self.primary)
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0]["file_state_changed"])
        self.assertIn(focus, updates[0]["_changed_file_paths"])

    def test_same_named_primary_and_linked_changes_have_separate_visible_receipts(self):
        self.r.config["work_memory"].update(max_context_chars=1800, max_items=3)
        self.r.task_update(self.project, "reader", "parser transaction boundary", {"mode": "analysis"})
        handle = self.r.register_context(self.project, "reader")["context_handle"]
        text = "*** Begin Patch\n*** Update File: same.py\n*** Update File: " + str(self.linked / "same.py") + "\n*** End Patch"
        _, created = self.event(raw={"tool_name": "apply_patch", "tool_input": text,
            "tool_response": {"exit_code": 0, "output": "parser transaction boundary implementation checked"}})
        row = self.r.db.execute("SELECT * FROM work_memories WHERE id=?", (created["memory_id"],)).fetchone()
        baseline = self.r._work_item(self.project, row, workspace=self.primary)
        with self.r.transaction():
            self.r.work_record_exposure(self.project, "reader", [baseline], method="mcp")
        for root in (self.primary, self.linked):
            (root / "same.py").write_text("changed " + root.name, encoding="utf-8")
        text = self.r._context(self.project, "reader", query="parser transaction boundary",
            context_handle=handle, workspace=self.primary, tool_only=True)
        cards = [json.loads(line.split(" ", 1)[1]) for line in text.splitlines() if line.startswith("[工作历史] ")]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(len(card["files"]), 2)
        self.assertEqual(visible_file_keys(card), {"same.py", checkout_key(self.linked) + ":same.py"})
        state = json.loads(self.r.db.execute("SELECT file_state FROM work_exposures WHERE conversation='reader'").fetchone()[0])
        current = self.r._work_item(self.project, row, workspace=self.primary)
        self.assertEqual(state, current["file_state"])
        (self.linked / "same.py").write_text("linked changed a second time", encoding="utf-8")
        with patch("agent_learning.work_notice_delivery.time.time", return_value=time.time() + 61):
            text = self.r._context(self.project, "reader", query="parser transaction boundary",
                context_handle=handle, workspace=self.primary, tool_only=True)
        cards = [json.loads(line.split(" ", 1)[1]) for line in text.splitlines() if line.startswith("[工作历史] ")]
        self.assertEqual(len(cards), 1)
        self.assertEqual(visible_file_keys(cards[0]), {checkout_key(self.linked) + ":same.py"})

    def test_notice_version_cache_and_partial_acknowledgement_keep_checkout_identity(self):
        primary_key, linked_key = checkout_key(self.primary), checkout_key(self.linked)
        linked_path = linked_key + ":same.py"
        item = {"memory_id": "index", "state": "reported", "validation_level": "reported",
                "file_state_changed": True, "file_state": {"same.py": "primary-new", linked_path: "linked-new"},
                "_changed_file_paths": ["same.py", linked_path],
                "files": [{"relative_path": "same.py", "checkout_key": primary_key, "version_changed": True,
                           "current_version": {"status": "stat_observed"}},
                          {"relative_path": "same.py", "checkout_key": linked_key, "version_changed": True,
                           "current_version": {"status": "stat_observed"}}]}
        gate = FileNoticeGate(self.r, self.project, "reader", None, "scope", "persistent")
        self.assertTrue(file_notice(item))
        self.assertEqual(set(gate._version_keys(item)), {"same.py", linked_path})
        limited_card = {"files": [{"path": "same.py", "checkout_key": linked_key}]}
        visible = visible_file_keys(limited_card)
        exposed = visible_exposures([dict(item, _visible_file_paths=visible)])[0]
        self.assertEqual(exposed["file_state"], {linked_path: "linked-new"})
        self.assertEqual(file_state_key(item["files"][0], item["file_state"]), "same.py")
        self.assertEqual(file_state_key(item["files"][1], item["file_state"]), linked_path)

    def test_recovery_uses_original_multiroot_versions_after_revocation(self):
        text = "*** Begin Patch\n*** Update File: same.py\n*** Update File: " + str(self.linked / "same.py") + "\n*** End Patch"
        raw = {"tool_name": "apply_patch", "tool_input": text, "tool_response": {"exit_code": 0}}
        with patch.object(self.r, "_work_index", side_effect=sqlite3.OperationalError("synthetic interruption")):
            event, _ = self.event(raw=raw, safe=True)
        receipt = json.loads(self.r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0])["file_projection"]
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(len(receipt["checkout_keys"]), 2)
        (self.linked / "same.py").write_text("a later version", encoding="utf-8")
        self.r.config["linked_workspaces"] = {}
        with self.r.transaction():
            result = restore(self.r, self.project, event["id"], self.primary)
        self.assertEqual(result["status"], "completed")
        links = self.r.db.execute("SELECT checkout_key,version FROM work_file_links WHERE event_id=?", (event["id"],)).fetchall()
        self.assertEqual(len(links), 2)
        self.assertTrue(all(json.loads(row["version"])["size"] == 8 for row in links))
        source = self.r.db.execute("SELECT e.conversation,e.task_id,e.execution_epoch FROM events e WHERE id=?", (event["id"],)).fetchone()
        self.assertEqual(tuple(source), (event["conversation"], event["task_id"], event["execution_epoch"]))

    def test_legacy_rebuild_cannot_borrow_new_linked_approval(self):
        raw = {"tool_name": "write_file", "cwd": str(self.primary),
               "tool_input": {"path": str(self.linked / "same.py")}, "tool_response": {"exit_code": 0}}
        with self.r.transaction():
            event, _ = self.r._event(self.project, "chat", "PostToolUse", raw, outcome="succeeded")
            restore(self.r, self.project, event["id"], self.primary)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links").fetchone()[0], 0)

    def test_original_outside_callback_cannot_gain_links_when_replayed_after_approval(self):
        self.r.config["linked_workspaces"] = {}
        raw = {"tool_name": "write_file", "tool_input": {"path": str(self.linked / "same.py")},
               "tool_response": {"exit_code": 0}}
        event, result = self.event(raw=raw)
        self.assertIsNone(result)
        self.r.config["linked_workspaces"] = self.config["linked_workspaces"]
        with self.r.transaction():
            self.r._work_capture_event(self.project, event["id"], self.primary, raw=raw)
            self.r._work_capture_safely(self.project, event["id"], self.primary, raw=raw)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)

    def test_recovery_v1_stays_primary_and_v2_manifest_is_checked(self):
        with self.r.transaction():
            event, _ = self.r._event(self.project, "chat", "PreToolUse", {"tool_name": "write_file", "tool_input": {"path": "same.py"}})
            receipt = register(self.r, self.project, event["id"], self.primary, pending=False)
        legacy = dict(receipt, version=1)
        legacy.pop("checkout_keys")
        self.assertEqual(len(projection_links(dict(event), checkout_key(self.primary), legacy)), 1)
        forged = json.loads(json.dumps(receipt))
        forged["links"][0]["checkout_key"] = checkout_key(self.outside)
        with self.assertRaises(ContractError):
            projection_links(dict(event), checkout_key(self.primary), forged)

    def test_21_outside_patch_headers_have_exact_bounded_reasons(self):
        text = "*** Begin Patch\n" + "".join("*** Update File: " + str(self.outside / f"p{i}.py") + "\n@@\n-private\n+secret\n" for i in range(21)) + "*** End Patch"
        raw = {"tool_name": "apply_patch", "tool_input": text, "tool_response": {"exit_code": 0}}
        event, _ = self.event(raw=raw)
        output = summary(self.r, self.project)
        self.assertEqual((output["target_count"], output["accepted_count"], output["filtered_count"]), (21, 0, 21))
        self.assertEqual(output["reasons"], {"outside_workspace": 21})
        with self.r.transaction():
            observe_event(self.r, self.project, event, self.primary, raw=raw)
        self.assertEqual(summary(self.r, self.project), output)
        serialized = " ".join(str(tuple(row)) for row in self.r.db.execute("SELECT * FROM work_file_observations"))
        for secret in ("private", "secret", str(self.outside), "p20.py"):
            self.assertNotIn(secret, serialized)

    def test_unsupported_shell_and_missing_targets_are_distinct_from_outside(self):
        for raw in ({"tool_name": "exec_command", "tool_input": {"cmd": "python fix.py"}},
                    {"tool_name": "read_file", "tool_input": {}},
                    {"tool_name": "write_file", "tool_input": {"path": str(self.outside / "same.py")}},
                    {"tool_name": "write_file", "tool_input": {"path": str(self.linked / ".env")}}):
            self.event(raw=raw)
        reasons = summary(self.r, self.project)["reasons"]
        self.assertEqual(reasons, {"unsupported_arguments": 1, "no_structured_target": 1,
                                  "outside_workspace": 1, "sensitive_target": 1})

    def test_disabled_work_memory_still_collects_filter_metadata(self):
        self.r.config["work_memory"]["enabled"] = False
        self.event(str(self.outside / "same.py"), safe=True)
        self.assertEqual(summary(self.r, self.project)["reasons"], {"outside_workspace": 1})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links").fetchone()[0], 0)

    def test_optional_diagnostic_failure_cannot_interrupt_capture_or_register(self):
        with patch("agent_learning.work_file_diagnostics.observe_event", side_effect=sqlite3.OperationalError("derived table unavailable")):
            event, result = self.event(str(self.linked / "same.py"))
            self.assertTrue(result["indexed"])
            event, _ = self.event("same.py", safe=True)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)

    def test_junction_and_symlink_targets_cannot_cross_approved_roots(self):
        roots = file_workspace_roots(self.r, self.project, self.primary)
        target = self.primary / "bridge" / "same.py"
        bridge = self.primary / "bridge"
        original_lstat = Path.lstat
        def junction_stat(path, *args, **kwargs):
            if path == bridge:
                class Junction:
                    st_file_attributes = 0x400
                    st_mode = 0
                return Junction()
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", junction_stat):
            details = {}
            self.assertEqual(file_links_from_payload("write_file", {"path": str(target)}, self.primary,
                roots=roots, diagnostics=details), [])
            self.assertEqual(details["reasons"], {"symlink_target": 1})
            details = {}
            self.assertEqual(file_links_from_payload("exec_command", {"cmd": "cat same.py", "cwd": str(bridge)},
                self.primary, roots=roots, diagnostics=details), [])
            self.assertEqual(details["reasons"], {"symlink_target": 1})

    def test_post_cannot_inherit_pre_across_task_or_host_epochs(self):
        for field, before, after in (("execution_epoch", 1, 2), ("task_write_version", 3, 4),
                                     ("assignment_id", "assignment-a", "assignment-b"), ("host_epoch", 1, 2)):
            with self.subTest(field=field):
                with self.r.transaction():
                    pre_raw = {"tool_name": "write_file", "tool_input": {"path": str(self.linked / "same.py")}}
                    post_raw = {"tool_name": "write_file", "tool_response": {"exit_code": 0}}
                    pre_binding, post_binding = {}, {}
                    if field == "host_epoch":
                        pre_raw[field], post_raw[field] = before, after
                    else:
                        pre_binding[field], post_binding[field] = before, after
                    pre, _ = self.r._event(self.project, "chat", "PreToolUse", pre_raw,
                        operation_id=field, **pre_binding)
                    self.r._work_capture_event(self.project, pre["id"], self.primary, raw=pre_raw)
                    post, _ = self.r._event(self.project, "chat", "PostToolUse", post_raw,
                        operation_id=field, outcome="succeeded", **post_binding)
                    self.r._work_capture_event(self.project, post["id"], self.primary, raw=post_raw)
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links WHERE event_id=?", (post["id"],)).fetchone()[0], 0)

    def test_diagnostics_scope_limit_and_old_database_missing_table(self):
        for index in range(4):
            self.event(str(self.outside / f"p{index}.py"), conversation=f"chat{index}")
        self.assertTrue(summary(self.r, self.project, limit=2)["truncated"])
        self.assertEqual(summary(self.r, self.project, conversation="chat1")["events"], 1)
        self.assertEqual(summary(self.r, self.other_project)["events"], 0)
        self.assertEqual(summary(self.r, self.project, host="dsh")["events"], 0)
        self.r.db.execute("DROP TABLE work_file_observations")
        self.assertEqual(summary(self.r, self.project)["events"], 0)
        self.assertFalse(summary(self.r, self.project)["available"])


if __name__ == "__main__":
    unittest.main()
