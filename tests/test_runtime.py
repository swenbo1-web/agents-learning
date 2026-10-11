import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_learning.admin import admin_once
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat-a", "修复工具错误", {"mode": "analysis"})

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def hook(self, kind, **extra):
        raw = {"hook_event_name": kind, "session_id": "chat-a", "cwd": str(self.workspace), **extra}
        return self.r.handle_hook(raw)

    def problem(self):
        self.hook("PreToolUse", tool_use_id="failed-call", tool_name="Bash", tool_input={"command": "example"})
        self.hook("PostToolUse", tool_use_id="failed-call", tool_name="Bash", tool_response={"exit_code": 1, "output": "module lookup failed"})
        return self.r.problems(self.p)[0]

    def candidate(self, problem=None, level="reported"):
        problem = problem or self.problem()
        packet = self.r.review_packet(self.p, problem["id"])
        body = {"problem_id": problem["id"], "problem_revision": packet["problem"]["revision"],
                "title": "模块路径诊断", "content": "先核对模块路径与当前环境。", "category": "operation",
                "applicability": "相同模块加载错误", "limitations": "只验证记录，不代表业务验收。",
                "evidence_refs": [e["id"] for e in packet["events"]], "validation_level": level}
        return self.r.submit_candidate(self.p, **body)["candidate_id"]

    def test_persistent_closed_loop_and_revocation(self):
        candidate = self.candidate()
        self.assertEqual(self.r.search(self.p, "模块路径")["items"], [])
        published = self.r.publish(self.p, candidate)
        self.assertEqual(self.r.publish(self.p, candidate)["revision"], published["revision"])
        self.assertEqual(self.r.search(self.p, "模块路径")["items"][0]["knowledge_id"], published["knowledge_id"])
        self.r.close()
        self.r = Runtime(self.home)
        self.assertEqual(self.r.read(self.p, published["knowledge_id"])["revision"], 1)
        self.r.revoke(self.p, published["knowledge_id"], expected_revision=self.r.read(self.p, published["knowledge_id"])["revision"])
        self.assertEqual(self.r.search(self.p, "模块路径")["items"], [])
        with self.assertRaises(ContractError): self.r.read(self.p, published["knowledge_id"])

    def test_two_chats_with_same_tool_id_do_not_mix(self):
        self.problem()
        self.hook("PostToolUse", session_id="chat-b", tool_use_id="failed-call", tool_name="Bash", tool_response={"exit_code": 0})
        operations = self.r.db.execute("SELECT conversation,state FROM operations ORDER BY conversation").fetchall()
        self.assertEqual([(r[0], r[1]) for r in operations], [("chat-a", "failed"), ("chat-b", "succeeded")])
        for event in self.r.review_packet(self.p, self.r.problems(self.p)[0]["id"])["events"]:
            source = self.r.db.execute("SELECT conversation FROM events WHERE id=?", (event["id"],)).fetchone()
            self.assertEqual(source[0], "chat-a")

    def test_replay_and_missing_ids_are_distinct(self):
        self.problem()
        count = self.r.status(self.p)["counts"]["events"]
        self.problem()
        self.assertEqual(self.r.status(self.p)["counts"]["events"], count)
        for _ in range(2): self.hook("PostToolUse", tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.status(self.p)["counts"]["events"], count + 2)

    def test_running_is_not_success_and_binding_survives_scope_change(self):
        self.hook("PreToolUse", tool_use_id="long", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="long", tool_name="Bash", tool_response={"session_id": 42, "exit_code": None})
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "running")
        task = self.r.task(self.p, "chat-a")
        self.r.task_update(self.p, "chat-a", "只读新范围", expected_revision=1,
                           expected_task_id=task["id"], expected_write_version=task["write_version"])
        self.hook("PostToolUse", tool_use_id="long", tool_name="Bash", tool_response={"exit_code": 0})
        operation = self.r.db.execute("SELECT state,task_revision FROM operations").fetchone()
        self.assertEqual(tuple(operation), ("succeeded", 1))

    def test_cancel_and_interrupt_preserve_unknown(self):
        self.hook("PreToolUse", tool_use_id="open", tool_name="Bash")
        self.r.config["enforce_task_policy"] = True
        self.hook("Interrupt")
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "unknown")
        self.r.task_update(self.p, "chat-a", "修复工具错误", {"mode": "analysis"}, status="cancelled")
        result = self.hook("PreToolUse", tool_use_id="blocked", tool_name="Bash")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.r.db.execute("SELECT state FROM operations ORDER BY rowid DESC LIMIT 1").fetchone()[0], "denied")

    def test_source_change_rejects_old_candidate(self):
        candidate = self.candidate()
        self.hook("PreToolUse", tool_use_id="new-evidence", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="new-evidence", tool_name="Bash", tool_response={"exit_code": 0})
        with self.assertRaises(ContractError): self.r.publish(self.p, candidate)

    def test_historical_delivery_remains_reviewable_after_new_task(self):
        candidate = self.candidate()
        self.r.task_update(self.p, "chat-a", "修复工具错误", status="delivered")
        self.assertEqual(self.r.task(self.p, "chat-a")["revision"], 1)
        self.assertEqual(self.r.task(self.p, "chat-a")["scope"], {"mode": "analysis"})
        published = self.r.publish(self.p, candidate)
        self.r.task_update(self.p, "chat-a", "另一项任务", new_task=True)
        self.assertEqual(self.r.publish(self.p, candidate)["knowledge_id"], published["knowledge_id"])

    def test_no_success_promotion_from_reported_or_running(self):
        with self.assertRaises(ContractError): self.candidate(level="tool_success")
        self.assertEqual(Runtime.outcome({"isError": False}), "call_completed")
        self.assertEqual(Runtime.outcome("Process exited with code 0"), "unknown")
        self.assertEqual(Runtime.outcome({"exitCode": 0}), "succeeded")

    def test_current_feedback_cannot_reuse_old_task_success(self):
        self.hook("PostToolUse", tool_use_id="old-success", tool_name="Bash", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        self.r.task_update(self.p, "chat-a", "新任务", new_task=True)
        with self.assertRaises(ContractError):
            self.r.feedback(self.p, "chat-a", "新结论", event_refs=[event])

    def test_revoked_injected_knowledge_reports_invalidation_before_tool(self):
        published = self.r.publish(self.p, self.candidate())
        self.hook("UserPromptSubmit", turn_id="memory", prompt="模块路径")
        self.r.revoke(self.p, published["knowledge_id"], expected_revision=self.r.read(self.p, published["knowledge_id"])["revision"])
        result = self.hook("PreToolUse", tool_use_id="check-revocation", tool_name="Bash")
        self.assertIn(published["knowledge_id"], result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.r.search(self.p, "模块路径")["items"], [])

    def test_late_failure_stays_with_original_task(self):
        self.hook("PreToolUse", tool_use_id="late-failure", tool_name="Bash")
        old = self.r.task(self.p, "chat-a")
        self.r.task_update(self.p, "chat-a", "新任务", new_task=True)
        self.hook("PostToolUse", tool_use_id="late-failure", tool_name="Bash", tool_response={"exit_code": 1})
        problem = self.r.problems(self.p)[0]
        self.assertEqual(problem["task_id"], old["id"])

    def test_compression_reinjects_without_duplicate_body(self):
        published = self.r.publish(self.p, self.candidate())
        first = self.hook("UserPromptSubmit", turn_id="one", prompt="模块路径")
        self.assertIn(published["knowledge_id"], first["hookSpecificOutput"]["additionalContext"])
        second = self.hook("UserPromptSubmit", turn_id="two", prompt="模块路径")
        self.assertNotIn(published["knowledge_id"], second["hookSpecificOutput"]["additionalContext"])
        compact = self.hook("SessionStart", source="compact")
        self.assertIn(published["knowledge_id"], compact["hookSpecificOutput"]["additionalContext"])
        clear = self.hook("SessionStart", source="clear")
        self.assertIn(published["knowledge_id"], clear["hookSpecificOutput"]["additionalContext"])

    def test_mcp_role_and_project_boundaries(self):
        worker, admin = Server(self.r, self.p), Server(self.r, self.p, "admin")
        for server in (worker, admin):
            with self.assertRaises(ContractError): server.call("publish", {"candidate_id": "anything"})
        with self.assertRaises(ContractError): worker.call("submit_candidate", {})
        with self.assertRaises(ContractError): worker.call("knowledge_search", {"query": "x", "role": "admin"})
        self.assertEqual(self.r.project(self.workspace / "src"), self.p)
        p2 = self.r.project(self.workspace.parent / "other")
        with self.assertRaises(ContractError): self.r.review_packet(p2, self.problem()["id"])

    def test_admin_budget_is_durable_and_new_problems_are_not_starved(self):
        self.problem()
        fake = Path(self.temp.name) / "reviewer.py"
        fake.write_text("import sys,json,pathlib\nargs=sys.argv\njson.loads(sys.stdin.read().split('<untrusted_evidence>')[1].split('</untrusted_evidence>')[0])\npathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({'decision':'skip','reason':'no new fact','candidate':None}),encoding='utf-8')\n", encoding="utf-8")
        self.r.config["admin"].update(enabled=True, command=[sys.executable, str(fake)], max_calls_per_day=1)
        self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.r.feedback(self.p, "chat-a", "另一项专业问题", category="professional")
        self.assertEqual(admin_once(self.r, self.p)["status"], "budget_exhausted")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_plugin_observation_does_not_create_learning_loop(self):
        problem = self.problem()
        self.hook("PostToolUse", tool_use_id="query", tool_name="mcp__agent_learning__knowledge_search", tool_response={"success": True})
        self.hook("PostToolUse", tool_use_id="read", tool_name="Bash", tool_response={"output": "status"})
        self.assertEqual(self.r.problems(self.p)[0]["revision"], problem["revision"])
        self.hook("PostToolUse", tool_use_id="repair", tool_name="Bash", tool_response={"exit_code": 0})
        first_revision = self.r.problems(self.p)[0]["revision"]
        self.hook("PostToolUse", tool_use_id="later", tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.problems(self.p)[0]["revision"], first_revision)

    def test_expired_sync_does_not_blindly_repeat_remote_write(self):
        self.r.config["hindsight"]["enabled"] = True
        self.r.publish(self.p, self.candidate())
        self.r.db.execute("UPDATE outbox SET state='running',lease='crashed',lease_until=0")
        self.assertEqual(self.r.sync_once(self.p)["status"], "idle")
        self.assertEqual(self.r.status(self.p)["outbox_states"], {"blocked": 1})

    def test_stdio_protocol_native_process(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "native-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "learning_status", "arguments": {}}},
        ]
        script = Path(__file__).resolve().parents[1] / "scripts/learning.py"
        process = subprocess.run([sys.executable, str(script), "--home", str(self.home), "--workspace", str(self.workspace), "serve"],
                                 input="\n".join(json.dumps(m) for m in messages) + "\n", text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertNotIn("submit_candidate", {t["name"] for t in replies[1]["result"]["tools"]})
        self.assertFalse(replies[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
