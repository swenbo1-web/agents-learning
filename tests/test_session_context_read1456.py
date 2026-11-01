"""Session recovery reads one SQLite snapshot without competing for the WAL writer."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_control import readonly_runtime
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime


class SessionContextRead1456Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-session-read1456-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.home = self.base / "home"
        self.r = Runtime(self.home, config={"enforce_task_policy": True,
            "admin": {"enabled": False}, "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.original_task = self.r.task_update(self.p, "chat", "Recover the original task", {"mode": "analysis"})
        self.parent = self.r.register_context(self.p, "chat")["context_handle"]
        self.child = self.r.register_context(self.p, "chat", "child-one")["context_handle"]
        self.assertEqual(self.r.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.r.db.execute("PRAGMA busy_timeout").fetchone()[0], 1500)

    @contextmanager
    def writer_lock(self):
        with closing(sqlite3.connect(self.home / "runtime.sqlite3", timeout=1.5, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                yield writer
            finally:
                if writer.in_transaction:
                    writer.execute("ROLLBACK")

    def writer(self):
        runtime = Runtime(self.home, config=self.r.config)
        self.addCleanup(runtime.close)
        return runtime

    def assert_read_only_call(self, call, runtime=None):
        runtime = runtime or self.r
        before, statements = runtime.db.total_changes, []
        runtime.db.set_trace_callback(statements.append)
        try:
            return call()
        finally:
            runtime.db.set_trace_callback(None)
            self.assertEqual(runtime.db.total_changes, before)
            controls = [" ".join(statement.upper().split()) for statement in statements]
            self.assertFalse(any(statement.startswith("BEGIN IMMEDIATE") for statement in controls), controls)

    def ready(self):
        server = Server(self.r, self.p, workspace=str(self.workspace))
        response = server.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "isolated-session-read-test", "version": "1"}}})
        self.assertNotIn("error", response)
        server.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    @staticmethod
    def tool(server, handle=None):
        arguments = {} if handle is None else {"context_handle": handle}
        return server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "session_context", "arguments": arguments}})

    def test_unbound_context_reads_under_writer_lock_without_a_write_or_guessed_identity(self):
        with self.writer_lock():
            began = time.monotonic()
            result = self.assert_read_only_call(lambda: self.r.session_context(self.p))
            self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(result, {"project_id": self.p, "host_epoch": self.r.session_epoch(),
            "context_bound": False, "rebind_required": True})
        self.assertFalse(self.r.db.in_transaction)
        self.assertEqual(self.r.db.execute("PRAGMA busy_timeout").fetchone()[0], 1500)

    def test_parent_context_reads_under_writer_lock_without_granting_execution(self):
        with self.writer_lock():
            began = time.monotonic()
            result = self.assert_read_only_call(lambda: self.r.session_context(self.p, self.parent))
            self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(result["context_handle"], self.parent)
        self.assertEqual(result["task"], self.original_task)
        self.assertIsNone(result["parent_task"])
        self.assertIsNone(result["assignment"])
        self.assertEqual(result["task_access"], "read_write")
        self.assertEqual(result["execution"], {"ready": False, "reason": "execution_approval_required", "policy": None})
        self.assertFalse(self.r.db.in_transaction)

    def test_child_context_reads_under_writer_lock_with_original_assignment(self):
        with self.writer_lock():
            began = time.monotonic()
            result = self.assert_read_only_call(lambda: self.r.session_context(self.p, self.child))
            self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(result["agent_id"], "child-one")
        self.assertEqual(result["task"], self.original_task)
        self.assertEqual(result["parent_task"], self.original_task)
        self.assertTrue(result["assignment"]["current"])
        self.assertEqual(result["assignment"]["task_id"], self.original_task["id"])
        self.assertEqual(result["task_access"], "parent_read_only")
        self.assertFalse(result["execution"]["ready"])
        self.assertFalse(self.r.db.in_transaction)

    def test_mcp_parent_child_and_unbound_context_reads_succeed_under_writer_lock(self):
        cases = [(self.ready(), handle) for handle in (None, self.parent, self.child)]
        with self.writer_lock():
            began = time.monotonic()
            for server, handle in cases:
                with self.subTest(handle_kind="unbound" if handle is None else "parent" if handle == self.parent else "child"):
                    response = self.assert_read_only_call(lambda: self.tool(server, handle))
                    self.assertNotIn("error", response)
                    self.assertFalse(response["result"]["isError"])
                    body = response["result"]["structuredContent"]
                    self.assertEqual(body["context_bound"], handle is not None)
                    self.assertEqual(server.context_handle, handle)
            self.assertLess(time.monotonic() - began, 1.0)
        self.assertFalse(self.r.db.in_transaction)

    def test_readonly_uri_connection_can_read_parent_child_and_unbound_context(self):
        database = self.home / "runtime.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                   timeout=1.5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            view = readonly_runtime(database, db, "codex", "default")
            with self.writer_lock():
                for handle in (None, self.parent, self.child):
                    result = self.assert_read_only_call(lambda: view.session_context(self.p, handle), view)
                    self.assertEqual(result["context_bound"], handle is not None)
                    self.assertFalse(db.in_transaction)
            self.assertEqual(db.total_changes, 0)
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_outer_read_transaction_keeps_ownership_after_success_and_rejection(self):
        self.r.db.execute("BEGIN")
        self.r.task(self.p, "chat")
        statements = []
        self.r.db.set_trace_callback(statements.append)
        try:
            result = self.r.session_context(self.p, self.parent)
            self.assertEqual(result["task"]["id"], self.original_task["id"])
            self.assertTrue(self.r.db.in_transaction)
            with self.assertRaisesRegex(ContractError, "context_mismatch"):
                self.r.session_context(self.p, "invalid-handle")
            self.assertTrue(self.r.db.in_transaction)
            controls = {" ".join(statement.upper().split()) for statement in statements}
            self.assertTrue(controls.isdisjoint({"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}), controls)
        finally:
            self.r.db.set_trace_callback(None)
            self.r.db.execute("ROLLBACK")

    def test_outer_write_transaction_retains_changes_when_context_read_fails(self):
        self.r.db.execute("CREATE TEMP TABLE session_read_probe(value INTEGER)")
        self.r.db.execute("BEGIN IMMEDIATE")
        self.r.db.execute("INSERT INTO session_read_probe VALUES(1)")
        before, statements = self.r.db.total_changes, []
        self.r.db.set_trace_callback(statements.append)
        try:
            self.r.session_context(self.p, self.parent)
            with patch.object(self.r, "execution_status", side_effect=RuntimeError("synthetic read failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic read failure"):
                    self.r.session_context(self.p, self.parent)
            self.assertTrue(self.r.db.in_transaction)
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM session_read_probe").fetchone()[0], 1)
            self.assertEqual(self.r.db.total_changes, before)
            controls = {" ".join(statement.upper().split()) for statement in statements}
            self.assertTrue(controls.isdisjoint({"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}), controls)
        finally:
            self.r.db.set_trace_callback(None)
            self.r.db.execute("ROLLBACK")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM session_read_probe").fetchone()[0], 0)

    def concurrent_task_change(self, boundary):
        writer, committed = self.writer(), []
        original = getattr(self.r, boundary)

        def advance():
            if committed:
                return
            changed = writer.task_update(self.p, "chat", "A concurrent replacement task", {"mode": "implementation"}, new_task=True)
            committed.append(changed)
            writer.approve_execution(self.p, "chat", changed["id"], changed["write_version"],
                {"mode": "read_only", "allowed_tools": ["Read"], "tool_rules": {"Read": {"action": "read"}}})

        def interleave(*args, **kwargs):
            if boundary == "execution_status":
                advance()
            value = original(*args, **kwargs)
            if boundary == "task":
                advance()
            return value

        with patch.object(self.r, boundary, side_effect=interleave):
            result = self.assert_read_only_call(lambda: self.r.session_context(self.p, self.parent))
        self.assertEqual(len(committed), 1)
        self.assertEqual(result["task"], self.original_task)
        self.assertEqual(result["execution"], {"ready": False, "reason": "execution_approval_required", "policy": None})
        self.assertEqual(self.r.task(self.p, "chat")["id"], committed[0]["id"])
        self.assertTrue(self.r.execution_status(self.p, "chat")["ready"])
        self.assertFalse(self.r.db.in_transaction)

    def test_task_change_committed_after_parent_task_read_does_not_mix_execution_snapshot(self):
        self.concurrent_task_change("task")

    def test_task_change_committed_before_execution_read_does_not_mix_task_snapshot(self):
        self.concurrent_task_change("execution_status")

    def test_child_keeps_dispatched_task_after_parent_replacement_without_gaining_permission(self):
        newer = self.r.task_update(self.p, "chat", "The current parent task", {"mode": "implementation"}, new_task=True)
        result = self.assert_read_only_call(lambda: self.r.session_context(self.p, self.child))
        self.assertEqual(result["task"], self.original_task)
        self.assertEqual(result["parent_task"], newer)
        self.assertFalse(result["assignment"]["current"])
        self.assertEqual(result["assignment"]["task_write_version"], self.original_task["write_version"])
        self.assertEqual(result["task_access"], "parent_read_only")
        self.assertFalse(result["execution"]["ready"])
        self.assertIsNone(result["execution"]["policy"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM execution_policies").fetchone()[0], 0)

    def test_account_change_after_task_read_is_fenced_without_an_expected_epoch_argument(self):
        writer, changed = self.writer(), []
        original = self.r.task

        def switch_after_task(*args, **kwargs):
            task = original(*args, **kwargs)
            if not changed:
                changed.append(writer.account_changed("isolated-mid-task-read"))
            return task

        before = self.r.db.total_changes
        with patch.object(self.r, "task", side_effect=switch_after_task):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.r.session_context(self.p, self.parent)
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertFalse(self.r.db.in_transaction)

    def test_account_change_after_execution_read_rejects_mcp_response_without_binding(self):
        writer, server = self.writer(), self.ready()
        original = self.r.execution_status

        def switch_after_execution(*args, **kwargs):
            result = original(*args, **kwargs)
            writer.account_changed("isolated-mid-execution-read")
            return result

        with patch.object(self.r, "execution_status", side_effect=switch_after_execution):
            response = self.tool(server, self.parent)
        self.assertEqual(response["error"]["message"], "reconnect_required")
        self.assertNotIn("result", response)
        self.assertIsNone(server.context_handle)
        self.assertFalse(self.r.db.in_transaction)

    def test_unbound_result_is_fenced_when_account_changes_after_its_epoch_read(self):
        writer, changed = self.writer(), []
        original = self.r.assert_session_epoch

        def switch_after_epoch(*args, **kwargs):
            epoch = original(*args, **kwargs)
            if not changed:
                changed.append(writer.account_changed("isolated-unbound-read"))
            return epoch

        with patch.object(self.r, "assert_session_epoch", side_effect=switch_after_epoch):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.r.session_context(self.p)
        self.assertEqual(len(changed), 1)
        self.assertFalse(self.r.db.in_transaction)

    def test_other_host_account_change_does_not_invalidate_current_host_read_or_binding(self):
        server = self.ready()
        original = self.r.execution_status
        scope_epoch, governance_epoch = self.r.session_epoch(), self.r.host_epoch()
        with closing(Runtime(self.home, config=self.r.config, host="antigravity")) as other_host:
            def switch_other_host_after_execution(*args, **kwargs):
                result = original(*args, **kwargs)
                other_host.scope_account_changed("isolated-other-host-midread")
                return result

            with patch.object(self.r, "execution_status", side_effect=switch_other_host_after_execution):
                response = self.assert_read_only_call(lambda: self.tool(server, self.parent))
        self.assertNotIn("error", response)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["host_epoch"], scope_epoch)
        self.assertEqual(response["result"]["structuredContent"]["task"], self.original_task)
        self.assertEqual(server.context_handle, self.parent)
        self.assertEqual(self.r.session_epoch(), scope_epoch)
        self.assertGreater(self.r.host_epoch(), governance_epoch)
        self.assertFalse(self.r.db.in_transaction)

    def test_invalid_cross_project_cross_host_and_expired_handles_keep_identity_protection(self):
        other_workspace = self.base / "other-workspace"
        other_workspace.mkdir()
        other = self.r.project(other_workspace)
        with closing(Runtime(self.home, config=self.r.config, host="dsh")) as dsh:
            foreign = dsh.register_context(self.p, dsh.host_conversation("foreign-chat"))["context_handle"]
        for project, handle, reason in ((self.p, "unknown-handle", "context_mismatch"),
                                        (self.p, "", "session_context_required"),
                                        (other, self.parent, "context_mismatch"),
                                        (self.p, foreign, "context belongs to another host")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ContractError, reason):
                self.assert_read_only_call(lambda: self.r.session_context(project, handle))
            self.assertFalse(self.r.db.in_transaction)
        old_epoch = self.r.session_epoch()
        self.r.account_changed("isolated-expired-session-read")
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.assert_read_only_call(lambda: self.r.session_context(self.p, self.parent))
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.assert_read_only_call(lambda: self.r.session_context(self.p, expected_epoch=old_epoch))

    def test_owned_read_failure_closes_snapshot_without_writing_or_granting_execution(self):
        before = self.r.db.total_changes
        with patch.object(self.r, "execution_status", side_effect=sqlite3.OperationalError("interrupted")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                self.r.session_context(self.p, self.parent)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertFalse(self.r.db.in_transaction)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM execution_policies").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
