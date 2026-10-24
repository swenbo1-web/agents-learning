"""Read discovery shares full verification only within a single request."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import Runtime


class PanelInspectionScopeTests(unittest.TestCase):
    def test_discovery_reuses_inspection_and_next_request_checks_installation_change(self):
        temp = tempfile.TemporaryDirectory(prefix="alp-panel-inspection-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = root / "project"
        workspace.mkdir()
        runtime = Runtime(root / "home", config={})
        self.addCleanup(runtime.close)
        project = runtime.project(workspace)
        receipt = {"panel_workspace": str(workspace), "home": str(runtime.home), "release_id": "one"}
        with patch("agent_learning.install_update.inspect_installation", return_value={"receipt": receipt}) as inspect:
            server = Server(runtime, project, workspace=workspace, panel_installation=root / "export")
            self.addCleanup(server.close)
            server.state = "ready"
            server.panel_ui_supported = True
            inspect.reset_mock()
            response = server.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertIn("learning_panel_open", [tool["name"] for tool in response["result"]["tools"]])
            self.assertEqual(inspect.call_count, 1)
            inspect.return_value = {"receipt": dict(receipt, release_id="two")}
            response = server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertNotIn("learning_panel_open", [tool["name"] for tool in response["result"]["tools"]])
            self.assertEqual(inspect.call_count, 2)
            inspect.reset_mock()
            server.panel.eligible()
            server.direct_panel.eligible()
            self.assertEqual(inspect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
