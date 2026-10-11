"""真实本地Runtime的MCP身份和协议边界；不启动模型、UE或外部记忆服务。"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import CANDIDATE, WORKER_TOOLS, Server, validate
from agent_learning.runtime import ContractError, Runtime


class LimitedInput(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_sizes = []

    def readline(self, size=-1):
        self.read_sizes.append(size)
        return super().readline(size)


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-mcp-protocol-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.runtime = Runtime(base / "home", config={"admin": {"enabled": False},
                                                      "hindsight": {"enabled": False}})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.ids = 0

    def request(self, server, method, params=None):
        self.ids += 1
        message = {"jsonrpc": "2.0", "id": self.ids, "method": method}
        if params is not None:
            message["params"] = params
        return server.request(message)

    def ready(self, handle=None, role="worker", version="2025-11-25"):
        server = Server(self.runtime, self.project, role, context_handle=handle)
        reply = self.request(server, "initialize", {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "local-protocol-check", "version": "1"}})
        self.assertNotIn("error", reply)
        self.assertIsNone(server.request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        return server

    def bind(self, conversation="chat-a", agent_id=None):
        return self.runtime.register_context(self.project, conversation, agent_id=agent_id)["context_handle"]

    def tool(self, server, name, args=None):
        return self.request(server, "tools/call", {"name": name, "arguments": args or {}})

    def data(self, reply):
        self.assertFalse(reply["result"]["isError"], reply)
        return json.loads(reply["result"]["content"][0]["text"])

    def published(self):
        task = self.runtime.task_update(self.project, "knowledge-source", "compiler recovery")
        problem = self.runtime.feedback(self.project, "knowledge-source", "compiler recovery", category="operation")
        packet = self.runtime.review_packet(self.project, problem["id"])
        candidate = self.runtime.submit_candidate(
            self.project, problem_id=problem["id"], problem_revision=packet["problem"]["revision"],
            title="compiler recovery", content="Check the configured compiler executable.",
            category="operation", applicability="This local workspace.", limitations="Reported guidance only.",
            evidence_refs=[packet["events"][0]["id"]], validation_level="reported")
        return self.runtime.publish(self.project, candidate["candidate_id"]), task

    def test_invalid_envelopes_do_not_reach_business_calls(self):
        server = self.ready()
        for value in (None, True, 1.5, [], {}):
            with self.subTest(value=value):
                response = server.request({"jsonrpc": "2.0", "id": value, "method": "ping"})
                self.assertEqual(response["error"]["code"], -32600)
                self.assertIsNone(response["id"])
        for method in (None, [], 7, ""):
            with self.subTest(method=method):
                response = server.request({"jsonrpc": "2.0", "id": "bad-method", "method": method})
                self.assertEqual(response["error"]["code"], -32600)
        self.assertEqual(server.request({"jsonrpc": "2.0", "id": "valid", "method": "ping"})["result"], {})

    def test_initialization_is_complete_and_required_before_mutation(self):
        server = Server(self.runtime, self.project)
        denied = self.tool(server, "task_update", {"goal": "must not write"})
        self.assertEqual(denied["error"]["message"], "initialization_required")
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        for params in ({}, {"protocolVersion": "2025-11-25", "capabilities": {}},
                       {"protocolVersion": [], "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}):
            self.assertEqual(self.request(server, "initialize", params)["error"]["code"], -32602)
            self.assertEqual(server.state, "new")
        self.request(server, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "x", "version": "1"}})
        self.assertEqual(self.request(server, "tools/list")["error"]["code"], -32002)
        self.assertIsNone(server.request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIn("tools", self.request(server, "tools/list")["result"])
        self.assertEqual(self.request(server, "initialize", {})["error"]["message"], "already_initialized")

    def test_protocol_and_tool_business_errors_are_distinct(self):
        server = self.ready()
        for params in ({}, {"name": []}, {"name": "task_update", "arguments": []}, {"name": "missing"}):
            self.assertEqual(self.request(server, "tools/call", params)["error"]["code"], -32602)
        self.assertEqual(self.request(server, "tools/call", [])["error"]["code"], -32602)
        bad_goal = self.tool(server, "task_update", {"goal": " "})
        self.assertTrue(bad_goal["result"]["isError"])
        self.assertNotIn("error", bad_goal)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM session_projects").fetchone()[0], 0)

    def test_schema_guides_allowed_categories_and_keeps_metadata_optional(self):
        contracts = {name: contract for name, _, contract in WORKER_TOOLS}
        self.assertNotIn("metadata", CANDIDATE["required"])
        self.assertNotIn("knowledge_kind", CANDIDATE["required"])
        validate({"query": "C++", "filters": {"category": "professional", "knowledge_kind": "pattern"}},
                 contracts["knowledge_search"])
        for filters in ({"knowledge_kind": "verified"}, {"category": "any"}, {"tool": ""}, {"sql": "1=1"}):
            with self.assertRaises(ContractError):
                validate({"query": "compiler", "filters": filters}, contracts["knowledge_search"])
        self.assertNotIn("conversation_id", contracts["task_update"]["required"])
        for value in ("verified", "unrestricted"):
            with self.assertRaises(ContractError):
                validate({"summary": "problem", "category": value}, contracts["case_feedback"])
        for limit in (0, 11, True):
            with self.assertRaises(ContractError):
                validate({"query": "compiler", "limit": limit}, contracts["knowledge_search"])

    def test_worker_role_cannot_discover_or_call_admin_tools(self):
        worker = self.ready()
        names = {item["name"] for item in self.request(worker, "tools/list")["result"]["tools"]}
        self.assertIn("session_context", names)
        for name in ("problem_list", "review_packet", "submit_candidate", "publish"):
            self.assertNotIn(name, names)
            self.assertEqual(self.tool(worker, name)["error"]["code"], -32602)
        admin = self.ready(role="admin")
        self.assertEqual(self.data(self.tool(admin, "problem_list")), [])
        self.assertEqual(self.tool(admin, "publish")["error"]["code"], -32602)

    def test_unbound_context_returns_only_project_and_requires_handle_for_writes(self):
        self.bind("another-session")
        server = self.ready()
        context = self.data(self.tool(server, "session_context"))
        self.assertFalse(context["context_bound"])
        self.assertTrue(context["rebind_required"])
        for key in ("context_handle", "conversation_id", "agent_id", "task"):
            self.assertNotIn(key, context)
        refused = self.tool(server, "task_update", {"goal": "test", "conversation_id": "another-session"})
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("session_context_required", refused["result"]["content"][0]["text"])

    def test_valid_handle_resolves_conversation_and_recovers_task_revision(self):
        handle = self.bind()
        server = self.ready()
        updated = self.data(self.tool(server, "task_update", {"context_handle": handle, "goal": "中文检查"}))
        self.assertEqual(updated["conversation"], "chat-a")
        self.assertEqual(server.context_handle, handle)
        recovered = self.data(self.tool(server, "session_context"))
        self.assertEqual(recovered["conversation_id"], "chat-a")
        self.assertEqual(recovered["task"]["revision"], updated["revision"])
        feedback = self.data(self.tool(server, "case_feedback", {"summary": "中文依据", "category": "professional",
            "expected_task_id": updated["id"], "expected_task_revision": updated["revision"]}))
        self.assertEqual(feedback["conversation"], "chat-a")

    def test_connection_cannot_switch_handle_or_conversation(self):
        first, second = self.bind("first"), self.bind("second")
        server = self.ready()
        self.data(self.tool(server, "session_context", {"context_handle": first}))
        for args in ({"context_handle": second, "goal": "wrong"},
                     {"conversation_id": "second", "goal": "wrong"}):
            reply = self.tool(server, "task_update", args)
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("context_mismatch", reply["result"]["content"][0]["text"])
        self.assertIsNone(self.runtime.task(self.project, "second"))

    def test_project_mismatch_does_not_rebind_foreign_context(self):
        handle = self.bind()
        other = self.runtime.project(self.workspace.parent / "other-project")
        server = Server(self.runtime, other)
        with self.assertRaises(ContractError):
            server.call("task_update", {"context_handle": handle, "goal": "wrong project"})
        self.assertIsNone(self.runtime.task(other, "chat-a"))
        self.assertEqual(self.runtime.access_context(self.project, handle)["project_id"], self.project)

    def test_child_context_cannot_overwrite_parent_task(self):
        parent = self.bind()
        server = self.ready(parent)
        original = self.data(self.tool(server, "task_update", {"goal": "parent task"}))
        child = self.ready(self.bind(agent_id="child-a"))
        context = self.data(self.tool(child, "session_context"))
        self.assertEqual(context["agent_id"], "child-a")
        refused = self.tool(child, "task_update", {"goal": "replace parent task"})
        self.assertTrue(refused["result"]["isError"])
        self.assertEqual(self.runtime.task(self.project, "chat-a")["goal"], original["goal"])

    def test_account_change_requires_old_connection_to_reconnect(self):
        server = self.ready(self.bind())
        original = self.data(self.tool(server, "task_update", {"goal": "original"}))
        self.runtime.account_changed(notification_id="account-change-a")
        for method, params in (("ping", {}), ("tools/list", {}),
                               ("tools/call", {"name": "task_update", "arguments": {"goal": "stale"}})):
            reply = self.request(server, method, params)
            self.assertEqual(reply["error"]["message"], "reconnect_required")
        self.assertEqual(self.runtime.task(self.project, "chat-a")["goal"], original["goal"])
        self.assertIsNone(server.request({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_account_change_keeps_project_knowledge_shared_and_invalidates_handles(self):
        published, _ = self.published()
        old_handle = self.bind()
        epoch = self.runtime.host_epoch()
        changed = self.runtime.account_changed(notification_id="account-change-b")
        self.assertGreater(changed["host_epoch"], epoch)
        repeated = self.runtime.account_changed(notification_id="account-change-b")
        self.assertEqual(repeated["host_epoch"], changed["host_epoch"])
        new_server = self.ready()
        found = self.data(self.tool(new_server, "knowledge_search", {"query": "compiler recovery"}))
        self.assertEqual(found["items"][0]["knowledge_id"], published["knowledge_id"])
        with self.assertRaises(ContractError):
            new_server.call("task_update", {"context_handle": old_handle, "goal": "stale handle"})
        new_handle = self.bind()
        fresh = self.ready(new_handle)
        self.assertEqual(self.data(self.tool(fresh, "session_context"))["conversation_id"], "chat-a")

    def test_epoch_change_between_context_check_and_transaction_cannot_write(self):
        server = self.ready(self.bind())
        self.data(self.tool(server, "task_update", {"goal": "original"}))
        original_access = self.runtime.access_context

        def change_after_check(*args, **kwargs):
            context = original_access(*args, **kwargs)
            self.runtime.account_changed(notification_id="race-after-check")
            return context

        with patch.object(self.runtime, "access_context", side_effect=change_after_check):
            reply = self.tool(server, "task_update", {"goal": "must not persist"})
        self.assertEqual(reply["error"]["message"], "reconnect_required")
        self.assertEqual(self.runtime.task(self.project, "chat-a")["goal"], "original")

    def test_negotiated_versions_keep_text_and_gate_structured_content(self):
        for version in Server.SUPPORTED_VERSIONS | {"2099-01-01"}:
            with self.subTest(version=version):
                server = self.ready(version=version)
                reply = self.tool(server, "session_context")
                body = self.data(reply)
                self.assertEqual("structuredContent" in reply["result"],
                                 server.protocol_version in {"2025-06-18", "2025-11-25"})
                if "structuredContent" in reply["result"]:
                    self.assertEqual(reply["result"]["structuredContent"], body)

    def test_bounded_frame_drain_recovers_next_message(self):
        server = Server(self.runtime, self.project)
        server.MAX_MESSAGE_BYTES = 64
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        incoming = LimitedInput(b"x" * 200 + b"\n" + ping)
        outgoing = io.StringIO()
        with patch.object(sys, "stdin", incoming), patch.object(sys, "stdout", outgoing), patch.object(sys, "stderr", io.StringIO()):
            server.serve()
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["error"]["message"], "message too large")
        self.assertEqual(replies[1]["result"], {})
        self.assertEqual(set(incoming.read_sizes), {65})

    def test_invalid_json_utf8_and_nonfinite_numbers_recover(self):
        server = Server(self.runtime, self.project)
        incoming = LimitedInput(b'bad-json\n\xff\n{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'
                                b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        outgoing = io.StringIO()
        with patch.object(sys, "stdin", incoming), patch.object(sys, "stdout", outgoing), patch.object(sys, "stderr", io.StringIO()):
            server.serve()
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([reply.get("error", {}).get("code") for reply in replies], [-32700, -32700, -32700, None])
        self.assertEqual(replies[-1]["id"], 2)

    def test_cli_session_binding_and_account_notification_use_local_runtime(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, ALP_HOME=str(self.runtime.home), PYTHONDONTWRITEBYTECODE="1")
        environment.pop("ALP_CONFIG", None)

        def run(*arguments):
            process = subprocess.run([sys.executable, str(root / "scripts/learning.py"), "--workspace",
                                      str(self.workspace), *arguments], cwd=self.workspace, env=environment,
                                     capture_output=True, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
            return json.loads(process.stdout.decode("utf-8"))

        registered = run("session-bind", "--conversation", "cli-session")
        self.assertEqual(registered["conversation_id"], "cli-session")
        changed = run("account-changed", "--notification-id", "cli-account-change")
        repeated = run("account-changed", "--notification-id", "cli-account-change")
        self.assertEqual(changed["host_epoch"], repeated["host_epoch"])

    def test_cli_serve_uses_context_handle_environment_without_guessing_session(self):
        root = Path(__file__).resolve().parents[1]
        handle = self.bind("environment-session")
        self.bind("later-unrelated-session")
        environment = dict(os.environ, ALP_HOME=str(self.runtime.home), ALP_CONTEXT_HANDLE=handle,
                           PYTHONDONTWRITEBYTECODE="1")
        environment.pop("ALP_CONFIG", None)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "local-cli-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "task_update", "arguments": {"goal": "environment context"}}},
        ]
        process = subprocess.run([sys.executable, str(root / "scripts/learning.py"), "--workspace",
                                  str(self.workspace), "serve"], cwd=self.workspace, env=environment,
                                 input=("\n".join(json.dumps(message) for message in messages) + "\n").encode("utf-8"),
                                 capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        replies = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
        self.assertEqual(self.data(replies[-1])["conversation"], "environment-session")
        self.assertIsNone(self.runtime.task(self.project, "later-unrelated-session"))


if __name__ == "__main__":
    unittest.main()
