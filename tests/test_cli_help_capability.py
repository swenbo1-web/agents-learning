"""No-model subprocess checks for bounded standard CLI help and SDK probes."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import admin_providers as providers


AG_FLAGS = (b"--input-format", b"--output-format", b"--json-schema", b"--agent", b"--print-timeout")
CODEX_FLAGS = b"--ignore-user-config --ignore-rules --ephemeral --output-schema --output-last-message"
SDK_READY = b'{"protocol":"alp-dsh-sdk-v1","ready":true}\n'


class CliHelpCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-cli-help-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sequence = 0
        self.cache = patch.dict(providers._CAPABILITIES, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def probe(self, *, stdout=b"", stderr=b"", returncode=0, provider="antigravity", sdk=False):
        self.sequence += 1
        script = self.root / ("probe-" + str(self.sequence) + ".py")
        expected = ["--probe"] if sdk else ["exec", "--help"] if provider == "codex" else ["--help"]
        script.write_text(
            "import os, sys\n"
            "assert sys.argv[1:] == " + repr(expected) + "\n"
            "assert sys.stdin.buffer.read() == b''\n"
            "assert os.environ.get('ALP_DISABLED') == '1'\n"
            "assert 'ALP_DSH_REVIEW_PROFILE' not in os.environ\n"
            "sys.stdout.buffer.write(" + repr(stdout) + ")\n"
            "sys.stdout.buffer.flush()\n"
            "sys.stderr.buffer.write(" + repr(stderr) + ")\n"
            "sys.stderr.buffer.flush()\n"
            "sys.exit(" + str(returncode) + ")\n", encoding="utf-8")
        args = [sys.executable, "-B", str(script)]
        name = "dsh" if sdk else "codex" if provider == "codex" else "agy"
        plan = {"provider": provider, "command": [name]}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": "PRIVATE_FAKE_PROFILE"}))
            stack.enter_context(patch.object(providers, "resolve_command", return_value={"args": args, "launch_kind": "executable"}))
            if sdk:
                stack.enter_context(patch.object(providers, "dsh_sdk_launcher", return_value={"args": args}))
                stack.enter_context(patch("agent_learning.dsh_profile.assert_profile"))
            return providers.executor_capability(plan)

    def assert_help(self, result, ready):
        self.assertEqual(result, {"ready": ready, "reason": None if ready else "cli_protocol_unsupported", "checked": "help"})

    def test_antigravity_help_accepts_stdout_stderr_and_split_flags(self):
        help_text = b" ".join(AG_FLAGS)
        for stdout, stderr in ((help_text, b""), (b"", help_text),
                               (b" ".join(AG_FLAGS[:2]) + b"\n", b" ".join(AG_FLAGS[2:]))):
            with self.subTest(stdout=bool(stdout), stderr=bool(stderr)):
                self.assert_help(self.probe(stdout=stdout, stderr=stderr), True)

    def test_codex_standard_help_also_accepts_stderr(self):
        self.assert_help(self.probe(stderr=CODEX_FLAGS, provider="codex"), True)

    def test_nonzero_exit_rejects_complete_help_on_either_stream(self):
        help_text = b" ".join(AG_FLAGS)
        for stdout, stderr in ((help_text, b""), (b"", help_text)):
            with self.subTest(stderr=bool(stderr)):
                self.assert_help(self.probe(stdout=stdout, stderr=stderr, returncode=2), False)

    def test_each_required_flag_is_needed_even_when_help_is_on_stderr(self):
        for absent in AG_FLAGS:
            with self.subTest(absent=absent):
                self.assert_help(self.probe(stderr=b" ".join(flag for flag in AG_FLAGS if flag != absent)), False)

    def test_help_limit_counts_combined_stdout_and_stderr_bytes(self):
        help_text = b" ".join(AG_FLAGS) + b"\n"
        for total, ready in ((65536, True), (65537, False)):
            with self.subTest(total=total):
                self.assert_help(self.probe(stdout=help_text, stderr=b"x" * (total - len(help_text))), ready)

    def test_multibyte_help_cannot_bypass_byte_limit(self):
        help_text = b" ".join(AG_FLAGS) + b"\n" + "诊断".encode("utf-8") * 11000
        self.assertGreater(len(help_text), 65536)
        self.assertLess(len(help_text.decode("utf-8")), 65536)
        self.assert_help(self.probe(stderr=help_text), False)

    def test_sdk_json_ignores_stderr_diagnostics(self):
        result = self.probe(stdout=SDK_READY, stderr=b"non-JSON diagnostic\n" * 5000, provider="dsh", sdk=True)
        self.assertEqual(result, {"ready": True, "reason": None, "checked": "sdk"})

    def test_sdk_cannot_take_protocol_json_from_stderr(self):
        result = self.probe(stdout=b"{}", stderr=SDK_READY, provider="dsh", sdk=True)
        self.assertEqual(result, {"ready": False, "reason": "dsh_sdk_unavailable", "checked": "sdk"})

    def test_help_timeout_keeps_fixed_reason(self):
        with patch.object(providers, "resolve_command", return_value={"args": ["agy"], "launch_kind": "executable"}), \
                patch.object(providers.subprocess, "run", side_effect=subprocess.TimeoutExpired(["agy", "--help"], 3)):
            result = providers.executor_capability({"provider": "antigravity", "command": ["agy"]})
        self.assertEqual(result, {"ready": False, "reason": "cli_help_timeout", "checked": "help"})


if __name__ == "__main__":
    unittest.main()
