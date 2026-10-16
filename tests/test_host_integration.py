"""通过真实 Python 子进程检查官方 Hook 事件和持久状态，不启动模型。"""

from __future__ import annotations

import json
import re
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_learning.runtime import Runtime
from agent_learning.install import install


class HostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alp host checks ")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.workspace = base / "project with spaces"
        self.workspace.mkdir()
        self.home = base / "runtime data"
        self.home.mkdir()
        (self.home / "config.json").write_text(
            json.dumps({"admin": {"enabled": False}, "hindsight": {"enabled": False}}),
            encoding="utf-8",
        )
        self.environment = os.environ.copy()
        self.environment.pop("ALP_CONFIG", None)
        self.environment.pop("ALP_DISABLED", None)
        self.environment["ALP_HOME"] = str(self.home)
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def dispatch(self, kind, **fields):
        event = {
            "session_id": "conversation-a",
            "cwd": str(self.workspace),
            "transcript_path": None,
            "hook_event_name": kind,
            "model": "host-contract-check",
            **fields,
        }
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "dispatch.py")],
            input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            cwd=self.workspace,
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stderr, b"")
        # json.loads 检查 stdout 只有一个 JSON 值，没有日志/调试前后缀。
        output = json.loads(result.stdout.decode("utf-8"))
        self.assertIsInstance(output, dict)
        self.assertNotIn("systemMessage", output, output)
        self.assertNotIn("decision", output)
        self.assertNotEqual(output.get("continue"), False)
        if "hookSpecificOutput" in output:
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], kind)
            self.assertIsInstance(output["hookSpecificOutput"]["additionalContext"], str)
        return output

    def rows(self, query, params=()):
        connection = sqlite3.connect(self.home / "runtime.sqlite3")
        try:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, params)]
        finally:
            connection.close()

    def run_cli(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/learning.py"), "--workspace", str(self.workspace), *arguments],
            cwd=self.workspace, env=self.environment, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def test_cli_scope_omission_preserves_existing_task_scope(self):
        first = self.run_cli("task", "--conversation", "cli-scope", "--goal", "compile",
                             "--scope", '{"mode":"analysis"}')
        after = self.run_cli("task", "--conversation", "cli-scope", "--goal", "compile", "--status", "delivered")
        self.assertEqual(after["scope"], {"mode": "analysis"})
        self.assertEqual(after["revision"], first["revision"])
        self.assertEqual(after["status"], "delivered")

    def test_mcp_utf8_in_source_and_console_entry_with_legacy_encoding(self):
        environment = dict(self.environment, PYTHONUTF8="0", PYTHONIOENCODING="gbk", PYTHONPATH=str(ROOT))
        entries = {
            "source": [sys.executable, str(ROOT / "scripts/learning.py")],
            # console script调用cli.main，不经过scripts/learning.py的编码初始化。
            "console": [sys.executable, "-c", "from agent_learning.cli import main; raise SystemExit(main())"],
        }
        for entry, command in entries.items():
            with self.subTest(entry=entry):
                goal = "检查中文编译问题：读取正确知识"
                session = self.dispatch("SessionStart", session_id="unicode-" + entry,
                                        source="startup", permission_mode="default")
                context = session["hookSpecificOutput"]["additionalContext"]
                import re
                handle = re.search(r"context_handle=([a-f0-9]{32})", context).group(1)
                messages = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "host-contract-check", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "task_update", "arguments": {"context_handle": handle, "goal": goal}}},
                ]
                body = ("\n".join(json.dumps(item, ensure_ascii=False) for item in messages) + "\n").encode("utf-8")
                result = subprocess.run(
                    [*command, "--workspace", str(self.workspace), "serve"],
                    input=body, cwd=self.workspace, env=environment, capture_output=True, timeout=5, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                responses = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
                self.assertEqual(len(responses), 2)
                self.assertFalse(responses[-1]["result"]["isError"])
                self.assertEqual(json.loads(responses[-1]["result"]["content"][0]["text"])["goal"], goal)
                saved = self.rows("SELECT goal FROM tasks WHERE conversation=?", ("unicode-" + entry,))
                self.assertEqual(saved[0]["goal"], goal)

    def test_cli_metadata_filters_and_explicit_knowledge_update(self):
        runtime = Runtime(self.home)
        try:
            project = runtime.project(self.workspace)
            runtime.task_update(project, "cli-publish", "compiler recovery")
            problem = runtime.feedback(project, "cli-publish", "compiler recovery", category="operation")
            packet = runtime.review_packet(project, problem["id"])
        finally:
            runtime.close()
        body = {
            "problem_id": problem["id"], "problem_revision": packet["problem"]["revision"],
            "title": "compiler recovery", "content": "Check the configured compiler path.",
            "category": "operation", "applicability": "Windows compiler.",
            "limitations": "Reported guidance only.", "evidence_refs": [packet["events"][0]["id"]],
            "validation_level": "reported", "metadata": {"tool": "compiler", "environment": "windows"},
            "knowledge_kind": "pattern",
        }
        candidate_file = self.workspace / "candidate.json"
        candidate_file.write_text(json.dumps(body), encoding="utf-8")
        first_candidate = self.run_cli("propose", str(candidate_file))
        first = self.run_cli("publish", first_candidate["candidate_id"])
        body.update(title="compiler recovery revised", content="Check the compiler executable and version.")
        candidate_file.write_text(json.dumps(body), encoding="utf-8")
        update_candidate = self.run_cli("propose", str(candidate_file))
        updated = self.run_cli("publish", update_candidate["candidate_id"],
                               "--knowledge-id", first["knowledge_id"], "--expected-revision", str(first["revision"]))
        self.assertEqual(updated["knowledge_id"], first["knowledge_id"])
        self.assertEqual(updated["revision"], first["revision"] + 1)
        selected = self.run_cli("search", "compiler", "--filters", '{"tool":"compiler","environment":"windows","category":"operation","knowledge_kind":"pattern"}')
        self.assertEqual([item["knowledge_id"] for item in selected["items"]], [first["knowledge_id"]])
        self.assertEqual(selected["items"][0]["title"], body["title"])
        self.assertEqual(self.run_cli("search", "compiler", "--filters", '{"environment":"linux"}')["items"], [])
        self.assertEqual(self.run_cli("search", "compiler", "--filters", '{"knowledge_kind":"fact"}')["items"], [])
        # 真实子进程重放、撤销、恢复和在线备份，检查持久数据没有被旧候选覆盖。
        self.assertEqual(self.run_cli("publish", first_candidate["candidate_id"]), first)
        self.assertEqual(self.run_cli("read", first["knowledge_id"])["revision"], updated["revision"])
        self.assertTrue(self.run_cli("revoke", first["knowledge_id"], "--expected-revision", str(updated["revision"]))["revoked"])
        self.assertEqual(self.run_cli("publish", first_candidate["candidate_id"]), first)
        self.assertEqual(self.run_cli("search", "compiler")["items"], [])
        restored = self.run_cli("restore", first["knowledge_id"], "--expected-revision", str(updated["revision"]))
        self.assertEqual(restored["revision"], updated["revision"])
        self.assertEqual(self.run_cli("doctor")["status"], "ok")
        self.assertEqual(self.run_cli("rebuild-index")["indexed"], 1)
        backup_path = self.home.parent / "backup with spaces.sqlite3"
        self.assertEqual(self.run_cli("backup", str(backup_path))["status"], "backed_up")
        with closing(sqlite3.connect(backup_path)) as copied:
            self.assertEqual(copied.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(copied.execute("SELECT revision FROM active WHERE id=?", (first["knowledge_id"],)).fetchone()[0], updated["revision"])
        backfill = self.run_cli("sync", "--backfill")
        self.assertEqual(backfill["sync"]["status"], "disabled")
        self.assertIn("backfill", backfill)

    def test_official_identity_fields_and_nullable_stop(self):
        self.dispatch("SessionStart", source="startup", permission_mode="default")
        self.dispatch("UserPromptSubmit", turn_id="turn-a", prompt="核对当前编译问题")
        self.dispatch("SubagentStart", turn_id="turn-a", agent_id="child-a", agent_type="worker")
        self.dispatch(
            "PreToolUse", turn_id="turn-a", tool_use_id="call-a", tool_name="Bash",
            tool_input={"command": "compiler --check"},
        )
        self.dispatch("Stop", turn_id="turn-a", stop_hook_active=False, last_assistant_message=None)
        rows = self.rows("SELECT type,agent,payload FROM events ORDER BY created")
        child = next(row for row in rows if row["type"] == "SubagentStart")
        self.assertEqual(child["agent"], "child-a")
        operation = next(row for row in rows if row["type"] == "PreToolUse")
        self.assertIsNone(operation["agent"])
        self.assertFalse(json.loads(operation["payload"])["agent_identity_known"])
        stop = next(row for row in rows if row["type"] == "Stop")
        self.assertIsNone(json.loads(stop["payload"])["last_assistant_message"])

    def test_long_command_only_succeeds_on_terminal_result(self):
        common = {
            "turn_id": "turn-long", "tool_use_id": "original-command",
            "tool_name": "Bash", "tool_input": {"command": "long-running-check"},
        }
        self.dispatch("PreToolUse", **common)
        self.assertEqual(self.rows("SELECT state FROM operations")[0]["state"], "started")
        self.dispatch("PostToolUse", **common, tool_response={"session_id": 451, "output": "running"})
        self.assertEqual(self.rows("SELECT state FROM operations")[0]["state"], "running")
        self.assertEqual(self.rows("SELECT COUNT(*) AS total FROM problems")[0]["total"], 0)
        self.dispatch("Stop", turn_id="turn-long", last_assistant_message="命令仍在运行")
        self.assertEqual(self.rows("SELECT state FROM operations")[0]["state"], "running")
        completed = {"exit_code": 0, "output": "check complete"}
        self.dispatch("PostToolUse", **common, tool_response=completed)
        self.assertEqual(self.rows("SELECT state FROM operations")[0]["state"], "succeeded")
        before = self.rows("SELECT COUNT(*) AS total FROM events")[0]["total"]
        self.dispatch("PostToolUse", **common, tool_response=completed)
        self.assertEqual(self.rows("SELECT COUNT(*) AS total FROM events")[0]["total"], before)
        self.dispatch("PostToolUse", **common, tool_response={"session_id": 451, "output": "late observation"})
        self.assertEqual(self.rows("SELECT state FROM operations")[0]["state"], "succeeded")

    def test_compact_restores_knowledge_once_in_new_context(self):
        runtime = Runtime(self.home)
        try:
            project = runtime.project(self.workspace)
            runtime.task_update(project, "conversation-a", "compiler recovery")
            problem = runtime.feedback(project, "conversation-a", "compiler recovery", category="operation")
            packet = runtime.review_packet(project, problem["id"])
            candidate = runtime.submit_candidate(
                project, problem_id=problem["id"], problem_revision=packet["problem"]["revision"],
                title="compiler recovery", content="Check the configured compiler path.",
                category="operation", applicability="This test workspace only.",
                limitations="Reported guidance; gameplay remains unverified.",
                evidence_refs=[packet["events"][0]["id"]], validation_level="reported",
            )
            published = runtime.publish(project, candidate["candidate_id"])
        finally:
            runtime.close()
        first = self.dispatch("UserPromptSubmit", turn_id="first", prompt="compiler recovery")
        self.assertIn("compiler recovery", first["hookSpecificOutput"]["additionalContext"])
        repeat = self.dispatch("UserPromptSubmit", turn_id="second", prompt="compiler recovery")
        self.assertNotIn(published["knowledge_id"], repeat.get("hookSpecificOutput", {}).get("additionalContext", ""))
        restored = self.dispatch("SessionStart", source="compact")
        self.assertIn("compiler recovery", restored["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.rows("SELECT epoch FROM contexts")[0]["epoch"], 1)
        after = self.dispatch("UserPromptSubmit", turn_id="third", prompt="compiler recovery")
        self.assertNotIn(published["knowledge_id"], after.get("hookSpecificOutput", {}).get("additionalContext", ""))

    def test_fork_start_matches_export_and_registers_context_before_a_new_prompt(self):
        destination = self.workspace.parent / "fork plugin export"
        install(destination, runtime_store=self.workspace.parent / "fork releases")
        handlers = json.loads((destination / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        self.assertTrue(any(re.fullmatch(handler["matcher"], "fork") for handler in handlers))
        output = self.dispatch("SessionStart", source="fork")
        self.assertIn("context_handle=", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(self.rows("SELECT * FROM contexts")), 1)
        self.assertEqual(self.rows("SELECT model FROM admin_model_observations")[0]["model"], "host-contract-check")

    @unittest.skipUnless(os.name == "nt", "Windows Hook command contract")
    def test_exported_windows_command_accepts_json_stdin(self):
        destination = self.workspace.parent / "installed plugin copy"
        install(destination, runtime_store=self.workspace.parent / "runtime releases")
        handlers = json.loads((destination / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        handler = handlers["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertIn("commandWindows", handler)
        self.assertNotIn("command_windows", handler)
        command = handler["commandWindows"]
        self.assertIn("EncodedCommand", command)
        cached = self.workspace.parent / "cached plugin copy"
        shutil.copytree(destination, cached)
        # 宿主资源可来自缓存；实际入口固定到已校验release，导出源变更不影响运行。
        (destination / "hooks/dispatch.py").write_text("raise RuntimeError('export source must not execute')\n", encoding="utf-8")
        event = {
            "session_id": "installed-conversation", "turn_id": "installed-turn",
            "cwd": str(self.workspace), "transcript_path": None,
            "hook_event_name": "UserPromptSubmit", "prompt": "检查安装副本的中文输入",
        }
        # Windows shell=True 使用真实 COMSPEC，不绕过已导出的命令字符串。
        result = subprocess.run(
            command, shell=True, input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            cwd=self.workspace, env=dict(self.environment, PLUGIN_ROOT=str(cached)), capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stderr, b"")
        output = json.loads(result.stdout.decode("utf-8"))
        self.assertNotIn("systemMessage", output, output)
        recorded = self.rows("SELECT payload FROM events WHERE conversation='installed-conversation'")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(json.loads(recorded[0]["payload"])["prompt"], event["prompt"])


if __name__ == "__main__":
    unittest.main()
