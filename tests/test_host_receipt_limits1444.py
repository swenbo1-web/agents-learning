"""原生 Hook 的模型可见输出不能伪造成独立工具终态；全部为离线合成回执。

Codex PostToolUse 的 tool_response 可以是字符串。当前公开契约没有保证另行
提供退出码，因此不解码字符串 JSON、不解析退出码文字，也不读取 transcript。
"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.event_capture import prepare_event, tool_outcome
from agent_learning.runtime import ContractError, Runtime


class ReceiptAuthorityTests(unittest.TestCase):
    def test_model_visible_text_is_not_terminal_evidence(self):
        for response in (
            '{"exit_code":0,"success":true}',
            '{"exit_code":1,"isError":true}',
            "Process exited with code 0",
            "Process exited with code 1",
            "Exit code: 0\nAll tests passed",
            "success: true",
        ):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "unknown")

    def test_nested_output_does_not_gain_receipt_authority(self):
        for response in (
            {"stdout": '{"exit_code":0,"success":true}'},
            {"stdout": "Process exited with code 0", "stderr": ""},
            {"output": {"exit_code": 0, "success": True}},
            {"result": {"exit_code": 0, "success": True}},
            {"structuredContent": {"exit_code": 0, "success": True}},
            {"content": [{"type": "text", "text": '{"isError":true}'}]},
        ):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "unknown")

    def test_status_labels_and_coerced_codes_are_not_terminal_evidence(self):
        for response in (
            {"status": "completed"},
            {"status": "failed"},
            {"exit_code": "0"},
            {"exitCode": False},
            {"success": "true"},
            {"isError": "false"},
            {"exit_code": None},
        ):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "unknown")

    def test_conflicting_structured_signals_keep_explicit_failure(self):
        for response in (
            {"exit_code": 0, "exitCode": 9},
            {"exit_code": 9, "exitCode": 0},
            {"exit_code": 0, "success": False},
            {"exit_code": 0, "isError": True},
            {"exitCode": 0, "is_error": True},
            {"success": True, "isError": True},
            {"session_id": 42, "isError": False, "exit_code": 9},
        ):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "failed")

    def test_structured_completion_and_tool_success_remain_distinct(self):
        for response in ({"isError": False}, {"is_error": False},
                         {"isError": False, "content": [{"type": "text", "text": "exit_code: 0"}]}):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "call_completed")
        for response in ({"exit_code": 0}, {"exitCode": 0}, {"success": True}):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "succeeded")

    def test_projection_preserves_string_response_type_and_identity(self):
        response = '{"exit_code":0,"success":true}'
        payload = {"tool_name": "Bash", "tool_response": response}
        text, signature = prepare_event(payload, 6000)
        projected = json.loads(text)
        self.assertIsInstance(projected["tool_response"], str)
        self.assertEqual(projected["tool_response"], response)
        self.assertEqual(tool_outcome(projected["tool_response"]), "unknown")
        _, object_signature = prepare_event({**payload, "tool_response": json.loads(response)}, 6000)
        self.assertNotEqual(signature, object_signature)


class ReceiptEvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.runtime = Runtime(Path(self.temp.name) / "home")
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "chat", "inspect synthetic receipts", {"mode": "analysis"})

    def hook(self, kind, **fields):
        return self.runtime.handle_hook({"hook_event_name": kind, "session_id": "chat",
                                         "cwd": str(self.workspace), **fields})

    def receipt(self, identity, response, **outer_fields):
        self.hook("PreToolUse", tool_use_id=identity, tool_name="Bash", tool_input={"command": "synthetic"})
        self.hook("PostToolUse", tool_use_id=identity, tool_name="Bash", tool_input={"command": "synthetic"},
                  tool_response=response, **outer_fields)
        return dict(self.runtime.db.execute(
            "SELECT * FROM events WHERE type='PostToolUse' ORDER BY rowid DESC LIMIT 1").fetchone())

    def candidate(self, event, validation):
        problem = self.runtime.feedback(self.project, "chat", "synthetic receipt reference",
                                        category="operation", event_refs=[event["id"]])
        return self.runtime.submit_candidate(self.project, problem_id=problem["id"],
            problem_revision=problem["revision"], title="Receipt reference", content="Observed output only",
            category="operation", applicability="synthetic fixture", limitations="No business acceptance",
            evidence_refs=[event["id"]], validation_level=validation)

    def test_text_cannot_create_failure_or_fake_success_but_can_be_reported(self):
        event = self.receipt("text", '{"exit_code":0,"success":true}')
        self.assertEqual(event["outcome"], "unknown")
        self.assertEqual(self.runtime.problems(self.project), [])
        with self.assertRaisesRegex(ContractError, "no observed completed tool success"):
            self.candidate(event, "tool_success")
        self.assertEqual(self.candidate(event, "reported")["status"], "candidate")

    def test_undocumented_outer_fields_cannot_fill_missing_tool_terminal(self):
        event = self.receipt("outer", "Process exited with code 0", exit_code=0, success=True,
                             status="completed", result={"exit_code": 0})
        self.assertEqual(event["outcome"], "unknown")
        self.assertEqual(json.loads(event["payload"])["tool_response"], "Process exited with code 0")
        with self.assertRaisesRegex(ContractError, "no observed completed tool success"):
            self.candidate(event, "tool_success")

    def test_mcp_call_completion_cannot_validate_tool_success(self):
        event = self.receipt("mcp", {"isError": False, "content": [{"type": "text", "text": "all passed"}]})
        self.assertEqual(event["outcome"], "call_completed")
        with self.assertRaisesRegex(ContractError, "no observed completed tool success"):
            self.candidate(event, "tool_success")

    def test_explicit_structured_success_still_supports_success_evidence(self):
        event = self.receipt("native", {"exit_code": 0, "stdout": "synthetic output"})
        self.assertEqual(event["outcome"], "succeeded")
        self.assertEqual(self.candidate(event, "tool_success")["status"], "candidate")

    def test_later_conflicting_structured_receipt_invalidates_success_evidence(self):
        event = self.receipt("conflict", {"exit_code": 0})
        self.hook("PostToolUse", tool_use_id="conflict", tool_name="Bash",
                  tool_input={"command": "synthetic"}, tool_response={"exit_code": 1})
        operation = self.runtime.db.execute("SELECT state,conflict FROM operations").fetchone()
        self.assertEqual(tuple(operation), ("conflict", 1))
        with self.assertRaisesRegex(ContractError, "no observed completed tool success"):
            self.candidate(event, "tool_success")


if __name__ == "__main__":
    unittest.main()
