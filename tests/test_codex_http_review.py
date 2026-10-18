"""Explicit HTTP review transport retains the administrator execution contract."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from unittest.mock import patch

from agent_learning.admin_providers import (build_execution, codex_http_command,
    resolve_command, validate_command)
from agent_learning.admin_routing import validate_executor_command
from agent_learning.cli import main
from agent_learning.runtime import ContractError, Runtime


class CodexHttpReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="http-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_exact_preset_has_no_credentials_and_does_not_allow_arbitrary_options(self):
        command = codex_http_command()
        self.assertEqual(validate_command(command), command)
        self.assertEqual(validate_executor_command(command), command)
        for bad in ([*command, "--dangerously-bypass-approvals-and-sandbox"],
                    [*command, "--model", "different"],
                    [command[0], *command[1:-1], command[-1].replace("chatgpt.com", "example.com")],
                    ["other-cli", *command[1:]], ["codex", "-c", "agents.enabled=true"]):
            with self.subTest(command=bad), self.assertRaises(ContractError):
                validate_command(bad)
        command.append("mutated")
        self.assertNotIn("mutated", codex_http_command())

    def test_http_preserves_model_stdin_and_all_reviewer_restrictions(self):
        execution = build_execution(codex_http_command(), "codex", "gpt-test", self.root,
            self.root / "schema.json", self.root / "result.json", "private evidence", 20,
            selection_mode="first")
        args = execution["args"]
        self.assertEqual(args[:5], codex_http_command())
        self.assertEqual(args[args.index("--model") + 1], "gpt-test")
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--output-schema"):
            self.assertIn(flag, args)
        for feature in ("hooks", "plugins", "apps", "shell_tool", "unified_exec", "multi_agent"):
            self.assertTrue(any(args[i:i+2] == ["--disable", feature] for i in range(len(args)-1)))
        self.assertNotIn("private evidence", " ".join(args))
        self.assertEqual(execution["stdin_data"], b"private evidence")

    def test_http_preset_still_uses_the_host_native_cli_hint(self):
        native = self.root / "codex.exe"
        native.write_bytes(b"not executable; never launched")
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(native)}), \
                patch("agent_learning.admin_providers.shutil.which", side_effect=AssertionError("no PATH fallback")):
            resolved = resolve_command(codex_http_command())
        self.assertEqual(resolved["executable_source"], "host_cli")
        self.assertEqual(resolved["args"], [str(native), *codex_http_command()[1:]])

    def cli(self, host="codex"):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--home", str(self.root / "home"), "--workspace", str(self.root),
                         "--host", host, "admin-configure", "--enabled", "on", "--codex-http"])
        return code, json.loads(output.getvalue() or errors.getvalue())

    def test_cli_applies_only_to_selected_project_without_starting_review(self):
        code, _ = self.cli()
        self.assertEqual(code, 0)
        with closing(Runtime(self.root / "home", config={})) as runtime:
            project = runtime.project(self.root)
            other_workspace = self.root / "other"
            (other_workspace / ".git").mkdir(parents=True)
            other = runtime.project(other_workspace)
            self.assertEqual(runtime.admin_policy(project)["command"], codex_http_command())
            self.assertFalse(runtime.admin_policy(other)["enabled"])
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_cli_refuses_non_codex_host_without_enabling_its_administrator(self):
        code, result = self.cli("dsh")
        self.assertEqual(code, 1)
        self.assertIn("Codex administrator", result["reason"])
        with closing(Runtime(self.root / "home", config={}, host="dsh")) as runtime:
            self.assertFalse(runtime.admin_policy(runtime.project(self.root))["enabled"])


if __name__ == "__main__":
    unittest.main()
