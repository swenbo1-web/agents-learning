"""Boundary outcomes from the second health audit, with no model calls."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.knowledge_applicability import automatic_applicability, project_environment
from agent_learning.manifest_versions import _fallback_fields, toml_fields
from agent_learning.query_language import canonical_text
from agent_learning.runtime import Runtime
from agent_learning.runtime_storage import initialization_guard
from agent_learning.software_updates import _locked


class HealthBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.r = Runtime(self.root / "state")
        self.addCleanup(self.r.close)

    def test_malformed_disposable_receipt_recovers_without_changing_authority(self):
        project = self.r.project(self.root / "project")
        self.r.feedback(project, "chat", "Retain this reported observation")
        epoch = self.r.host_epoch()
        evidence = [tuple(row) for row in self.r.db.execute("SELECT * FROM events ORDER BY id")]
        for definition in ("id INTEGER PRIMARY KEY,signature TEXT", "id INTEGER,signature TEXT,schema_cookie INTEGER"):
            with self.subTest(definition=definition):
                self.r.db.execute("DROP TABLE runtime_storage_receipt")
                self.r.db.execute("CREATE TABLE runtime_storage_receipt(" + definition + ")")
                other = Runtime(self.r.home)
                try:
                    self.assertEqual(other.host_epoch(), epoch)
                    self.assertEqual([tuple(row) for row in other.db.execute("SELECT * FROM events ORDER BY id")], evidence)
                    self.assertEqual(other.db.execute("PRAGMA user_version").fetchone()[0], 20)
                finally:
                    other.close()

    def test_first_startup_is_serialized_across_three_host_processes(self):
        state = self.root / "cold"
        code = ("import sys; from agent_learning.runtime import Runtime; "
                "r=Runtime(sys.argv[1],host=sys.argv[2]); print(r.session_epoch()); r.close()")
        children = []
        try:
            for host in ("codex", "antigravity", "dsh"):
                children.append(subprocess.Popen([sys.executable, "-B", "-c", code, str(state), host],
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            for child in children:
                output, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, error.decode("utf-8", "replace"))
                self.assertTrue(output.strip().isdigit())
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)

    def test_initialization_lock_is_bounded_and_released_after_failure(self):
        with _locked(self.r.home / ".runtime-initialization.lock") as acquired:
            self.assertTrue(acquired)
            with self.assertRaisesRegex(sqlite3.OperationalError, "initialization busy"):
                with initialization_guard(self.r, timeout=.02):
                    self.fail("lock was already held")
        cold = self.root / "interrupted"
        with patch.object(Runtime, "_migrate_context_storage", side_effect=RuntimeError("injected migration failure")):
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                Runtime(cold)
        other = Runtime(cold)
        other.close()

    def test_chinese_paths_and_keys_are_not_translated(self):
        for text in ("Docs/知识索引.md", r"C:\项目\管理员.md", "设置.管理员开关", "user_知识库",
                     "Docs/project_memory.py", "配置:管理员", "项目-知识库"):
            with self.subTest(text=text):
                self.assertEqual(canonical_text(text), text)
        self.assertIn("administrator", canonical_text("管理员：检查项目知识"))
        self.assertIn("knowledge", canonical_text("管理员：检查项目知识"))

    def test_toml_examples_and_nested_values_are_not_project_fields(self):
        documents = [
            '[project]\nname = "example"\ndynamic = ["version"]\ndescription = """\nversion = "0.1.0"\n"""\n',
            "[package]\nname = 'example'\ndescription = '''\nversion = '0.1.0'\n'''\n",
            '[project]\nname = "example"\nauthors = [\n{name="someone", version="0.1.0"}\n]\n',
        ]
        for text in documents:
            section = "package" if text.startswith("[package]") else "project"
            with self.subTest(text=text):
                self.assertNotIn("version", _fallback_fields(text, section))
                self.assertNotIn("version", toml_fields(text, section))
        static = '[project] # header\nname = "example"\nversion = "2.1.0"\n'
        self.assertEqual(_fallback_fields(static, "project"), {"name":"example", "version":"2.1.0"})

    def test_manifest_depth_and_multiline_descriptions_remain_optional(self):
        workspace = self.root / "manifest-project"
        workspace.mkdir()
        (workspace / "package.json").write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
        (workspace / "pyproject.toml").write_text(
            '[project]\nname="example"\ndescription="""\nversion="0.1.0"\n"""\ndynamic=["version"]\n', encoding="utf-8")
        self.assertEqual(project_environment(workspace), {})

    def test_version_alternatives_ranges_and_markup_remain_unknown(self):
        environment = {"project_version":"2.0.0", "project_names":["example"]}
        for text in ("项目版本 1.0.0 或 2.0.0", "project version 1.0.0 or 2.0.0",
                     "项目版本 `1.0.0` 或 `2.0.0`", "项目版本1.0.0至2.0.0",
                     "项目版本1.0.0-2.0.0", "项目版本1.0.*", "项目版本1.0.x"):
            with self.subTest(text=text):
                self.assertEqual(automatic_applicability({"category":"project", "applicability":text},
                                                        environment)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
