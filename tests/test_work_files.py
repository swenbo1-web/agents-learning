"""文件关系只解释明确目标，不读取正文或执行命令。"""
from pathlib import Path
import tempfile
import unittest

from agent_learning.work_files import checkout_key, checkout_root, file_links_from_payload, normalize_file_target


class WorkFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()

    def test_same_relative_file_is_distinct_in_other_checkout(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        a = normalize_file_target("src/a.py", self.root)
        b = normalize_file_target("src/a.py", other)
        self.assertEqual(a["relative_path"], b["relative_path"])
        self.assertNotEqual(a["checkout_key"], b["checkout_key"])
        self.assertEqual(a["version"]["status"], "unavailable")

    def test_outside_path_url_and_nul_are_rejected(self):
        for target in ("../outside.py", "https://example.org/a.py", "bad\0path"):
            self.assertIsNone(normalize_file_target(target, self.root))

    def test_symlink_outside_workspace_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        self.assertIsNone(normalize_file_target("link/a.py", self.root))

    def test_native_antigravity_fields_and_stat_are_bounded(self):
        path = self.root / "a.py"
        path.write_text("private source body", encoding="utf-8")
        result = file_links_from_payload("replace_file_content", {"TargetFile": str(path)}, self.root)
        self.assertEqual(result[0]["relation"], "write_target")
        self.assertEqual(result[0]["version"]["status"], "stat_observed")
        self.assertIsNone(result[0]["version"]["content_hash"])
        self.assertNotIn("private source body", repr(result))

    def test_patch_explicit_headers_include_move_target(self):
        patch = "*** Begin Patch\n*** Update File: src/a.py\n*** Move to: src/b.py\n@@\n-x\n+y\n*** End Patch"
        result = file_links_from_payload("functions.apply_patch", patch, self.root)
        self.assertEqual([r["relative_path"] for r in result], ["src/a.py", "src/b.py"])
        self.assertTrue(all(r["relation"] == "write_target" for r in result))

    def test_only_explicit_single_read_shell_commands_are_supported(self):
        for command in ('cat "src/a.py"', 'Get-Content -LiteralPath "src/a.py"'):
            result = file_links_from_payload("exec_command", {"cmd": command}, self.root)
            self.assertEqual(result[0]["relative_path"], "src/a.py")
        for command in ("cat a.py | sort", "cat $HOME/a.py", "python -c 'open(a.py)'", "cat a.py b.py"):
            self.assertEqual(file_links_from_payload("exec_command", {"cmd": command}, self.root), [])

    def test_directory_fields_are_not_file_targets(self):
        self.assertEqual(file_links_from_payload("exec_command", {"cwd": str(self.root)}, self.root), [])

    def test_sensitive_names_and_roots_are_not_project_file_context(self):
        for path in (".env", ".env.local", "auth.json", "credentials.json", "secrets/config.json",
                     ".git/config", ".ssh/id_rsa", "private.pem"):
            self.assertIsNone(normalize_file_target(path, self.root))
        self.assertIsNotNone(normalize_file_target("config.json", self.root))

    def test_shell_relative_target_uses_explicit_command_workdir(self):
        result = file_links_from_payload("exec_command", {"cmd": "cat a.py", "workdir": "src"}, self.root)
        self.assertEqual(result[0]["relative_path"], "src/a.py")
        native = file_links_from_payload("run_command", {"CommandLine": "cat a.py", "Cwd": str(self.root / "src")}, self.root)
        self.assertEqual(native[0]["relative_path"], "src/a.py")
        self.assertEqual(file_links_from_payload("exec_command", {"cmd": "cat a.py", "cwd": "../outside"}, self.root), [])

    def test_git_subdirectories_share_checkout_identity_but_worktrees_do_not(self):
        (self.root / ".git").mkdir()
        source = self.root / "src"
        source.mkdir()
        tree = Path(self.temp.name) / "worktree"
        tree.mkdir()
        (tree / ".git").write_text("gitdir: ../project/.git/worktrees/tree", encoding="utf-8")
        self.assertEqual(checkout_root(source), self.root.resolve())
        self.assertEqual(checkout_key(source), checkout_key(self.root))
        self.assertNotEqual(checkout_key(tree), checkout_key(self.root))
        file = normalize_file_target("a.py", source)
        self.assertEqual(file["relative_path"], "src/a.py")
        parent = normalize_file_target("../README.md", source)
        self.assertEqual(parent["relative_path"], "README.md")

    def test_git_shell_relative_directory_stays_relative_to_actual_cwd(self):
        (self.root / ".git").mkdir()
        source = self.root / "src"
        source.mkdir()
        result = file_links_from_payload("exec_command", {"cmd": "cat a.py", "workdir": "components"}, source)
        self.assertEqual(result[0]["relative_path"], "src/components/a.py")


if __name__ == "__main__":
    unittest.main()
