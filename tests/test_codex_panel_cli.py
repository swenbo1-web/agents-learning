"""Project UI commands delegate before the default Runtime can create a DB."""
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.runtime import ContractError


class CodexPanelCliTests(unittest.TestCase):
    def test_project_action_launch_uses_explicit_data_scope_before_default_runtime(self):
        with patch("agent_learning.codex_panel.open_panel") as opened, \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("wrong database")):
            result = main(["--home", "isolated-home", "--workspace", "installed-project", "--host-instance", "test",
                           "codex-panel", "--installation", "installed-plugin", "--port", "4444", "--no-open"])
        self.assertEqual(result, 0)
        opened.assert_called_once_with("installed-plugin", workspace="installed-project", home="isolated-home",
                                      host_instance="test", port=4444, open_browser=False)

    def test_project_panel_rejection_does_not_construct_default_runtime(self):
        with patch("agent_learning.codex_panel.open_panel", side_effect=ContractError("workspace mismatch")), \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("wrong database")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["codex-panel", "--installation", "installed-plugin"]), 1)

    def test_other_hosts_cannot_use_codex_launch_or_registration(self):
        for command in ("codex-panel", "codex-action"):
            with self.subTest(command=command), patch("agent_learning.cli.Runtime", side_effect=AssertionError("wrong database")), \
                    patch("agent_learning.codex_panel.open_panel") as opened, \
                    patch("agent_learning.codex_actions.install_action") as installed, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--host", "antigravity", command, "--installation", "installed-plugin"]), 1)
                opened.assert_not_called()
                installed.assert_not_called()
