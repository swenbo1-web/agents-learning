"""Worktree admission uses Git's registered identity and rejects forged pointers."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from agent_learning.git_worktrees import LinkedWorktree, linked_worktree_identity


@unittest.skipUnless(shutil.which("git"), "Git is required for linked worktree fixtures")
class GitWorktreeIdentityTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "git-worktree-verifier"
        base.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(fixture.cleanup)
        self.base = Path(fixture.name).resolve()
        self.primary = self.base / "project main"
        self.checkout = self.base / "worktree 子分支"
        self.environment = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update(GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
                                GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Worktree Fixture",
                                GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_NAME="Worktree Fixture",
                                GIT_COMMITTER_EMAIL="fixture@example.invalid")
        self.git("init", "--quiet", str(self.primary))
        self.git("-C", str(self.primary), "commit", "--quiet", "--allow-empty", "-m", "fixture")
        self.git("-C", str(self.primary), "worktree", "add", "--quiet", "--detach", str(self.checkout))
        self.admin = Path(os.fsdecode(self.git("-C", str(self.checkout), "rev-parse", "--absolute-git-dir").strip()))
        info = self.admin.stat()
        self.identity = LinkedWorktree(self.checkout, self.primary, self.admin, (info.st_dev, info.st_ino))

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], env=self.environment, cwd=self.base, timeout=10,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

    def test_registered_worktree_resolves_from_nested_directory(self):
        nested = self.checkout / "src" / "code"
        nested.mkdir(parents=True)
        self.assertEqual(linked_worktree_identity(nested), self.identity)
        self.assertEqual(linked_worktree_identity(self.checkout), self.identity)
        self.assertIsNone(linked_worktree_identity(self.primary))

    def test_relative_registration_paths(self):
        try:
            self.git("-C", str(self.primary), "worktree", "repair", "--relative-paths", str(self.checkout))
        except subprocess.CalledProcessError as exc:
            if b"unknown option" in exc.stderr:
                self.skipTest("Git lacks worktree repair --relative-paths")
            raise
        self.assertTrue((self.checkout / ".git").read_bytes().startswith(b"gitdir: ../"))
        self.assertEqual(linked_worktree_identity(self.checkout), self.identity)

    def test_switching_branches_leaves_the_checkout_identity_intact(self):
        self.git("-C", str(self.checkout), "switch", "--quiet", "-c", "second")
        (self.checkout / "dirty").write_text("untracked", encoding="utf-8")
        self.assertEqual(linked_worktree_identity(self.checkout), self.identity)

    def test_copied_gitfile_does_not_inherit_primary(self):
        spoof = self.base / "lookalike"
        spoof.mkdir()
        shutil.copyfile(self.checkout / ".git", spoof / ".git")
        self.assertIsNone(linked_worktree_identity(spoof))

    def test_common_directory_must_match_the_registration(self):
        (self.admin / "commondir").write_text("..\n", encoding="utf-8")
        self.assertIsNone(linked_worktree_identity(self.checkout))

    def test_regular_nested_checkout_keeps_its_own_identity(self):
        child = self.checkout / "nested checkout"
        self.git("init", "--quiet", str(child))
        self.assertIsNone(linked_worktree_identity(child))

    def test_separate_git_directory_is_not_a_worktree_registration(self):
        other = self.base / "separate"
        self.git("init", "--quiet", "--separate-git-dir", str(self.base / "external gitdir"), str(other))
        self.assertIsNone(linked_worktree_identity(other))

    def test_external_primary_worktree_override_is_rejected(self):
        self.git("-C", str(self.primary), "config", "core.worktree", str(self.base / "other"))
        self.assertIsNone(linked_worktree_identity(self.checkout))

    def test_bare_primary_is_rejected(self):
        self.git("-C", str(self.primary), "config", "core.bare", "true")
        self.assertIsNone(linked_worktree_identity(self.checkout))

    def test_primary_worktree_configuration_is_checked(self):
        self.git("-C", str(self.primary), "config", "extensions.worktreeConfig", "true")
        self.git("-C", str(self.primary), "config", "--worktree", "core.worktree", str(self.base / "other"))
        self.assertIsNone(linked_worktree_identity(self.checkout))

    def test_linked_worktree_configuration_is_checked(self):
        self.git("-C", str(self.primary), "config", "extensions.worktreeConfig", "true")
        self.git("-C", str(self.checkout), "config", "--worktree", "core.worktree", str(self.base / "other"))
        self.assertIsNone(linked_worktree_identity(self.checkout))

    def test_inline_git_config_assignment_cannot_hide_override(self):
        config = self.primary / ".git" / "config"
        with config.open("ab") as stream:
            stream.write(b"\n[core] bare=true\n")
        self.assertIsNone(linked_worktree_identity(self.checkout))

    def test_linked_backlink_is_rejected(self):
        backlink = self.admin / "gitdir"
        separate = self.base / "backlink-text"
        shutil.copyfile(backlink, separate)
        backlink.unlink()
        try:
            backlink.symlink_to(separate)
        except (NotImplementedError, OSError):
            self.skipTest("creating a symlink requires host support")
        self.assertIsNone(linked_worktree_identity(self.checkout))


if __name__ == "__main__":
    unittest.main()
