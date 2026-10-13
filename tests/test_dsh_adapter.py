"""DSH (DeepSeek Harness) 事件适配、1.0.0 工作观察/文件提示/管理员摘要、模型路由与历史吸收回归。"""
import contextlib
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import RESPONSE_SCHEMA, admin_once
from agent_learning.admin_providers import build_execution, dsh_usage, read_response
from agent_learning.cli import main
from agent_learning.dsh import DshAdapter, reset_session
from agent_learning.history_sources import scan_sources
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode
from tests.test_admin_providers import stream_events


class DshAdapterAndV1FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src/engine.py").write_text("v = 1\n", encoding="utf-8")
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home, config={}, host="dsh")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("dsh-session-1")
        self.task = self.r.task_update(self.p, self.c, "fix pwsh pipeline issue", {"mode": "execute"})
        self.adapter = DshAdapter(self.r, self.workspace)

    def test_session_start_user_prompt_and_reasoning_block_filtering(self):
        # 空白未挂载目录的会话安全跳过，不报错
        unbound = DshAdapter(self.r, workspace=None)
        self.assertEqual(unbound.handle("SessionStart", {"sessionId": "blank-hero"}), {})

        start = self.adapter.handle("SessionStart", {
            "sessionId": "dsh-session-1",
            "cwd": str(self.workspace),
            "source": "startup",
            "requestContext": {"provider": "deepseek", "model": "gemini-4-argon"},
        })
        text = start["hookSpecificOutput"]["additionalContext"]
        self.assertIn("context_handle=", text)
        # 验证根会话模型被精确观察为 '<provider>/<model>'
        obs = self.r.db.execute(
            "SELECT model,active FROM admin_model_observations WHERE project=? AND host_scope='dsh:default' AND conversation=?",
            (self.p, self.c),
        ).fetchone()
        self.assertEqual(tuple(obs), ("deepseek/gemini-4-argon", 1))

        # 验证 UserPromptSubmit 和 Stop 严格剔除 ReasoningBlock / thinking
        self.adapter.handle("UserPromptSubmit", {
            "sessionId": "dsh-session-1",
            "turn": 1,
            "messages": [
                {"role": "user", "content": [
                    {"type": "reasoning", "text": "SECRET_INTERNAL_REASONING_USER"},
                    {"type": "text", "text": "不是这样，实际上是 src/engine.py 的路径问题"},
                ]}
            ],
        })
        self.adapter.handle("Stop", {
            "sessionId": "dsh-session-1",
            "turn": 1,
            "lastAssistantMessage": [
                {"type": "reasoning", "text": "SECRET_INTERNAL_THINKING_ASSISTANT"},
                {"type": "text", "text": "已完成 src/engine.py 检查，等待用户验收"},
            ],
        })
        all_events = self.r.db.execute("SELECT payload FROM events WHERE project=?", (self.p,)).fetchall()
        combined = " ".join(row[0] for row in all_events)
        self.assertNotIn("SECRET_INTERNAL_REASONING_USER", combined)
        self.assertNotIn("SECRET_INTERNAL_THINKING_ASSISTANT", combined)

        # 验证 1.0.0 工作观察捕获了 user_correction 和 public_summary
        memories = self.r.work_search(self.p, "engine.py", workspace=self.workspace)["items"]
        types = {item["type"] for item in memories}
        self.assertIn("user_correction", types)
        self.assertIn("public_summary", types)

    def test_v1_work_memory_file_links_pwsh_exit_code_and_progressive_read(self):
        start = self.adapter.handle("SessionStart", {"sessionId": "dsh-session-1", "source": "startup"})
        handle = start["hookSpecificOutput"]["additionalContext"].split("context_handle=")[1].split(";")[0]

        # 1. DSH pwsh 后台任务应识别为 running，不产生失败或完成结论
        self.adapter.handle("PreToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-bg", "name": "pwsh",
            "arguments": {"command": "python long.py", "run_in_background": True},
        })
        self.adapter.handle("PostToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-bg", "name": "pwsh",
            "arguments": {"command": "python long.py", "run_in_background": True},
            "result": {"isError": False, "value": {"job_id": "job-123"}, "content": [{"type": "text", "text": "started job-123"}]},
        })
        op_bg = self.r.db.execute("SELECT state,observation FROM operations WHERE project=? AND last_event IN "
                                  "(SELECT id FROM events WHERE payload LIKE '%job-123%')", (self.p,)).fetchone()
        self.assertEqual(tuple(op_bg), ("running", "running"))

        # 2. DSH pwsh 非零退出码标记 [exit code: 2] 即使 isError=False 也必须识别为 failed 并生成 tool_failure 工作观察
        self.adapter.handle("PreToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-fail", "name": "pwsh",
            "arguments": {"command": "Get-Content -LiteralPath src/engine.py", "workdir": str(self.workspace)},
        })
        self.adapter.handle("PostToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-fail", "name": "pwsh",
            "arguments": {"command": "Get-Content -LiteralPath src/engine.py", "workdir": str(self.workspace)},
            "result": {"isError": False, "content": [{"type": "text", "text": "pipeline error\n[exit code: 2]"}]},
        })
        # 3. DSH edit 工具修改文件识别为 succeeded 并生成 file_change 工作观察与文件关系
        self.adapter.handle("PreToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-edit", "name": "edit",
            "arguments": {"file_path": "src/engine.py", "old_string": "v = 1", "new_string": "v = 2"},
        })
        (self.workspace / "src/engine.py").write_text("v = 2\n", encoding="utf-8")
        self.adapter.handle("PostToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-edit", "name": "edit",
            "arguments": {"file_path": "src/engine.py", "old_string": "v = 1", "new_string": "v = 2"},
            "result": {"isError": False, "content": [{"type": "text", "text": "Updated src/engine.py"}]},
        })

        # 验证通过 MCP Server 进行 1.0.0 work_memory_search 与渐进分页 work_memory_read
        server = Server(self.r, self.p, workspace=self.workspace)
        search_res = server.call("work_memory_search", {"file_path": "src/engine.py", "context_handle": handle})
        self.assertGreaterEqual(len(search_res["items"]), 2)
        kinds = {item["type"] for item in search_res["items"]}
        self.assertIn("tool_failure", kinds)
        self.assertIn("file_change", kinds)

        target_mem = next(item for item in search_res["items"] if item["type"] == "file_change")
        read_summary = server.call("work_memory_read", {
            "memory_id": target_mem["memory_id"],
            "context_handle": handle,
            "expected_revision": target_mem["revision"],
            "section": "summary",
        })
        self.assertEqual(read_summary["status"], "ok")
        self.assertIn("Updated src/engine.py", read_summary["content"])

        read_evidence = server.call("work_memory_read", {
            "memory_id": target_mem["memory_id"],
            "context_handle": handle,
            "expected_revision": target_mem["revision"],
            "section": "evidence",
        })
        self.assertEqual(read_evidence["status"], "ok")
        self.assertIn("projection_only", read_evidence["content"])

    def test_v1_orphan_post_file_hints_and_session_reset(self):
        # 模拟 observe 模式下无前置 PreToolUse 的孤儿 PostToolUse 修改文件
        self.adapter.handle("PostToolUse", {
            "sessionId": "dsh-session-1",
            "callId": "orphan-edit-1",
            "name": "write",
            "arguments": {"file_path": "src/engine.py"},
            "result": {"isError": False, "content": [{"type": "text", "text": "wrote src/engine.py"}]},
        })
        hints = self.r.db.execute("SELECT COUNT(*) FROM work_file_hints WHERE project=?", (self.p,)).fetchone()[0]
        self.assertEqual(hints, 1)
        items = self.r.work_context_items(
            self.p, self.c, query="", workspace=self.workspace,
            task_state=self.r.task(self.p, self.c), delivery="persistent", context_epoch=0,
        )
        self.assertTrue(any(item.get("task_affinity") == "context_only" for item in items))

        # 执行 dsh-session-reset 后，generation 推进，旧 context_only 文件提示与句柄失效
        reset_res = reset_session(self.r, self.workspace, "dsh-session-1")
        self.assertTrue(reset_res["rebind_required"])
        items_after = self.r.work_context_items(
            self.p, self.c, query="", workspace=self.workspace,
            task_state=self.r.task(self.p, self.c), delivery="persistent", context_epoch=1,
        )
        self.assertFalse(any(item.get("task_affinity") == "context_only" for item in items_after))

    def test_enforce_policy_blocks_pwsh_in_read_only_and_exempts_native_learning_tools(self):
        enforce_adapter = DshAdapter(self.r, self.workspace, "enforce")
        # 原生 DSH 学习工具名称（无 mcp__ 前缀）在未审批任务下依然允许自举调用
        for tool_name in ("session_context", "knowledge_search", "work_memory_search"):
            res = enforce_adapter.handle("PreToolUse", {
                "sessionId": "dsh-session-1", "callId": f"rec-{tool_name}",
                "name": tool_name, "arguments": {},
            })
            self.assertEqual(res["decision"], "allow")

        # 签发 read_only 规则，验证 pwsh 即使被写入 tool_rules 也会因属于 _SHELL_TOOLS 被拒绝
        task = self.r.task(self.p, self.c)
        self.r.approve_execution(
            self.p, self.c, task["id"], task["write_version"],
            {
                "mode": "read_only",
                "allowed_tools": ["read", "pwsh"],
                "tool_rules": {
                    "read": {"action": "read", "path_fields": ["file_path"]},
                    "pwsh": {"action": "read", "path_fields": ["workdir"]},
                },
                "allowed_paths": [str(self.workspace)],
            },
            workspace=str(self.workspace),
        )
        allow_read = enforce_adapter.handle("PreToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-read-ok",
            "name": "read", "arguments": {"file_path": "src/engine.py"},
        })
        self.assertEqual(allow_read["decision"], "allow")

        deny_pwsh = enforce_adapter.handle("PreToolUse", {
            "sessionId": "dsh-session-1", "callId": "call-pwsh-ro",
            "name": "pwsh", "arguments": {"command": "Get-Location", "workdir": str(self.workspace)},
        })
        self.assertEqual(deny_pwsh["decision"], "deny")
        self.assertIn("read_only_action_denied", deny_pwsh["reason"])

    def test_subagent_lifecycle_and_dsh_admin_routing_with_v1_work_summary(self):
        # 根会话启动并观察模型
        self.adapter.handle("SessionStart", {
            "sessionId": "dsh-session-1",
            "source": "startup",
            "requestContext": {"provider": "deepseek", "model": "deepseek-chat"},
        })
        # 子 Agent 启动不应覆盖根会话模型观察
        sub_start = self.adapter.handle("SubagentStart", {
            "parentSessionId": "dsh-session-1",
            "childSessionId": "subagent-child-1",
            "provider": "spawn",
            "prompt": "inspect engine module",
            "requestContext": {"provider": "deepseek", "model": "deepseek-reasoner"},
        })
        self.assertIn("context_handle=", sub_start["hookSpecificOutput"]["additionalContext"])
        # 开启 DSH 管理员 (current 模式) 与 1.0.0 工作摘要 (summary_enabled=True)
        self.r.config["work_memory"]["summary_enabled"] = True
        self.r.admin_configure(self.p, True, provider="host", model_mode="current", command=[sys.executable])
        executor = self.r.resolve_admin_executor(self.p, self.c)
        self.assertEqual(executor["provider"], "dsh")
        self.assertEqual(executor["model"], "deepseek/deepseek-chat")
        self.assertEqual(executor["model_source"], "host_observation")

        # 产生两条有价值的根会话工作观察并在 Stop 时自动排队工作摘要
        for idx in (1, 2):
            self.adapter.handle("PreToolUse", {
                "sessionId": "dsh-session-1", "callId": f"w-{idx}",
                "name": "edit", "arguments": {"file_path": "src/engine.py"},
            })
            self.adapter.handle("PostToolUse", {
                "sessionId": "dsh-session-1", "callId": f"w-{idx}",
                "name": "edit", "arguments": {"file_path": "src/engine.py"},
                "result": {"isError": False, "content": [{"type": "text", "text": f"step {idx} updated engine.py"}]},
            })
        with patch.object(self.r, "_launch_admin"):
            self.adapter.handle("Stop", {
                "sessionId": "dsh-session-1", "turn": 2,
                "lastAssistantMessage": "两次修改已完成，待用户验证",
            })
        link = self.r.db.execute("SELECT problem_id,state FROM work_summary_links WHERE project=?", (self.p,)).fetchone()
        self.assertIsNotNone(link)
        self.assertEqual(link["state"], "pending")

        # 模拟 DSH 流式管理员返回工作摘要（含驼峰 TokenUsage 字段与零工具初始化握手）
        events = [row["id"] for row in self.r.db.execute("SELECT event_id AS id FROM work_memories WHERE project=?", (self.p,))]
        candidate_body = {
            "decision": "candidate",
            "reason": "Summarized observable work history",
            "candidate": {
                "problem_id": link["problem_id"],
                "problem_revision": 1,
                "title": "engine.py 调整工作摘要",
                "content": "完成两次 src/engine.py 编辑",
                "category": "project",
                "applicability": "当前 checkout 的 src/engine.py",
                "limitations": "仅工具观察，未经用户最终验收",
                "evidence_refs": events[:2],
                "validation_level": "reported",
                "metadata": {"tool": "edit", "tool_version": "", "error_code": "", "environment": "dsh", "verification": ""},
                "knowledge_kind": "fact",
            },
            "missing_evidence": [],
            "proposal": None,
        }
        fake_dsh_script = Path(self.temp.name) / "fake_dsh_cli.py"
        fake_dsh_script.write_text(
            "import json, sys\n"
            "def emit(obj): print(json.dumps(obj), flush=True)\n"
            "emit({'event': 'init', 'init': {'tools': [], 'permission_mode': 'request-review', "
            "'agent': 'alp-review', 'model': 'deepseek/deepseek-chat'}})\n"
            "line = sys.stdin.readline()\n"
            "if not line: sys.exit(0)\n"
            "emit({'event': 'step_update', 'step_update': {'step_type': 'thinking', 'text_delta': 'PRIVATE_DSH_REASONING'}})\n"
            f"body = {repr(candidate_body)}\n"
            "emit({'event': 'result', 'result': {'status': 'SUCCESS', 'num_turns': 1, 'structured_output': body, "
            "'usage': {'inputTokens': 150, 'outputTokens': 30, 'cacheReadTokens': 50, 'totalTokens': 180}}})\n",
            encoding="utf-8",
        )
        self.r.admin_configure(
            self.p, True, provider="host", model_mode="current",
            command=[sys.executable, "-B", str(fake_dsh_script)],
        )
        review_res = admin_once(self.r, self.p, link["problem_id"])
        self.assertEqual(review_res["status"], "candidate")
        self.assertEqual(review_res["purpose"], "work_summary")
        self.assertEqual(review_res["validation_level"], "model_reported")
        self.assertEqual(review_res["usage"]["provider"], "dsh")
        self.assertEqual(review_res["usage"]["completeness"], "complete")
        self.assertEqual(review_res["usage"]["token_totals"]["input_tokens"], 150)
        self.assertEqual(review_res["usage"]["token_totals"]["output_tokens"], 30)
        self.assertEqual(review_res["usage"]["token_totals"]["cached_input_tokens"], 50)

    def test_dsh_session_jsonl_history_import_excludes_reasoning_blocks(self):
        dsh_log = self.workspace / "dsh-history.jsonl"
        records = [
            {"version": 1, "id": "dsh-hist-conv-1", "createdAt": 1700000000000, "cwd": str(self.workspace), "isSeeded": False},
            {"type": "turn/start", "seq": 0, "time": 1700000000100, "data": {"turn": 1}},
            {"type": "user/message", "seq": 1, "time": 1700000000200, "data": {
                "role": "user", "content": [{"type": "text", "text": "排查 pwsh 命名管道 EPERM 问题"}]
            }},
            {"type": "assistant/message", "seq": 2, "time": 1700000000300, "data": {
                "turn": 1, "step": 0,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "INTERNAL_MODEL_REASONING_NEVER_SAVE"},
                        {"type": "text", "text": "在 Windows 沙箱下避免嵌套管道捕获，改由 Host 进程执行。"},
                        {"type": "tool-call", "id": "tc-1", "name": "pwsh", "arguments": "{\"command\":\"pwd\"}"},
                    ],
                },
            }},
            {"type": "tool/call", "seq": 3, "time": 1700000000400, "data": {
                "turn": 1, "step": 0, "callId": "tc-1", "name": "pwsh", "arguments": "{\"command\":\"pwd\"}"
            }},
            {"type": "tool/result", "seq": 4, "time": 1700000000500, "data": {
                "turn": 1, "step": 0,
                "message": {"role": "tool", "toolCallId": "tc-1", "content": [{"type": "text", "text": "D:\\Projects\\demo"}]},
            }},
            {"type": "turn/end", "seq": 5, "time": 1700000000600, "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        dsh_log.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        prep = self.r.history_prepare(
            self.p, str(self.workspace),
            extra_sources=[{"path": str(dsh_log), "kind": "conversation"}],
        )
        run_res = self.r.history_run(self.p, prep["job_id"])
        self.assertEqual(run_res["job"]["state"], "materialized")
        self.assertGreaterEqual(run_res["job"]["counts"]["cases"], 1)
        events_dump = " ".join(
            row[0] for row in self.r.db.execute("SELECT payload FROM events WHERE project=?", (self.p,)).fetchall()
        )
        self.assertNotIn("INTERNAL_MODEL_REASONING_NEVER_SAVE", events_dump)
        sources_report = self.r.history_source_report(self.p, prep["job_id"], section="sources")
        conv_sources = [item for item in sources_report["items"] if item["kind"] == "conversation"]
        self.assertEqual(len(conv_sources), 1)
        self.assertEqual(conv_sources[0]["metadata"]["format"], "dsh-session-jsonl")
        self.assertEqual(conv_sources[0]["metadata"]["conversation_id"], "dsh-hist-conv-1")
        self.assertGreaterEqual(conv_sources[0]["metadata"]["excluded_internal_records"], 1)

    def test_dsh_dispatch_script_end_to_end(self):
        script = Path(__file__).resolve().parents[1] / "hooks/dsh_dispatch.py"
        payload = json.dumps({
            "sessionId": "dsh-cli-session",
            "cwd": str(self.workspace),
            "source": "startup",
        }).encode("utf-8")
        env = dict(os.environ if "os" in globals() else __import__("os").environ,
                   PYTHONDONTWRITEBYTECODE="1", ALP_HOME=str(self.home))
        proc = subprocess.run(
            [sys.executable, "-B", str(script), "--event", "SessionStart", "--hook-mode", "observe"],
            input=payload, capture_output=True, env=env, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        output = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("context_handle=", output["hookSpecificOutput"]["additionalContext"])

    def test_dsh_admin_rejects_tool_calling_stream(self):
        directory = Path(self.temp.name) / "dsh-protocol-check"
        directory.mkdir()
        schema = directory / "schema.json"
        output = directory / "output.json"
        log = directory / "events.log"
        schema.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        execution = build_execution(["fake-dsh"], "dsh", "deepseek/deepseek-chat", directory, schema, output, "evidence", 10, selection_mode="fixed")
        self.assertIn("--model", execution["args"])
        self.assertTrue((directory / ".dsh/presets/alp-review.json").is_file())
        preset = json.loads((directory / ".dsh/presets/alp-review.json").read_text(encoding="utf-8"))
        self.assertEqual(preset["tools"], [])
        self.assertFalse(preset["subagent"])
        events = [
            {"event": "init", "init": {"tools": [], "permission_mode": "request-review", "agent": "alp-review"}},
            {"event": "adapter_input_sent"},
            {"type": "tool/call", "data": {"name": "pwsh", "arguments": "{}"}},
            {"event": "result", "result": {"status": "SUCCESS", "num_turns": 1, "structured_output": {
                "decision": "skip", "reason": "ok", "candidate": None, "missing_evidence": [], "proposal": None}}},
        ]
        log.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "disallowed tool"):
            read_response("dsh", output, log)

    def test_dsh_orphan_fault_recovery_and_cli_reset(self):
        # 发布一条与特定错误相关的正式知识
        problem = self.r.feedback(self.p, self.c, "EPERM named pipe sandbox error", "operation")
        packet = self.r.review_packet(self.p, problem["id"])
        cand = self.r.submit_candidate(
            self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="EPERM named pipe sandbox error",
            content="Run subprocess from Host instead of confined pwsh pipe",
            category="operation", applicability="DSH Windows sandbox",
            limitations="Host plugin only", validation_level="reported",
            evidence_refs=[item["id"] for item in packet["events"]],
        )
        published = self.r.publish(self.p, cand["candidate_id"])

        # 在一个尚未创建任务的全新 DSH 会话中触发孤儿 PostToolUse 失败
        self.adapter.handle("PostToolUse", {
            "sessionId": "orphan-dsh-chat",
            "callId": "call-orphan-err",
            "name": "pwsh",
            "arguments": {"command": "node spawn.js"},
            "result": {"isError": True, "error": "EPERM named pipe sandbox error"},
        })
        fault_row = self.r.db.execute(
            "SELECT state,query FROM dsh_faults WHERE project=? AND conversation=?",
            (self.p, self.r.host_conversation("orphan-dsh-chat")),
        ).fetchone()
        self.assertIsNotNone(fault_row)
        self.assertEqual(fault_row["state"], "pending")

        # 下一次 SessionStart 自动消费 dsh_faults 并注入匹配的正式知识
        res = self.adapter.handle("SessionStart", {"sessionId": "orphan-dsh-chat", "source": "startup"})
        injected_text = res["hookSpecificOutput"]["additionalContext"]
        self.assertIn(published["knowledge_id"], injected_text)
        fault_after = self.r.db.execute(
            "SELECT state FROM dsh_faults WHERE project=? AND conversation=?",
            (self.p, self.r.host_conversation("orphan-dsh-chat")),
        ).fetchone()
        self.assertEqual(fault_after["state"], "queried")

        # 测试 CLI dsh-session-reset 子命令
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([
                "--home", str(self.home), "--workspace", str(self.workspace),
                "dsh-session-reset", "--conversation", "orphan-dsh-chat",
            ])
        self.assertEqual(code, 0)
        reset_payload = json.loads(out.getvalue())
        self.assertEqual(reset_payload["conversation_id"], "dsh:default:orphan-dsh-chat")
        self.assertTrue(reset_payload["rebind_required"])


if __name__ == "__main__":
    unittest.main()
