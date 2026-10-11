"""用真实临时 SQLite 验证受信审批、范围交集和许可失效，不执行工具。"""
import copy
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_learning.execution import validate_rules, validate_scope
from agent_learning.runtime import ContractError, Runtime


class ScopeValidationTests(unittest.TestCase):
    def test_known_scope_fields_and_legacy_metadata(self):
        original = {"mode": "analysis", "allowed_paths": ["Docs"], "allowed_tools": [],
                    "allowed_actions": ["read"], "budget": {"remaining": None, "unit": "tokens"},
                    "stop_conditions": ["需求发生变化"], "verification": ["离线检查"],
                    "environment": {"project": "existing"}}
        result = validate_scope(original)
        self.assertEqual(result, original)
        result["environment"]["project"] = "changed"
        self.assertEqual(original["environment"]["project"], "existing")

    def test_malformed_known_fields_reject_with_contract_error(self):
        for scope in ({"mode": []}, {"allowed_paths": "Docs"}, {"allowed_actions": ["destroy"]},
                      {"allowed_tools": [""]}, {"verification": [None]}, {"budget": {}},
                      {"budget": {"remaining": True}}, {"budget": {"remaining": -1}},
                      {"budget": {"remaining": math.inf}}):
            with self.subTest(scope=scope), self.assertRaises(ContractError):
                validate_scope(scope)

    def test_trusted_rules_reject_unknown_fields_and_unmapped_tool(self):
        base = {"mode": "execute", "allowed_tools": ["Read"], "tool_rules": {"Read": {"action": "read"}}}
        for changes in ({"authorized": True}, {"allowed_tools": []}, {"allowed_tools": ["Bash"]},
                        {"mode": []}, {"tool_rules": {"Read": {"action": "read", "extra": 1}}},
                        {"budget": {"remaining": 1, "untrusted_balance": 2}}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_rules(dict(base, **changes))

    def test_relative_roots_require_frozen_absolute_workspace(self):
        rules = {"mode": "read_only", "allowed_tools": ["Read"], "allowed_paths": ["Docs"],
                 "tool_rules": {"Read": {"action": "read", "path_fields": ["path"]}}}
        with self.assertRaisesRegex(ContractError, "absolute execution workspace"):
            validate_rules(rules)
        with tempfile.TemporaryDirectory() as temp:
            result = validate_rules(rules, temp)
            self.assertEqual(result["workspace"], str(Path(temp).resolve()))
            self.assertNotIn("workspace", rules)
            with self.assertRaisesRegex(ContractError, "disagrees"):
                validate_rules(dict(rules, workspace=temp), str(Path(temp) / "different"))


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.docs = self.workspace / "Docs"
        self.docs.mkdir(parents=True)
        self.runtime = Runtime(self.root / "state")
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "chat", "分析执行控制", {"mode": "execute"})
        self.rules = {"mode": "execute", "allowed_tools": ["Read", "Write", "Bash"],
                      "tool_rules": {"Read": {"action": "read", "path_fields": ["path"]},
                                     "Write": {"action": "write", "path_fields": ["path"]},
                                     "Bash": {"action": "execute"}}, "allowed_paths": ["Docs"]}

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def task(self):
        return self.runtime.task(self.project, "chat")

    def approve(self, rules=None, **fields):
        task = self.task()
        return self.runtime.approve_execution(self.project, "chat", task["id"], task["write_version"],
                                              self.rules if rules is None else rules,
                                              workspace=str(self.workspace), **fields)

    def check(self, tool="Read", tool_input=None, cwd=None, assignment=None, task=None):
        raw = {"tool_name": tool, "tool_input": {"path": "Docs/example.md"} if tool_input is None else tool_input,
               "cwd": str(self.workspace if cwd is None else cwd)}
        with self.runtime.transaction():
            return self.runtime.check_execution(self.project, "chat", self.task() if task is None else task,
                                                raw, assignment)

    def update_scope(self, scope):
        task = self.task()
        return self.runtime.task_update(self.project, "chat", task["goal"], scope,
            expected_task_id=task["id"], expected_write_version=task["write_version"])

    def directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
            return
        except OSError:
            if os.name == "nt":
                # Junction 不要求管理员符号链接权限；目标和链接都在本测试临时目录。
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                        capture_output=True, timeout=5)
                if result.returncode == 0:
                    return
            self.skipTest("当前平台无法创建目录链接")

    def test_unapproved_task_is_denied_then_first_grant_preserves_assignment_epoch(self):
        self.assertEqual(self.check()["reason"], "execution_approval_required")
        task = self.task()
        assignment = {"task_id": task["id"], "task_revision": task["revision"], "execution_epoch": task["execution_epoch"]}
        receipt = self.approve()
        self.assertTrue(receipt["ready"])
        self.assertEqual(receipt["task"]["write_version"], task["write_version"])
        self.assertEqual(self.check(assignment=assignment)["decision"], "allowed")

    def test_approval_is_atomic_against_stale_task_and_account_epoch(self):
        task = self.task()
        self.runtime.task_update(self.project, "chat", "新任务", new_task=True)
        with self.assertRaisesRegex(ContractError, "stale execution approval"):
            self.runtime.approve_execution(self.project, "chat", task["id"], task["write_version"], self.rules,
                                           workspace=str(self.workspace))
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM execution_policies").fetchone()[0], 0)
        self.runtime.account_changed()
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.approve(expected_epoch=0)

    def test_rule_replacement_fences_old_assignment_and_preserves_history(self):
        self.approve()
        previous = self.task()
        assignment = {"task_id": previous["id"], "task_revision": previous["revision"], "execution_epoch": previous["execution_epoch"]}
        replacement = copy.deepcopy(self.rules)
        replacement["mode"] = "read_only"
        receipt = self.approve(replacement)
        current = receipt["task"]
        self.assertEqual(current["revision"], previous["revision"])
        self.assertEqual(current["write_version"], previous["write_version"] + 1)
        self.assertEqual(current["execution_epoch"], previous["execution_epoch"] + 1)
        self.assertEqual(self.check(assignment=assignment)["reason"], "assignment_stale")
        self.assertEqual(self.check(tool="Write")["reason"], "read_only_action_denied")
        self.assertGreaterEqual(self.runtime.db.execute("SELECT COUNT(*) FROM task_states WHERE id=?", (current["id"],)).fetchone()[0], 2)

    def test_repeating_identical_grant_does_not_invalidate_current_assignment(self):
        self.approve()
        previous = self.task()
        self.approve()
        self.assertEqual(self.task()["write_version"], previous["write_version"])
        self.assertEqual(self.task()["execution_epoch"], previous["execution_epoch"])

    def test_reported_scope_can_only_narrow_trusted_policy(self):
        self.update_scope({"mode": "implementation", "allowed_paths": ["Docs"]})
        readonly = copy.deepcopy(self.rules)
        readonly["mode"] = "read_only"
        self.approve(readonly)
        self.assertEqual(self.check(tool="Write")["reason"], "read_only_action_denied")
        self.update_scope({"mode": "execute", "allowed_paths": [str(self.root)]})
        self.assertEqual(self.check()["reason"], "execution_approval_stale")
        self.approve(readonly)
        self.assertEqual(self.check(tool_input={"path": str(self.root / "outside.txt")})["reason"], "resource_outside_approved_roots")

    def test_readonly_never_accepts_opaque_shell_even_when_mislabeled_read(self):
        self.update_scope({"mode": "analysis"})
        rules = {"mode": "execute", "allowed_tools": ["Bash"], "tool_rules": {"Bash": {"action": "read"}}}
        self.approve(rules)
        self.assertEqual(self.check(tool="Bash", tool_input={"command": "arbitrary command"})["reason"], "read_only_action_denied")

    def test_scope_and_global_tool_restrictions_intersect(self):
        self.update_scope({"mode": "execute", "allowed_tools": ["Read"], "allowed_actions": ["read"]})
        self.approve()
        self.assertEqual(self.check(tool="Write")["reason"], "tool_disallowed_by_scope")
        self.runtime.config["allowed_tools"] = ["Write"]
        self.assertEqual(self.check()["reason"], "tool_disallowed_by_config")

    def test_explicit_empty_scope_roots_and_tools_deny(self):
        self.update_scope({"allowed_paths": []})
        self.approve()
        self.assertEqual(self.check()["reason"], "resource_outside_approved_roots")
        self.update_scope({"allowed_tools": []})
        self.approve()
        self.assertEqual(self.check()["reason"], "tool_disallowed_by_scope")

    def test_zero_balances_stop_without_accounting_or_model_calls(self):
        self.update_scope({"budget": {"remaining": 0, "unit": "steps"}})
        self.approve()
        self.assertEqual(self.check()["reason"], "execution_budget_exhausted")
        self.update_scope({})
        rules = copy.deepcopy(self.rules)
        rules["budget"] = {"remaining": 0}
        self.assertFalse(self.approve(rules)["ready"])
        self.assertEqual(self.check()["reason"], "execution_budget_exhausted")

    def test_relative_roots_remain_bound_when_hook_cwd_changes(self):
        self.approve()
        other = self.root / "different"
        (other / "Docs").mkdir(parents=True)
        self.assertEqual(self.check(cwd=other)["reason"], "resource_outside_approved_roots")
        self.assertEqual(self.check(tool_input={"path": str(self.docs / "example.md")}, cwd=other)["decision"], "allowed")

    def test_missing_fields_traversal_and_unmapped_tools_fail_closed(self):
        self.approve()
        for value in ({}, {"path": None}, {"path": "../project/Docs/example.md"}):
            with self.subTest(value=value):
                self.assertEqual(self.check(tool_input=value)["reason"], "resource_field_invalid")
        self.assertEqual(self.check(tool="Bash", tool_input={"command": "opaque"})["reason"], "resource_fields_required")
        self.assertEqual(self.check(tool="Unknown")["reason"], "tool_not_approved")

    def test_nested_resource_fields_and_multiple_resources_all_checked(self):
        rules = {"mode": "execute", "allowed_tools": ["Read"], "allowed_paths": ["Docs"],
                 "tool_rules": {"Read": {"action": "read", "path_fields": ["request.paths"]}}}
        self.approve(rules)
        self.assertEqual(self.check(tool_input={"request": {"paths": ["Docs/a", "Docs/b"]}})["decision"], "allowed")
        self.assertEqual(self.check(tool_input={"request": {"paths": ["Docs/a", str(self.root / "private")]}})["reason"], "resource_outside_approved_roots")

    def test_child_assignment_scope_is_an_additional_intersection(self):
        self.approve()
        task = self.task()
        assignment = {"task_id": task["id"], "task_revision": task["revision"], "execution_epoch": task["execution_epoch"],
                      "scope": {"allowed_actions": ["read"]}}
        self.assertEqual(self.check(tool="Write", assignment=assignment)["reason"], "action_disallowed_by_scope")
        self.assertEqual(self.check(assignment=assignment)["decision"], "allowed")

    def test_symlink_escape_is_rejected_if_platform_supports_creation(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.directory_link(self.docs / "linked", outside)
        self.approve()
        self.assertEqual(self.check(tool_input={"path": "Docs/linked/file"})["reason"], "resource_outside_approved_roots")

    def test_approved_root_replaced_by_directory_link_cannot_expand_permit(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.approve()
        original = self.workspace / "original-docs"
        self.docs.rename(original)
        self.directory_link(self.docs, outside)
        self.assertEqual(self.check(tool_input={"path": "Docs/file"})["reason"], "resource_outside_approved_roots")

    def test_cancelled_tasks_and_old_account_permits_are_unusable(self):
        self.approve()
        self.runtime.account_changed()
        self.assertIn(self.check()["reason"], {"account_context_changed", "execution_approval_stale"})
        self.approve()
        task = self.task()
        self.runtime.task_update(self.project, "chat", task["goal"], status="cancelled",
            expected_task_id=task["id"], expected_write_version=task["write_version"])
        self.assertEqual(self.check()["reason"], "task_inactive")
        with self.assertRaisesRegex(ContractError, "active task"):
            self.approve()


if __name__ == "__main__":
    unittest.main()
