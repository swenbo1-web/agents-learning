"""Project-scoped UI installation contracts; no user database or host is opened."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main, parser
from agent_learning.install import _hook_commands, install
from agent_learning.install_update import INSTALL_MANIFEST, inspect_installation, snapshot
from agent_learning.runtime import ContractError


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class PanelInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-panel-install-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self._cleanup_root)
        self.target = self.root / "plugin export"
        self.store = self.root / "fixed releases"
        self.workspace = self.root / "项目 workspace"
        self.workspace.mkdir()

    def _cleanup_root(self):
        from agent_learning.release import filesystem_path
        if self.root.exists() and self.root.name.startswith("alp-panel-install-"):
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.temp.cleanup()

    def initial(self, *, bound=True):
        return install(self.target, runtime_store=self.store,
                       panel_workspace=self.workspace if bound else None)

    def test_project_ui_binding_does_not_pin_normal_codex_tools(self):
        result = self.initial()
        receipt = read_json(self.target / "runtime-release.json")
        self.assertEqual(receipt["panel_workspace"], str(self.workspace))
        self.assertEqual(result["panel_workspace"], str(self.workspace))
        self.assertNotIn("workspace", receipt)
        self.assertNotIn("home", receipt)
        mcp = read_json(self.target / ".mcp.json")["mcpServers"]["agent_learning"]
        self.assertEqual(mcp["args"], [str(Path(result["runtime_directory"]) / "scripts/learning.py"),
            "serve", "--role", "worker", "--panel-installation", str(self.target)])
        self.assertNotIn("--workspace", mcp["args"])
        self.assertNotIn("--home", mcp["args"])
        self.assertEqual(inspect_installation(self.target, _hook_commands)["receipt"], receipt)
        self.assertTrue((self.target / "skills/learning-panel/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-panel/agents/openai.yaml").is_file())

    def test_global_codex_export_and_upgrade_do_not_enable_a_project_ui(self):
        self.initial(bound=False)
        for upgraded in (False, True):
            if upgraded:
                install(self.target, upgrade=True)
            self.assertNotIn("panel_workspace", read_json(self.target / "runtime-release.json"))
            self.assertNotIn("--panel-installation",
                read_json(self.target / ".mcp.json")["mcpServers"]["agent_learning"]["args"])
            inspect_installation(self.target, _hook_commands)

    def test_legacy_receipt_without_new_fields_or_manifest_still_upgrades(self):
        self.initial(bound=False)
        receipt = read_json(self.target / "runtime-release.json")
        receipt.pop("target_host")
        write_json(self.target / "runtime-release.json", receipt)
        (self.target / INSTALL_MANIFEST).unlink()
        inspected = inspect_installation(self.target, _hook_commands)
        self.assertEqual(inspected["target_host"], "codex")
        install(self.target, upgrade=True)
        self.assertNotIn("panel_workspace", read_json(self.target / "runtime-release.json"))

    def test_upgrade_preserves_existing_panel_project(self):
        self.initial()
        result = install(self.target, upgrade=True)
        self.assertEqual(result["panel_workspace"], str(self.workspace))
        self.assertEqual(read_json(self.target / "runtime-release.json")["panel_workspace"], str(self.workspace))
        self.assertEqual(read_json(self.target / ".mcp.json")["mcpServers"]["agent_learning"]["args"][-2:],
                         ["--panel-installation", str(self.target)])
        inspect_installation(self.target, _hook_commands)

    def test_explicit_upgrade_can_add_binding_to_a_legacy_global_export(self):
        self.initial(bound=False)
        install(self.target, upgrade=True, panel_workspace=self.workspace)
        self.assertEqual(read_json(self.target / "runtime-release.json")["panel_workspace"], str(self.workspace))
        inspect_installation(self.target, _hook_commands)

    def test_an_existing_panel_cannot_silently_move_to_another_project(self):
        self.initial()
        other = self.root / "another project"
        other.mkdir()
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "cannot be rebound"):
            install(self.target, upgrade=True, panel_workspace=other)
        self.assertEqual(snapshot(self.target), before)

    def test_invalid_panel_workspace_is_rejected_without_creating_it(self):
        missing = self.root / "missing project"
        with self.assertRaisesRegex(ContractError, "existing directory"):
            install(self.target, runtime_store=self.store, panel_workspace=missing)
        self.assertFalse(missing.exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(self.store.exists())

    def test_panel_binding_is_codex_only_and_keeps_old_fixed_binding_restriction(self):
        for host in ("antigravity", "dsh"):
            with self.subTest(host=host), self.assertRaisesRegex(ContractError, "only to Codex"):
                install(self.target, host=host, panel_workspace=self.workspace)
        for options in ({"workspace": self.workspace}, {"home": self.root / "home"}):
            with self.subTest(options=options), self.assertRaisesRegex(ContractError, "fixed workspace and home"):
                install(self.target, **options)

    def test_inspection_rejects_noncanonical_or_missing_bound_project(self):
        self.initial()
        receipt = read_json(self.target / "runtime-release.json")
        for invalid in (None, "relative/project", str(self.root / "missing"), str(self.workspace) + "/."):
            with self.subTest(invalid=invalid):
                write_json(self.target / "runtime-release.json", {**receipt, "panel_workspace": invalid})
                with self.assertRaisesRegex(ContractError, "panel workspace"):
                    inspect_installation(self.target, _hook_commands)

    def test_inspection_rejects_removing_project_ui_mcp_wiring(self):
        self.initial()
        mcp = read_json(self.target / ".mcp.json")
        mcp["mcpServers"]["agent_learning"]["args"] = mcp["mcpServers"]["agent_learning"]["args"][:-2]
        write_json(self.target / ".mcp.json", mcp)
        with self.assertRaisesRegex(ContractError, "MCP configuration was modified"):
            inspect_installation(self.target, _hook_commands)

    def test_antigravity_exports_sidecar_from_the_same_fixed_release(self):
        home = self.root / "isolated home"
        result = install(self.target, runtime_store=self.store, host="antigravity",
                         workspace=self.workspace, home=home)
        runtime = Path(result["runtime_directory"])
        for filename in ("sidecar.json", "main.mjs", "index.html"):
            relative = Path("sidecars/control") / filename
            self.assertEqual((self.target / relative).read_bytes(),
                             (runtime / "host_resources/antigravity" / relative).read_bytes())
        self.assertFalse(home.exists(), "installation must not create the knowledge database")
        self.assertNotIn("panel_workspace", read_json(self.target / "runtime-release.json"))
        inspect_installation(self.target, _hook_commands)


class PanelCliTests(unittest.TestCase):
    def test_codex_install_explicit_workspace_only_binds_the_panel(self):
        with patch("agent_learning.cli.install", return_value={}) as exported, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--workspace", "chosen project", "install", "export directory"]), 0)
        self.assertEqual(exported.call_args.kwargs["panel_workspace"], "chosen project")
        self.assertNotIn("workspace", exported.call_args.kwargs)
        self.assertNotIn("home", exported.call_args.kwargs)

    def test_codex_install_without_explicit_workspace_keeps_panel_disabled(self):
        with patch("agent_learning.cli.install", return_value={}) as exported, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["install", "export directory"]), 0)
        self.assertIsNone(exported.call_args.kwargs["panel_workspace"])

    def test_panel_control_delegates_before_constructing_default_runtime(self):
        with patch("agent_learning.panel_control.serve_panel") as bridge, \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("CLI must not open a default database")):
            self.assertEqual(main(["panel-serve", "--installation", "export directory"]), 0)
        bridge.assert_called_once_with("export directory")

    def test_serve_parser_accepts_optional_installation_without_changing_workspace(self):
        parsed = parser().parse_args(["--workspace", "actual host workspace", "serve",
                                      "--panel-installation", "panel installation"])
        self.assertEqual(parsed.workspace, "actual host workspace")
        self.assertEqual(parsed.panel_installation, "panel installation")
        self.assertIsNone(parser().parse_args(["serve"]).panel_installation)

    def test_serve_forwards_panel_installation_without_rebinding_normal_workspace(self):
        with patch("agent_learning.cli.Runtime") as runtime, patch("agent_learning.cli.Server") as server:
            runtime.return_value.project.return_value = "current-project"
            self.assertEqual(main(["--workspace", "actual host workspace", "serve",
                                   "--panel-installation", "panel installation"]), 0)
        runtime.return_value.project.assert_called_once_with("actual host workspace")
        self.assertEqual(server.call_args.kwargs["workspace"], "actual host workspace")
        self.assertEqual(server.call_args.kwargs["panel_installation"], "panel installation")
        server.return_value.serve.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
