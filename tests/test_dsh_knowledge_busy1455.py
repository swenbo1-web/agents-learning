"""DSH's direct Python read adapter shares MCP's bounded storage-busy error body."""
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from agent_learning.runtime import ContractError, Runtime, encode
from agent_learning.sqlite_policy import knowledge_busy_response


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "hooks/dsh_tool.py"


def busy_error(code=5, message="PRIVATE SQL /private/database.sqlite3"):
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


class DSHKnowledgeBusy1455Tests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("dsh_knowledge_busy1455_entry", ENTRY)
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)

    def run_main(self, payload, error=None, startup_error=None):
        runtime, server = MagicMock(), MagicMock()
        runtime.project.return_value = "synthetic-project"
        server.tools = {name: ("synthetic", {"properties": {"context_handle": {}}})
            for name in ("knowledge_search", "knowledge_read", "task_update")}
        server.call.return_value = {"items": [], "status": "no_match"}
        if error is not None:
            server.call.side_effect = error
        output = io.StringIO()
        with patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
                patch.object(sys, "stdout", output), \
                patch("agent_learning.runtime.Runtime", return_value=runtime, side_effect=startup_error) as constructor, \
                patch("agent_learning.mcp.Server", return_value=server):
            self.assertEqual(self.entry.main(), 0)
        return json.loads(output.getvalue()), runtime, server, constructor

    @staticmethod
    def payload(operation):
        arguments = {"context_handle": "synthetic-handle"}
        arguments.update({"query": "synthetic"} if operation == "knowledge_search" else
            {"knowledge_id": "synthetic"} if operation == "knowledge_read" else {"goal": "synthetic"})
        return {"workspace": str(ROOT), "tool": operation, "args": arguments}

    def assert_busy(self, result, operation, error):
        expected = knowledge_busy_response(operation, error)
        self.assertEqual(result, {"ok": False, "error": encode(expected)})
        self.assertEqual(json.loads(result["error"]), expected)
        self.assertNotIn("items", expected)
        self.assertNotIn("PRIVATE", encode(result))
        self.assertNotIn("/private/", encode(result))

    def test_direct_read_busy_errors_share_exact_public_body_and_do_not_retry(self):
        for operation in ("knowledge_search", "knowledge_read"):
            for error in (busy_error(5), busy_error(6), busy_error(517),
                          busy_error(None, "database table is locked: PRIVATE_TABLE")):
                with self.subTest(operation=operation, error=repr(error)):
                    result, runtime, server, constructor = self.run_main(self.payload(operation), error=error)
                    self.assert_busy(result, operation, error)
                    constructor.assert_called_once()
                    server.call.assert_called_once_with(operation, self.payload(operation)["args"])
                    runtime.close.assert_called_once()

    def test_identity_epoch_and_revision_errors_keep_existing_rejection(self):
        for message in ("context_mismatch: unknown context handle", "reconnect_required: account context changed",
                        "knowledge revision changed; restart reading from offset 0"):
            for operation in ("knowledge_search", "knowledge_read"):
                with self.subTest(operation=operation, message=message):
                    result, runtime, server, _ = self.run_main(self.payload(operation), error=ContractError(message))
                    self.assertEqual(result, {"ok": False, "error": message})
                    self.assertEqual(server.call.call_count, 1)
                    runtime.close.assert_called_once()

    def test_nonbusy_read_errors_keep_existing_error_contract(self):
        for code, message in ((8, "attempt to write a readonly database"), (9, "interrupted"),
                              (1, "ordinary SQLite failure")):
            for operation in ("knowledge_search", "knowledge_read"):
                with self.subTest(operation=operation, code=code):
                    result, _, server, _ = self.run_main(self.payload(operation), error=busy_error(code, message))
                    self.assertEqual(result, {"ok": False, "error": message})
                    self.assertEqual(server.call.call_count, 1)

    def test_other_tool_busy_does_not_gain_retryable_classification(self):
        error = busy_error()
        result, runtime, server, _ = self.run_main(self.payload("task_update"), error=error)
        self.assertEqual(result, {"ok": False, "error": str(error)})
        self.assertNotIn("database_busy", result["error"])
        self.assertEqual(server.call.call_count, 1)
        runtime.close.assert_called_once()

    def test_read_startup_contention_is_classified_before_runtime_is_constructed(self):
        for operation in ("knowledge_search", "knowledge_read"):
            with self.subTest(operation=operation):
                error = busy_error()
                result, runtime, server, constructor = self.run_main(self.payload(operation), startup_error=error)
                self.assert_busy(result, operation, error)
                constructor.assert_called_once()
                runtime.close.assert_not_called()
                server.call.assert_not_called()

    def test_startup_error_with_absent_or_invalid_tool_has_no_unbound_local_failure(self):
        for tool in (None, {}, [], "task_update"):
            with self.subTest(tool=tool):
                payload = {"workspace": str(ROOT), "tool": tool, "args": {}}
                error = busy_error()
                result, _, server, _ = self.run_main(payload, startup_error=error)
                self.assertEqual(result, {"ok": False, "error": str(error)})
                server.call.assert_not_called()

    def test_successful_direct_read_keeps_existing_success_envelope(self):
        result, runtime, server, _ = self.run_main(self.payload("knowledge_search"))
        self.assertEqual(result, {"ok": True, "result": {"items": [], "status": "no_match"}})
        self.assertEqual(server.call.call_count, 1)
        runtime.close.assert_called_once()

    def test_release_verification_failure_still_stops_before_runtime_creation(self):
        payload = self.payload("knowledge_search")
        output = io.StringIO()
        with patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
                patch.object(sys, "stdout", output), \
                patch("agent_learning.release.validate_entry", side_effect=ValueError("synthetic manifest failure")), \
                patch("agent_learning.runtime.Runtime") as constructor:
            self.assertEqual(self.entry.main(), 0)
        self.assertEqual(json.loads(output.getvalue()),
            {"ok": False, "error": "release verification failed: synthetic manifest failure"})
        constructor.assert_not_called()

    def test_real_isolated_entrypoint_reports_writer_lock_and_succeeds_after_release(self):
        with tempfile.TemporaryDirectory(prefix="alp-dsh-knowledge-busy1455-") as directory:
            base = Path(directory)
            for name in ("workspace", "home", "tmp", "runtime-releases", "codex-home"):
                (base / name).mkdir()
            home, workspace = base / "home", base / "workspace"
            config = home / "config.json"
            config.write_text("{}", encoding="utf-8")
            env = dict(os.environ, ALP_HOME=str(home), ALP_CONFIG=str(config),
                ALP_RUNTIME_STORE=str(base / "runtime-releases"), CODEX_HOME=str(base / "codex-home"),
                PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", ALP_DISABLED="1")
            env.update({key: str(base / "tmp") for key in ("TMP", "TEMP", "TMPDIR")})
            with closing(Runtime(home, config={}, host="dsh")) as runtime:
                project = runtime.project(workspace)
                handle = runtime.register_context(project, runtime.host_conversation("synthetic-dsh-busy"))["context_handle"]
                payload = {"workspace": str(workspace), "home": str(home), "tool": "knowledge_search",
                    "args": {"query": "missing_synthetic_1455", "context_handle": handle}}

                def invoke():
                    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ENTRY)],
                        input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True,
                        env=env, cwd=workspace, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)

                with closing(sqlite3.connect(home / "runtime.sqlite3", timeout=1.5, isolation_level=None)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    try:
                        busy = invoke()
                    finally:
                        writer.execute("ROLLBACK")
                self.assert_busy(busy, "knowledge_search", busy_error())
                retry = invoke()
                self.assertTrue(retry["ok"], retry)
                self.assertEqual(retry["result"]["items"], [])
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
