"""第五模块采集回归：真实临时 SQLite，不执行工具、宿主或模型。"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.event_capture import operation_input_conflict, prepare_event, tool_outcome
from agent_learning.runtime import Runtime, clean, digest


class CaptureProjectionTests(unittest.TestCase):
    def test_explicit_failure_overrides_session_and_success(self):
        for response in (
            {"session_id": 451, "isError": True},
            {"session_id": 451, "is_error": True},
            {"exit_code": 0, "success": False},
            {"exit_code": 0, "isError": True},
            {"exit_code": 0, "exitCode": 1},
        ):
            with self.subTest(response=response):
                self.assertEqual(tool_outcome(response), "failed")

    def test_terminal_signals_precede_session(self):
        self.assertEqual(tool_outcome({"session_id": 451, "success": True}), "succeeded")
        self.assertEqual(tool_outcome({"session_id": 451, "isError": False}), "call_completed")
        self.assertEqual(tool_outcome({"session_id": 451, "exit_code": None}), "running")
        self.assertEqual(tool_outcome({"output": "finished"}), "unknown")
        self.assertEqual(tool_outcome({"exit_code": True}), "unknown")

    def test_projection_preserves_critical_fields_at_minimum_budget(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "x" * 1000},
                   "tool_response": {"exit_code": 0, "is_error": True, "error_code": "E_IMPORT",
                                     "output": "y" * 10000}}
        text, signature = prepare_event(payload, 128)
        result = json.loads(text)
        self.assertLessEqual(len(text), 128)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["tool_response"]["exit_code"], 0)
        self.assertTrue(result["tool_response"]["is_error"])
        self.assertEqual(result["tool_response"]["error_code"], "E_IMPORT")
        self.assertEqual(signature, digest(clean(payload)))

    def test_error_fields_outrank_metrics(self):
        response = {f"metric_{i}": "x" * 200 for i in range(32)}
        response.update(is_error=True, error_code="E_BUILD", error="essential root cause")
        text, _ = prepare_event({"tool_name": "Bash", "tool_response": response}, 6000)
        result = json.loads(text)
        self.assertLessEqual(len(text), 6000)
        self.assertTrue(result["tool_response"]["is_error"])
        self.assertEqual(result["tool_response"]["error_code"], "E_BUILD")
        self.assertIn("essential root cause", result["tool_response"]["error"])

    def test_long_logs_keep_head_and_tail_and_redact_credentials(self):
        payload = {"tool_name": "Bash", "tool_response": {
            "exit_code": 1, "output": "header " + "x" * 20000 + " FATAL missing include",
            "api_key": "SYNTHETIC_TEST_SECRET"}}
        text, _ = prepare_event(payload, 6000)
        result = json.loads(text)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["tool_response"]["output"].startswith("header"))
        self.assertTrue(result["tool_response"]["output"].endswith("FATAL missing include"))
        self.assertNotIn("SYNTHETIC_TEST_SECRET", text)

    def test_full_signature_distinguishes_clipped_middle(self):
        a = {"tool_response": {"output": "x" * 10000 + "a" + "y" * 10000}}
        b = {"tool_response": {"output": "x" * 10000 + "b" + "y" * 10000}}
        projected_a, signature_a = prepare_event(a, 128)
        projected_b, signature_b = prepare_event(b, 128)
        self.assertEqual(projected_a, projected_b)
        self.assertNotEqual(signature_a, signature_b)


class CaptureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "诊断构建错误", {"mode": "analysis"})

    def hook(self, kind, **extra):
        return self.r.handle_hook({"hook_event_name": kind, "session_id": "chat",
                                   "cwd": str(self.workspace), **extra})

    def tool(self, ident, response, *, agent=None, inputs=None):
        shared = {"tool_use_id": ident, "tool_name": "Bash"}
        if agent:
            shared["agent_id"] = agent
        if inputs is not None:
            shared["tool_input"] = inputs
        self.hook("PreToolUse", **shared)
        self.hook("PostToolUse", **shared, tool_response=response)
        return self.r.db.execute("SELECT * FROM operations ORDER BY rowid DESC LIMIT 1").fetchone()

    def evidence(self, problem_id):
        return [dict(row) for row in self.r.db.execute(
            "SELECT e.type,e.agent,e.outcome,pe.role,e.payload FROM problem_events pe "
            "JOIN events e ON e.id=pe.event WHERE pe.problem=? ORDER BY e.created,e.id", (problem_id,))]

    def test_session_error_forms_failure_and_keeps_attempt(self):
        operation = self.tool("failure", {"session_id": 451, "is_error": True, "error_code": "E_IMPORT"},
                              inputs={"command": "compile", "path": "src/a.py"})
        self.assertEqual(operation["state"], "failed")
        case = self.r.problems(self.p)[0]
        evidence = self.evidence(case["id"])
        self.assertEqual([(item["type"], item["role"]) for item in evidence],
                         [("PreToolUse", "attempt"), ("PostToolUse", "failure")])
        self.assertEqual(json.loads(evidence[0]["payload"])["tool_input"]["path"], "src/a.py")

    def test_pre_post_input_mismatch_is_conflict_even_when_clipped(self):
        self.r.config["event_max_chars"] = 128
        self.hook("PreToolUse", tool_use_id="mismatch", tool_name="Bash",
                  tool_input={"command": "x" * 1000, "path": "src/a.py"})
        operation = self.r.db.execute("SELECT id FROM operations").fetchone()[0]
        self.assertTrue(operation_input_conflict(self.r, self.p, operation,
                                                 {"tool_input": {"command": "x" * 1000, "path": "src/b.py"}}))
        self.hook("PostToolUse", tool_use_id="mismatch", tool_name="Bash",
                  tool_input={"command": "x" * 1000, "path": "src/b.py"}, tool_response={"exit_code": 0})
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "conflict")

    def test_reviewed_failure_receives_only_potential_recovery(self):
        self.tool("failure", {"exit_code": 1}, inputs={"command": "compile", "path": "src/a.py"})
        case = self.r.problems(self.p)[0]
        self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (case["id"],))
        self.tool("recover", {"exit_code": 0}, inputs={"command": "verify", "path": "src/a.py"})
        updated = self.r.problems(self.p)[0]
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["resolution_status"], "recovery_observed")
        self.assertEqual(updated["revision"], case["revision"] + 1)
        self.assertEqual(len([item for item in self.evidence(case["id"]) if item["role"] == "potential_recovery"]), 1)
        self.tool("later", {"exit_code": 0}, inputs={"command": "unrelated", "path": "src/a.py"})
        self.assertEqual(self.r.problems(self.p)[0]["revision"], updated["revision"])

    def test_different_resource_does_not_attach_recovery(self):
        self.tool("failure", {"exit_code": 1}, inputs={"path": "src/a.py"})
        case = self.r.problems(self.p)[0]
        self.tool("unrelated", {"exit_code": 0}, inputs={"path": "src/b.py"})
        self.assertEqual(self.r.problems(self.p)[0]["revision"], case["revision"])

    def test_child_success_does_not_recover_parent_failure(self):
        self.tool("parent", {"exit_code": 1}, inputs={"path": "src/a.py"})
        case = self.r.problems(self.p)[0]
        self.hook("SubagentStart", agent_id="child")
        self.tool("child", {"exit_code": 0}, agent="child", inputs={"path": "src/a.py"})
        self.assertEqual(self.r.problems(self.p)[0]["revision"], case["revision"])
        self.tool("parent-recovery", {"exit_code": 0}, inputs={"path": "src/a.py"})
        self.assertGreater(self.r.problems(self.p)[0]["revision"], case["revision"])

    def test_orphan_success_does_not_recover_verified_failure(self):
        self.tool("failure", {"exit_code": 1})
        case = self.r.problems(self.p)[0]
        self.hook("PostToolUse", tool_use_id="orphan", tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.problems(self.p)[0]["revision"], case["revision"])

    def test_shared_case_keeps_each_childs_own_potential_recovery(self):
        for agent in ("child-a", "child-b"):
            self.hook("SubagentStart", agent_id=agent)
            self.tool(agent + "-fail", {"exit_code": 1}, agent=agent, inputs={"path": "src/a.py"})
        self.assertEqual(len(self.r.problems(self.p)), 1)
        case = self.r.problems(self.p)[0]
        for agent in ("child-a", "child-b"):
            self.tool(agent + "-recover", {"exit_code": 0}, agent=agent, inputs={"path": "src/a.py"})
        agents = {item["agent"] for item in self.evidence(case["id"]) if item["role"] == "potential_recovery"}
        self.assertEqual(agents, {"child-a", "child-b"})

    def test_delivery_resume_reuses_case_and_preserves_rounds(self):
        self.tool("compile", {"exit_code": 0}, inputs={"command": "compile"})
        self.r.task_update(self.p, "chat", "诊断构建错误", status="delivered")
        self.hook("Stop", turn_id="first", last_assistant_message="第一版结论")
        case = self.r.problems(self.p)[0]
        self.hook("Stop", turn_id="first-repeat", last_assistant_message="第一版结论")
        self.assertEqual(self.r.problems(self.p)[0]["revision"], case["revision"])
        self.r.task_update(self.p, "chat", "诊断构建错误", resume=True)
        self.tool("recheck", {"exit_code": 0}, inputs={"command": "recheck"})
        self.r.task_update(self.p, "chat", "诊断构建错误", status="delivered")
        self.hook("Stop", turn_id="corrected", last_assistant_message="修正后的结论")
        cases = self.r.problems(self.p)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["id"], case["id"])
        self.assertGreater(cases[0]["revision"], case["revision"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM delivery_rounds").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
        evidence = self.evidence(case["id"])
        self.assertEqual(len([item for item in evidence if item["role"] == "delivery"]), 2)
        self.assertEqual(len([item for item in evidence if item["type"] == "PreToolUse"]), 2)


if __name__ == "__main__":
    unittest.main()
