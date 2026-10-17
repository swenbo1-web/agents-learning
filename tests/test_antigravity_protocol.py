"""真实临时数据库中的共享MCP、瞬时投递与宿主边界保护。"""
from pathlib import Path
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server, ProtocolError
from agent_learning.runtime import Runtime, ContractError


class AntigravityProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-ag-protocol-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.ag = Runtime(base / "home", config={}, host="antigravity")
        self.codex = Runtime(base / "home", config={})
        self.addCleanup(self.ag.close)
        self.addCleanup(self.codex.close)
        self.project = self.ag.project(self.workspace)

    def handle(self, native):
        conversation = self.ag.host_conversation(native)
        return self.ag.register_context(self.project, conversation)["context_handle"]

    def published(self):
        self.codex.task_update(self.project, "source", "compiler recovery")
        problem = self.codex.feedback(self.project, "source", "compiler recovery", category="operation")
        packet = self.codex.review_packet(self.project, problem["id"])
        candidate = self.codex.submit_candidate(
            self.project, problem_id=problem["id"], problem_revision=packet["problem"]["revision"],
            title="compiler recovery", content="compiler recovery requires checking the selected toolchain",
            category="operation", applicability="The same compiler environment", limitations="Verify locally",
            evidence_refs=[packet["events"][0]["id"]], validation_level="reported")
        return self.codex.publish(self.project, candidate["candidate_id"])

    def test_shared_mcp_routes_each_explicit_handle_without_inheriting_previous(self):
        server = Server(self.ag, self.project)
        first, second = self.handle("one"), self.handle("two")
        one = server.call("task_update", {"context_handle": first, "goal": "first task"})
        two = server.call("task_update", {"context_handle": second, "goal": "second task"})
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(server.call("session_context", {"context_handle": first})["task"]["goal"], "first task")
        self.assertEqual(server.call("session_context", {"context_handle": second})["task"]["goal"], "second task")
        self.assertFalse(server.call("session_context", {})["context_bound"])
        with self.assertRaisesRegex(ContractError, "session_context_required"):
            server.call("task_update", {"goal": "must not inherit"})
        self.assertIsNone(server.context_handle)

    def test_codex_switch_preserves_antigravity_worker_but_fences_governance(self):
        handle = self.handle("worker")
        worker = Server(self.ag, self.project)
        admin = Server(self.ag, self.project, "admin")
        worker.call("task_update", {"context_handle": handle, "goal": "ongoing"})
        self.codex.account_changed("codex-change")
        self.assertEqual(worker.call("session_context", {"context_handle": handle})["task"]["goal"], "ongoing")
        with self.assertRaisesRegex(ProtocolError, "reconnect_required"):
            admin.call("problem_list", {})

    def test_foreign_host_handle_rejected_before_knowledge_read(self):
        item = self.published()
        handle = self.codex.register_context(self.project, "reader")["context_handle"]
        with self.assertRaisesRegex(ContractError, "another host"):
            Server(self.ag, self.project).call("knowledge_read", {
                "context_handle": handle, "knowledge_id": item["knowledge_id"]})

    def test_ephemeral_cards_redeliver_without_retrieval_or_new_hit_count(self):
        item = self.published()
        handle = self.handle("reader")
        conversation = self.ag.host_conversation("reader")
        self.ag.task_update(self.project, conversation, "compiler recovery", context_handle=handle)
        first = self.ag._context(self.project, conversation, context_handle=handle, delivery="ephemeral")
        before = self.ag.status(self.project)["metrics"]
        second = self.ag._context(self.project, conversation, context_handle=handle, delivery="ephemeral")
        after = self.ag.status(self.project)["metrics"]
        self.assertIn(item["knowledge_id"], first)
        self.assertIn(item["knowledge_id"], second)
        self.assertLessEqual(len(second), 2400)
        self.assertEqual(after.get("search_queries"), before.get("search_queries"))
        self.assertEqual(after.get("knowledge_injected"), before.get("knowledge_injected"))
        self.assertEqual(after["ephemeral_cards_delivered"], before["ephemeral_cards_delivered"] + 1)
        shown = json.loads(self.ag.db.execute("SELECT shown FROM contexts WHERE conversation=?", (conversation,)).fetchone()[0])
        self.assertEqual(shown, [item["knowledge_id"] + "@1"])

    def test_revocation_replaces_ephemeral_card_with_notification(self):
        item = self.published()
        handle = self.handle("reader")
        conversation = self.ag.host_conversation("reader")
        self.ag.task_update(self.project, conversation, "compiler recovery", context_handle=handle)
        self.ag._context(self.project, conversation, context_handle=handle, delivery="ephemeral")
        self.codex.revoke(self.project, item["knowledge_id"], expected_revision=1, reason="invalid")
        text = self.ag._context(self.project, conversation, context_handle=handle, delivery="ephemeral")
        self.assertIn(item["knowledge_id"], text)
        self.assertNotIn('"content_summary"', text)
        self.assertNotIn(item["knowledge_id"] + "@1", json.loads(self.ag.db.execute(
            "SELECT shown FROM contexts WHERE conversation=?", (conversation,)).fetchone()[0]))

    def test_trusted_reset_cli_uses_native_identity_and_invalidates_old_handle(self):
        handle = self.handle("reset-chat")
        conversation = self.ag.host_conversation("reset-chat")
        self.ag.task_update(self.project, conversation, "ongoing", context_handle=handle)
        configuration = Path(self.temp.name) / "isolated-config.json"
        configuration.write_text("{}", encoding="utf-8")
        env = dict(os.environ, ALP_CONFIG=str(configuration), ALP_HOME=str(self.ag.home),
                   PYTHONDONTWRITEBYTECODE="1")
        for name in ("ALP_CONTEXT_HANDLE", "ALP_HOOK_MODE", "ALP_DISABLED"):
            env.pop(name, None)
        completed = subprocess.run([
            sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "scripts/learning.py"),
            "--workspace", str(self.workspace), "--home", str(self.ag.home),
            "antigravity-session-reset", "--conversation", "reset-chat"],
            env=env, capture_output=True, timeout=20, check=True)
        output = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(output["conversation_id"], conversation)
        self.assertEqual(output["generation"], 2)
        self.assertTrue(output["rebind_required"])
        self.assertEqual(self.ag.task(self.project, conversation)["status"], "paused")
        with self.assertRaises(ContractError):
            self.ag.access_context(self.project, handle)

    def test_native_stdio_ignores_inherited_handle_and_requires_each_request_identity(self):
        current, ambient = self.handle("current"), self.handle("ambient")
        configuration = Path(self.temp.name) / "isolated-config.json"
        configuration.write_text("{}", encoding="utf-8")
        env = dict(os.environ, ALP_CONFIG=str(configuration), ALP_HOME=str(self.ag.home),
                   ALP_CONTEXT_HANDLE=ambient, PYTHONDONTWRITEBYTECODE="1")
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "offline", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "session_context", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "task_update", "arguments": {
                "context_handle": current, "goal": "correct native task"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "task_update", "arguments": {
                "goal": "must not write ambient task"}}}]
        completed = subprocess.run([
            sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "scripts/learning.py"),
            "--workspace", str(self.workspace), "--home", str(self.ag.home), "serve", "--host", "antigravity"],
            input=("\n".join(json.dumps(item) for item in messages) + "\n").encode("utf-8"),
            env=env, capture_output=True, timeout=20, check=True)
        output = {item["id"]: item["result"] for item in (json.loads(line) for line in completed.stdout.splitlines())}
        self.assertFalse(output[2]["structuredContent"]["context_bound"])
        self.assertFalse(output[3]["isError"])
        self.assertTrue(output[4]["isError"])
        self.assertEqual(self.ag.task(self.project, self.ag.host_conversation("current"))["goal"], "correct native task")
        self.assertIsNone(self.ag.task(self.project, self.ag.host_conversation("ambient")))

    def test_schema11_upgrade_preserves_shared_knowledge_and_requires_handle_rebind(self):
        item = self.published()
        handle = self.codex.register_context(self.project, "legacy-reader")["context_handle"]
        self.ag.close()
        self.codex.close()
        with closing(sqlite3.connect(self.ag.home / "runtime.sqlite3", isolation_level=None)) as db:
            for table in ("host_scopes", "host_scope_notifications", "antigravity_trajectories",
                          "antigravity_steps", "antigravity_receipts", "antigravity_faults"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=11")
        upgraded = Runtime(self.ag.home, config={})
        try:
            self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(upgraded.read(self.project, item["knowledge_id"])["revision"], 1)
            with self.assertRaises(ContractError):
                upgraded.access_context(self.project, handle)
            rebound = upgraded.register_context(self.project, "legacy-reader")["context_handle"]
            self.assertEqual(upgraded.access_context(self.project, rebound)["conversation_id"], "legacy-reader")
        finally:
            upgraded.close()

    def test_schema12_migration_rolls_back_partial_native_storage(self):
        self.ag.close()
        self.codex.close()
        with closing(sqlite3.connect(self.ag.home / "runtime.sqlite3", isolation_level=None)) as db:
            for table in ("host_scopes", "host_scope_notifications", "antigravity_trajectories",
                          "antigravity_steps", "antigravity_receipts", "antigravity_faults"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=11")
        def fail_after_ddl(runtime):
            runtime.db.execute("CREATE TABLE partial_native_migration(id TEXT)")
            raise ContractError("controlled migration failure")
        with patch("agent_learning.antigravity.install_storage", side_effect=fail_after_ddl):
            with self.assertRaisesRegex(ContractError, "controlled migration failure"):
                Runtime(self.ag.home, config={})
        with closing(sqlite3.connect(self.ag.home / "runtime.sqlite3", isolation_level=None)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partial_native_migration'").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='host_scopes'").fetchone())


if __name__ == "__main__":
    unittest.main()
