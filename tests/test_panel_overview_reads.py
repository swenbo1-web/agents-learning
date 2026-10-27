"""Panel read projections share a bounded snapshot without loading full detail."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning import admin_control
from agent_learning.install import install
from agent_learning.panel_control import PanelControl
from agent_learning.runtime import ContractError, Runtime
from tests.test_panel_control import PanelControlTests as Fixtures


class PanelOverviewReadTests(unittest.TestCase):
    setUpClass = classmethod(Fixtures.setUpClass.__func__)
    tearDownClass = classmethod(Fixtures.tearDownClass.__func__)
    setUp = Fixtures.setUp
    bootstrap = Fixtures.bootstrap
    action = Fixtures.action

    def test_status_and_debug_do_not_load_unused_full_views(self):
        opened = self.bootstrap()
        before = self.runtime.db.total_changes
        with patch.object(self.runtime, "status", side_effect=AssertionError("full runtime status")), \
             patch("agent_learning.admin_control._control_queue", side_effect=AssertionError("queue detail")), \
             patch("agent_learning.admin_recovery.recovery_status", side_effect=AssertionError("recovery detail")), \
             patch("agent_learning.admin_review_flow.review_activity", side_effect=AssertionError("review activity")), \
             patch("subprocess.Popen", side_effect=AssertionError("no process")):
            for action in ("status", "debug"):
                self.assertTrue(self.action(opened, action)["visible"])
        self.assertEqual(self.runtime.db.total_changes, before)

    def test_debug_reads_common_control_only_once(self):
        opened = self.bootstrap()
        with patch("agent_learning.admin_control.control_summary", wraps=admin_control.control_summary) as read, \
             patch.object(self.runtime, "admin_routing_status", wraps=self.runtime.admin_routing_status) as routing:
            result = self.action(opened, "debug")
        read.assert_called_once_with(self.runtime, self.project)
        routing.assert_called_once_with(self.project)
        self.assertEqual(result["diagnostics"]["project"]["storage_schema"], 25)
        self.assertEqual(result["diagnostics"]["project"]["index_version"], 6)
        self.assertFalse(result["summary"]["administrator_readiness"]["authentication_checked"])

    def test_common_versions_and_counts_share_one_read_snapshot(self):
        self.runtime.feedback(self.project, "panel-source", "An isolated pending observation")
        opened = self.bootstrap()
        epoch = self.runtime.session_epoch()
        original = admin_control.control_summary
        def concurrent_update(runtime, project, **kwargs):
            state = original(runtime, project, **kwargs)
            with contextlib.closing(sqlite3.connect(runtime.home / "runtime.sqlite3", timeout=1)) as other:
                other.execute("UPDATE host_scopes SET epoch=epoch+1 WHERE scope=?", (runtime._session_scope(),))
                other.execute("UPDATE problems SET status='reviewed' WHERE project=?", (project,))
                other.commit()
            return state
        with patch("agent_learning.admin_control.control_summary", side_effect=concurrent_update):
            result = self.action(opened, "debug")
        self.assertEqual(result["summary"]["expected"]["expected_session_epoch"], epoch)
        self.assertEqual(result["summary"]["counts"]["pending_problems"], 1)
        self.assertFalse(self.runtime.db.in_transaction)
        with self.assertRaises(ContractError):
            self.action(opened, "status")

    def test_each_read_refreshes_data_and_does_not_leak_a_transaction(self):
        opened = self.bootstrap()
        first = self.action(opened, "status")
        self.runtime.feedback(self.project, "new-source", "A new pending observation")
        second = self.action(opened, "status")
        self.assertEqual(second["summary"]["counts"]["pending_problems"],
                         first["summary"]["counts"]["pending_problems"] + 1)
        self.assertFalse(self.runtime.db.in_transaction)

    def boundary_change_after_verify(self, change):
        opened = self.bootstrap()
        original = self.panel._verify
        def switch(*args, **kwargs):
            lease = original(*args, **kwargs)
            change()
            return lease
        with patch.object(self.panel, "_verify", side_effect=switch), \
             patch("agent_learning.admin_control.control_summary", side_effect=AssertionError("new boundary read")):
            with self.assertRaises(ContractError):
                self.action(opened, "status")
        self.assertFalse(self.runtime.db.in_transaction)

    def test_account_change_before_first_snapshot_read_rejects_old_capability(self):
        self.boundary_change_after_verify(self.runtime.account_changed)

    def test_governance_change_before_first_snapshot_read_rejects_old_capability(self):
        self.boundary_change_after_verify(lambda: self.runtime.db.execute(
            "UPDATE host_state SET epoch=epoch+1 WHERE id=1"))

    def test_failed_read_releases_only_its_owned_snapshot(self):
        opened = self.bootstrap()
        with patch("agent_learning.admin_control.control_summary", side_effect=ValueError("read interrupted")):
            with self.assertRaisesRegex(ValueError, "interrupted"):
                self.action(opened, "status")
        self.assertFalse(self.runtime.db.in_transaction)
        with self.runtime.transaction():
            result = self.action(opened, "status")
            self.assertTrue(result["visible"])
            self.assertTrue(self.runtime.db.in_transaction)

    def test_antigravity_keeps_capture_and_native_connection_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="panel-ag-read-") as temporary:
            base = Path(temporary)
            with contextlib.closing(Runtime(base / "home", config={}, host="antigravity")) as runtime:
                project = runtime.project(self.workspace)
                target = base / "installation"
                install(target, host="antigravity", runtime_store=base / "releases",
                        workspace=self.workspace, home=runtime.home)
                panel = PanelControl(runtime, project, self.workspace, target, require_context=False)
                self.addCleanup(panel.close)
                opened = panel.bootstrap(workspace=self.workspace, conversation_id="native-read")
                with patch.object(runtime, "status", side_effect=AssertionError("full AG status")):
                    result = panel.action(opened["capability"], "debug", workspace=self.workspace,
                                          conversation_id="native-read")
                self.assertIn("host_capture", result["summary"])
                self.assertEqual(result["diagnostics"]["capture"], result["summary"]["host_capture"])
                self.assertEqual(result["diagnostics"]["connection"]["mcp"], "unknown")
                self.assertIsNone(result["diagnostics"]["connection"]["context_bound"])

    def test_measure_summary_and_debug_without_timing_threshold(self):
        opened = self.bootstrap()
        for i in range(25):
            self.runtime.feedback(self.project, "load-source", "Isolated pending item " + str(i))
        results = {}
        for action in ("status", "debug"):
            statements = []
            self.runtime.db.set_trace_callback(statements.append)
            start = time.perf_counter()
            try:
                self.action(opened, action)
            finally:
                elapsed = time.perf_counter() - start
                self.runtime.db.set_trace_callback(None)
            results[action] = {"sql_statements": len(statements), "seconds": round(elapsed, 6)}
        label = os.environ.get("ALP_PANEL_MEASURE_LABEL", "current")
        root = Path(os.environ.get("ALP_TEST_ROOT", tempfile.gettempdir()))
        (root / ("panel-read-" + label + ".json")).write_text(json.dumps(results, indent=2), encoding="utf-8")
        self.assertTrue(all(item["sql_statements"] > 0 for item in results.values()))


del Fixtures

if __name__ == "__main__":
    unittest.main()
