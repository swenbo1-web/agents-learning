"""Project UI commands delegate before the default Runtime can create a DB."""
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import call, patch

from agent_learning.cli import main
from agent_learning.runtime import ContractError


class CodexPanelCliTests(unittest.TestCase):
    def test_global_discovery_constructs_no_runtime_or_project(self):
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("discovery opened a database")), \
                patch("agent_learning.cli.Server") as server:
            self.assertEqual(main(["serve", "--panel-discovery"]), 0)
        server.assert_called_once_with(None, None, panel_discovery=True)
        server.return_value.serve.assert_called_once_with()

    def test_discovery_rejects_project_or_admin_arguments_before_runtime(self):
        for arguments in (["--host", "antigravity", "serve", "--panel-discovery"],
                          ["--host", "dsh", "serve", "--panel-discovery"],
                          ["serve", "--panel-discovery", "--role", "admin"],
                          ["serve", "--panel-discovery", "--context-handle", "foreign"],
                          ["serve", "--panel-discovery", "--plugin-installation", "installation"],
                          ["serve", "--panel-discovery", "--panel-installation", "installation"]):
            with self.subTest(arguments=arguments), \
                    patch("agent_learning.cli.Runtime", side_effect=AssertionError("invalid discovery opened a database")), \
                    patch("agent_learning.cli.Server") as server, redirect_stderr(io.StringIO()):
                self.assertEqual(main(arguments), 1)
                server.assert_not_called()

    def test_invalid_project_installation_is_rejected_before_runtime(self):
        with patch("agent_learning.project_admission.require_project_installation", side_effect=ContractError("project_not_installed")), \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("uninstalled workspace opened a database")), \
                patch("agent_learning.cli.Server") as server, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--workspace", "uninstalled", "serve", "--panel-installation", "missing"]), 1)
        server.assert_not_called()

    def test_plugin_installation_requires_codex_worker_and_matching_panel_before_runtime(self):
        for arguments in (["--host", "antigravity", "serve", "--plugin-installation", "installation"],
                          ["--host", "dsh", "serve", "--plugin-installation", "installation"],
                          ["serve", "--role", "admin", "--plugin-installation", "installation"],
                          ["serve", "--plugin-installation", "one", "--panel-installation", "two"]):
            with self.subTest(arguments=arguments), \
                    patch("agent_learning.cli.Runtime", side_effect=AssertionError("invalid plugin connection opened a database")), \
                    patch("agent_learning.cli.Server") as server, redirect_stderr(io.StringIO()):
                self.assertEqual(main(arguments), 1)
                server.assert_not_called()

    def test_plugin_installation_is_checked_and_forwarded_without_panel(self):
        with patch("agent_learning.project_admission.require_project_installation",
                   return_value={"workspace": "installed root"}) as admission, \
                patch("agent_learning.cli.Runtime") as runtime, patch("agent_learning.cli.Server") as server:
            runtime.return_value.project.return_value = "installed-project"
            self.assertEqual(main(["--workspace", "installed workspace", "serve", "--plugin-installation", "plugin installation"]), 0)
        admission.assert_called_once_with("installed workspace", "plugin installation", host="codex", runtime_directory=None)
        self.assertEqual(runtime.return_value.project.call_args_list,
                         [call("installed root"), call("installed workspace")])
        self.assertEqual(server.call_args.kwargs["workspace"], "installed workspace")
        self.assertEqual(server.call_args.kwargs["plugin_installation"], "plugin installation")
        self.assertIsNone(server.call_args.kwargs["panel_installation"])

    def test_explicit_standalone_serve_keeps_normal_workspace_contract(self):
        with patch("agent_learning.cli.Runtime") as runtime, patch("agent_learning.cli.Server") as server:
            runtime.return_value.project.return_value = "explicit-project"
            self.assertEqual(main(["--workspace", "explicit workspace", "serve"]), 0)
        runtime.return_value.project.assert_called_once_with("explicit workspace")
        self.assertEqual(server.call_args.kwargs["workspace"], "explicit workspace")

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
