"""Knowledge reads retain atomic exposure when another isolated writer holds SQLite."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, encode
from agent_learning.sqlite_policy import busy_timeout, is_busy_error
from tests import test_module4_mcp as fixture


def sqlite_error(message, code=None):
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


class SQLiteBusyPolicyTests(unittest.TestCase):
    def test_primary_and_extended_busy_codes_are_recognized_without_message_details(self):
        for code in (5, 6, 261, 262, 517, 773):
            with self.subTest(code=code):
                self.assertTrue(is_busy_error(sqlite_error("PRIVATE SQL and local path", code)))

    def test_python310_standard_lock_messages_are_recognized_without_code(self):
        for message in ("database is locked", "database table is locked",
                        "database table is locked: PRIVATE_TABLE",
                        "database schema is locked: main", "DATABASE IS LOCKED"):
            with self.subTest(message=message):
                self.assertTrue(is_busy_error(sqlite_error(message)))

    def test_nonbusy_codes_take_priority_over_a_lock_like_message(self):
        for code in (1, 8, 9, 11, 14, 19):
            with self.subTest(code=code):
                self.assertFalse(is_busy_error(sqlite_error("database is locked", code)))

    def test_unrelated_errors_and_non_sqlite_exceptions_are_not_busy(self):
        for error in (sqlite_error("interrupted"), sqlite_error("attempt to write a readonly database"),
                      sqlite_error("no such table: locked"), sqlite_error("PRIVATE error includes database is locked"),
                      RuntimeError("database is locked"), ValueError("database is locked"),
                      sqlite3.IntegrityError("database is locked")):
            with self.subTest(error=repr(error)):
                self.assertFalse(is_busy_error(error))

    def test_timeout_scope_restores_previous_value_after_success_and_failure(self):
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
            db.execute("PRAGMA busy_timeout=1377")
            with busy_timeout(db, 0):
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 1377)
            with self.assertRaisesRegex(RuntimeError, "scope failure"):
                with busy_timeout(db, 25):
                    self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 25)
                    raise RuntimeError("scope failure")
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 1377)

    def test_nested_timeout_scopes_restore_each_parent_value(self):
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
            db.execute("PRAGMA busy_timeout=1377")
            with busy_timeout(db, 25):
                with busy_timeout(db, 0):
                    self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 0)
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 25)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 1377)

    def test_timeout_scope_does_not_commit_or_replace_existing_transaction(self):
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
            db.execute("CREATE TABLE observations(value INTEGER)")
            db.execute("BEGIN IMMEDIATE")
            with busy_timeout(db, 0):
                db.execute("INSERT INTO observations VALUES(1)")
                self.assertTrue(db.in_transaction)
            self.assertTrue(db.in_transaction)
            db.execute("ROLLBACK")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)


class KnowledgeBusy1455Tests(unittest.TestCase):
    setUp = fixture.Module4MCPTests.setUp
    published = fixture.Module4MCPTests.published
    bind = fixture.Module4MCPTests.bind
    shown = fixture.Module4MCPTests.shown

    @contextmanager
    def writer_lock(self):
        writer = sqlite3.connect(self.runtime.home / "runtime.sqlite3", timeout=1.5, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            yield writer
        finally:
            if writer.in_transaction:
                writer.execute("ROLLBACK")
            writer.close()

    def ready(self, version="2025-11-25"):
        server = Server(self.runtime, self.project, workspace=str(self.workspace))
        response = server.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "isolated-busy-policy-test", "version": "1"}}})
        self.assertNotIn("error", response)
        server.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    @staticmethod
    def tool(server, operation, arguments):
        return server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": operation, "arguments": arguments}})

    def timeout(self):
        return self.runtime.db.execute("PRAGMA busy_timeout").fetchone()[0]

    def exposure_snapshot(self):
        result = {}
        for table in ("contexts", "context_exposures", "learning_exposures"):
            result[table] = [tuple(row) for row in self.runtime.db.execute(
                "SELECT * FROM " + table + " WHERE project=? AND conversation=? ORDER BY rowid",
                (self.project, "reader"))]
        return result

    def assert_busy_result(self, response, operation, version="2025-11-25"):
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result["isError"], response)
        expected = {"status": "error", "error": "database_busy", "retryable": True,
            "operation": operation,
            "message": "Local knowledge storage is busy. Retry this read after the current write finishes."}
        self.assertEqual(result["content"], [{"type": "text", "text": encode(expected)}])
        if version in {"2025-06-18", "2025-11-25"}:
            self.assertEqual(result["structuredContent"], expected)
        else:
            self.assertNotIn("structuredContent", result)
        self.assertNotIn("items", json.loads(result["content"][0]["text"]))
        self.assertNotIn("PRIVATE", encode(response))
        self.assertNotIn(str(self.runtime.home), encode(response))

    def test_cache_lock_returns_completed_search_without_waiting_for_default_timeout(self):
        published = self.published()
        self.assertEqual(self.timeout(), 1500)
        before_metrics = [tuple(row) for row in self.runtime.db.execute("SELECT * FROM metrics ORDER BY rowid")]
        before_cache = [tuple(row) for row in self.runtime.db.execute("SELECT * FROM cache ORDER BY rowid")]
        statements = []
        self.runtime.db.set_trace_callback(statements.append)
        try:
            with self.writer_lock():
                began = time.monotonic()
                found = self.runtime.search(self.project, "compiler recovery")
                duration = time.monotonic() - began
                self.assertLess(duration, 1.0, "optional cache/metrics must not wait for the 1.5s write timeout")
        finally:
            self.runtime.db.set_trace_callback(None)
        self.assertEqual(found["items"][0]["knowledge_id"], published["knowledge_id"])
        self.assertIn("PRAGMA busy_timeout=0", [statement.replace(" ", "").replace("PRAGMA", "PRAGMA ")
            for statement in statements])
        self.assertEqual(self.timeout(), 1500)
        self.assertEqual([tuple(row) for row in self.runtime.db.execute("SELECT * FROM metrics ORDER BY rowid")], before_metrics)
        self.assertEqual([tuple(row) for row in self.runtime.db.execute("SELECT * FROM cache ORDER BY rowid")], before_cache)
        self.assertFalse(self.runtime.db.in_transaction)

    def test_unbound_read_returns_knowledge_while_another_writer_holds_lock(self):
        published = self.published()
        self.bind()
        server = self.ready()
        with self.writer_lock():
            began = time.monotonic()
            response = self.tool(server, "knowledge_search", {"query": "compiler recovery"})
            self.assertLess(time.monotonic() - began, 1.0)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["items"][0]["knowledge_id"], published["knowledge_id"])
        self.assertIsNone(server.context_handle)
        self.assertEqual(self.shown(), [])
        self.assertEqual(self.timeout(), 1500)

    def test_bound_search_waits_once_then_returns_busy_without_binding_or_partial_exposure(self):
        published = self.published()
        handle = self.bind()
        server = self.ready()
        before = self.exposure_snapshot()
        arguments = {"query": "compiler recovery", "context_handle": handle, "limit": 5, "max_chars": 6500}
        with self.writer_lock(), \
                patch.object(self.runtime, "search", wraps=self.runtime.search) as search, \
                patch.object(self.runtime, "record_exposure", wraps=self.runtime.record_exposure) as record:
            began = time.monotonic()
            response = self.tool(server, "knowledge_search", arguments)
            self.assertLess(time.monotonic() - began, 2.5, "the cache must not add a second 1.5s wait")
            self.assertEqual(search.call_count, 1)
            self.assertEqual(record.call_count, 1)
            self.assert_busy_result(response, "knowledge_search")
            self.assertIsNone(server.context_handle)
            self.assertEqual(self.exposure_snapshot(), before)
            self.assertEqual(self.timeout(), 1500)
            self.assertFalse(self.runtime.db.in_transaction)
        retry = self.tool(server, "knowledge_search", arguments)
        self.assertFalse(retry["result"]["isError"])
        self.assertEqual(server.context_handle, handle)
        self.assertIn(f"{published['knowledge_id']}@{published['revision']}", self.shown())

    def test_bound_knowledge_read_waits_once_and_returns_busy_without_exposure(self):
        published = self.published()
        handle = self.bind()
        server = self.ready()
        before = self.exposure_snapshot()
        with self.writer_lock(), patch.object(self.runtime, "read", wraps=self.runtime.read) as read:
            began = time.monotonic()
            response = self.tool(server, "knowledge_read", {"knowledge_id": published["knowledge_id"], "context_handle": handle})
            self.assertLess(time.monotonic() - began, 2.5)
            self.assertEqual(read.call_count, 1)
        self.assert_busy_result(response, "knowledge_read")
        self.assertEqual(self.exposure_snapshot(), before)
        self.assertIsNone(server.context_handle)
        self.assertEqual(self.timeout(), 1500)

    def test_empty_bound_search_validates_handle_again_without_writing_under_lock(self):
        handle = self.bind()
        server = self.ready()
        before = self.exposure_snapshot()
        with self.writer_lock(), patch.object(self.runtime, "access_context", wraps=self.runtime.access_context) as access:
            began = time.monotonic()
            response = self.tool(server, "knowledge_search", {"query": "missing_identifier_1455", "context_handle": handle})
            self.assertLess(time.monotonic() - began, 1.0)
            self.assertGreaterEqual(access.call_count, 2)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["items"], [])
        self.assertEqual(self.exposure_snapshot(), before)
        self.assertEqual(server.context_handle, handle)
        self.assertEqual(self.timeout(), 1500)

    def test_empty_exposure_rejects_unknown_handle_even_when_no_writer_is_needed(self):
        before = self.exposure_snapshot()
        with self.assertRaisesRegex(ContractError, "context_mismatch"):
            self.runtime.record_exposure(self.project, "unknown-context", [], expected_epoch=self.runtime.session_epoch())
        self.assertEqual(self.exposure_snapshot(), before)
        self.assertFalse(self.runtime.db.in_transaction)

    def test_invalid_handle_is_not_degraded_to_busy_and_does_not_run_search(self):
        server = self.ready()
        with self.writer_lock(), patch.object(self.runtime, "search", side_effect=AssertionError("invalid handle reached search")):
            response = self.tool(server, "knowledge_search", {"query": "missing_identifier_1455", "context_handle": "unknown-context"})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("context_mismatch", response["result"]["content"][0]["text"])
        self.assertNotIn("database_busy", encode(response))
        self.assertIsNone(server.context_handle)

    def test_empty_search_rechecks_epoch_after_retrieval_before_binding(self):
        handle = self.bind()
        server = self.ready()
        original = self.runtime.search

        def change_after_search(*args, **kwargs):
            result = original(*args, **kwargs)
            self.runtime.account_changed(notification_id="isolated-empty-read-epoch-change")
            return result

        with patch.object(self.runtime, "search", side_effect=change_after_search):
            response = self.tool(server, "knowledge_search", {"query": "missing_identifier_1455", "context_handle": handle})
        self.assertEqual(response["error"]["message"], "reconnect_required")
        self.assertNotIn("database_busy", encode(response))
        self.assertIsNone(server.context_handle)
        self.assertEqual(self.shown(), [])

    def test_busy_after_exposure_inserts_rolls_back_all_receipts_and_does_not_bind(self):
        self.published()
        self.runtime.task_update(self.project, "reader", "compiler recovery")
        handle = self.bind()
        server = self.ready()
        before = self.exposure_snapshot()
        original = self.runtime.record_task_exposure

        def fail_after_receipts(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite_error("PRIVATE SQL /private/database.sqlite3", 5)

        with patch.object(self.runtime, "record_task_exposure", side_effect=fail_after_receipts) as record:
            response = self.tool(server, "knowledge_search", {"query": "compiler recovery", "context_handle": handle})
        self.assertEqual(record.call_count, 1)
        self.assert_busy_result(response, "knowledge_search")
        self.assertEqual(self.exposure_snapshot(), before)
        self.assertIsNone(server.context_handle)
        self.assertFalse(self.runtime.db.in_transaction)

    def test_invalid_exposed_revision_remains_contract_error_without_partial_receipts(self):
        published = self.published()
        handle = self.bind()
        item = self.runtime.read(self.project, published["knowledge_id"])
        before = self.exposure_snapshot()
        with self.assertRaisesRegex(ContractError, "unknown exposed knowledge revision"):
            self.runtime.record_exposure(self.project, handle, [item, dict(item, revision=999)],
                expected_epoch=self.runtime.session_epoch())
        self.assertEqual(self.exposure_snapshot(), before)
        self.assertFalse(self.runtime.db.in_transaction)

    def test_busy_protocol_payload_preserves_text_and_structured_version_contracts(self):
        for version in Server.SUPPORTED_VERSIONS:
            for operation, method, arguments in (("knowledge_search", "search", {"query": "compiler"}),
                                                ("knowledge_read", "read", {"knowledge_id": "synthetic"})):
                with self.subTest(version=version, operation=operation):
                    server = self.ready(version)
                    with patch.object(self.runtime, method, side_effect=sqlite_error("PRIVATE SQL /private/path", 6)) as call:
                        response = self.tool(server, operation, arguments)
                    self.assertEqual(call.call_count, 1)
                    self.assert_busy_result(response, operation, version)
                    self.assertIsNone(server.context_handle)

    def test_nonbusy_sqlite_errors_remain_internal_errors_for_both_knowledge_tools(self):
        for code, message in ((8, "attempt to write a readonly database"), (9, "interrupted"),
                              (1, "PRIVATE SQL /private/path")):
            for operation, method, arguments in (("knowledge_search", "search", {"query": "compiler"}),
                                                ("knowledge_read", "read", {"knowledge_id": "synthetic"})):
                with self.subTest(code=code, operation=operation):
                    server = self.ready()
                    with patch.object(self.runtime, method, side_effect=sqlite_error(message, code)):
                        response = self.tool(server, operation, arguments)
                    self.assertEqual(response["error"], {"code": -32603, "message": "OperationalError"})
                    self.assertNotIn("PRIVATE", encode(response))
                    self.assertNotIn("database_busy", encode(response))

    def test_busy_from_other_tool_is_not_newly_classified_as_retryable(self):
        handle = self.bind()
        server = self.ready()
        with patch.object(self.runtime, "task_update", side_effect=sqlite_error("PRIVATE SQL /private/path", 5)) as call:
            response = self.tool(server, "task_update", {"goal": "isolated task", "context_handle": handle})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(response["error"], {"code": -32603, "message": "OperationalError"})
        self.assertNotIn("database_busy", encode(response))

    def test_read_revision_mismatch_is_not_classified_as_busy(self):
        published = self.published()
        handle = self.bind()
        server = self.ready()
        response = self.tool(server, "knowledge_read", {"knowledge_id": published["knowledge_id"],
            "expected_revision": 999, "context_handle": handle})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("knowledge revision changed", response["result"]["content"][0]["text"])
        self.assertNotIn("database_busy", encode(response))
        self.assertEqual(self.shown(), [])
        self.assertIsNone(server.context_handle)

    def test_nonbusy_cache_fault_is_not_swallowed_and_timeout_is_restored(self):
        self.published()
        with patch.object(self.runtime, "_store_search_cache", side_effect=sqlite_error("interrupted", 9)):
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                self.runtime.search(self.project, "compiler recovery")
        self.assertEqual(self.timeout(), 1500)
        self.assertFalse(self.runtime.db.in_transaction)

    def test_nested_search_retains_outer_timeout_transaction_and_metric_rollback(self):
        self.published()
        self.runtime.search(self.project, "compiler recovery")
        before = self.runtime.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'",
            (self.project,)).fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "rollback surrounding task"):
            with self.runtime.transaction():
                result = self.runtime.search(self.project, "compiler recovery")
                self.assertTrue(result["items"])
                self.assertTrue(self.runtime.db.in_transaction)
                self.assertEqual(self.timeout(), 1500)
                during = self.runtime.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'",
                    (self.project,)).fetchone()[0]
                self.assertEqual(during, before + 1)
                raise RuntimeError("rollback surrounding task")
        after = self.runtime.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'",
            (self.project,)).fetchone()[0]
        self.assertEqual(after, before)
        self.assertFalse(self.runtime.db.in_transaction)


if __name__ == "__main__":
    unittest.main()
