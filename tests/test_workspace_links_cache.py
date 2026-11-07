"""Failed configuration reads remain bounded and cannot retain old approval."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime
from agent_learning import workspace_links


class WorkspaceLinkCacheTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build/local/r63cache"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "config.json"
        self.primary, self.linked = self.base / "p", self.base / "linked"
        for root in (self.primary, self.linked):
            root.mkdir()
            (root / ".git").mkdir()
        environment = patch.dict(os.environ, {
            "ALP_HOME": str(self.base / "h"), "ALP_CONFIG": str(self.config),
            "ALP_RUNTIME_STORE": str(self.base / "r"), "CODEX_HOME": str(self.base / "c"),
            "TMP": str(base), "TEMP": str(base), "TMPDIR": str(base), "PYTHONDONTWRITEBYTECODE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        initial = Runtime(self.base / "h", config={})
        self.project = initial.project(self.primary)
        initial.close()
        self.approved = {"linked_workspaces": {self.project: [str(self.linked)]}, "admin": {"enabled": False}}
        self.write(self.approved)
        self.runtime = Runtime(self.base / "h")
        self.addCleanup(self.runtime.close)

    def write(self, value):
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def state(self):
        return workspace_links.workspace_links_status(self.runtime, self.project, self.primary)

    def assert_closed(self, calls=3):
        with patch.object(workspace_links, "_checked_root", side_effect=AssertionError("former root observed")):
            for _ in range(calls):
                result = self.state()
                self.assertEqual(len(result["roots"]), 1)
                self.assertTrue(result["reload_required"])

    def test_unchanged_large_invalid_json_or_shape_is_read_only_once(self):
        for bad in ('{"padding":"' + "x" * 900_000, json.dumps({"linked_workspaces": [], "padding": "x" * 900_000})):
            with self.subTest(kind=bad[-10:]):
                self.config.write_text(bad, encoding="utf-8")
                with patch.object(workspace_links, "_read_config", wraps=workspace_links._read_config) as read:
                    self.assert_closed(20)
                self.assertEqual(read.call_count, 1)

    def test_negative_cache_has_no_exception_text_or_stale_approved_config(self):
        with patch.object(workspace_links, "_read_config", side_effect=ContractError("private evidence never cache this")) as read:
            self.assert_closed()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.runtime._workspace_links_disk_cache, {})
        cached = {key: value for key, value in vars(self.runtime).items() if key.startswith("_workspace_links_disk_")}
        self.assertNotIn("private evidence", repr(cached))
        self.assertEqual(cached["_workspace_links_disk_error"], "invalid")

    def test_corrected_signature_is_revalidated_without_restarting_runtime(self):
        self.write({"linked_workspaces": []})
        self.assert_closed()
        self.write({**self.approved, "unrelated": "new settings"})
        with patch.object(workspace_links, "_read_config", wraps=workspace_links._read_config) as read:
            for _ in range(3):
                self.assertEqual(len(self.state()["roots"]), 2)
        self.assertEqual(read.call_count, 1)

    def test_revoked_or_deleted_configuration_is_empty_cached_and_never_observes_old_root(self):
        self.assertEqual(len(self.state()["roots"]), 2)
        self.config.unlink()
        with patch.object(workspace_links, "_read_config", wraps=workspace_links._read_config) as read:
            self.assert_closed()
        self.assertEqual(read.call_count, 1)
        self.assertIsNone(self.runtime._workspace_links_disk_signature)
        self.assertIsNone(self.runtime._workspace_links_disk_error)
        self.write(self.approved)
        self.assertEqual(len(self.state()["roots"]), 2)

    def test_unavailable_stat_does_not_trust_cache_or_block_restored_access(self):
        self.assertEqual(len(self.state()["roots"]), 2)
        with patch.object(workspace_links, "_config_signature", return_value=("unavailable",)), \
                patch.object(workspace_links, "_read_config", wraps=workspace_links._read_config) as read:
            self.assert_closed(4)
        self.assertEqual(read.call_count, 0)
        self.assertEqual(len(self.state()["roots"]), 2)

    def test_read_permission_restoration_retries_even_when_stat_signature_is_unchanged(self):
        original = workspace_links._read_config
        clock = [0.0]
        attempts = [0]

        def denied_then_available(path):
            attempts[0] += 1
            if attempts[0] == 1:
                raise PermissionError("private path or OS error")
            return original(path)

        with patch.object(workspace_links, "_read_config", side_effect=denied_then_available), \
                patch.object(workspace_links.time, "monotonic", side_effect=lambda: clock[0]):
            self.assert_closed(4)
            self.assertEqual(attempts[0], 1)
            clock[0] = workspace_links.CONFIG_IO_RETRY_SECONDS + .01
            self.assertEqual(len(self.state()["roots"]), 2)
            self.assertEqual(attempts[0], 2)

    def test_successful_read_with_signature_change_is_not_accepted_or_cached_under_new_signature(self):
        original = workspace_links._read_config
        attempts = [0]

        def changed_during_read(path):
            attempts[0] += 1
            result = original(path)
            if attempts[0] == 1:
                self.write({"linked_workspaces": {}})
            return result

        with patch.object(workspace_links, "_read_config", side_effect=changed_during_read):
            self.assert_closed(2)
        self.assertEqual(attempts[0], 2)
        self.assertEqual(self.runtime._workspace_links_disk_cache, {"linked_workspaces": {}})
        self.assertIsNone(self.runtime._workspace_links_disk_error)

    def test_failed_half_write_followed_by_valid_config_does_not_poison_new_signature(self):
        self.config.write_text('{"linked_workspaces":', encoding="utf-8")
        original = workspace_links._read_config
        attempts = [0]

        def completed_half_write(path):
            attempts[0] += 1
            if attempts[0] == 1:
                try:
                    return original(path)
                finally:
                    self.write(self.approved)
            return original(path)

        with patch.object(workspace_links, "_read_config", side_effect=completed_half_write):
            self.assert_closed(1)
            self.assertEqual(len(self.state()["roots"]), 2)
        self.assertEqual(attempts[0], 2)

    def test_unrelated_admin_edit_during_read_is_validated_then_preserves_loaded_roots(self):
        original = workspace_links._read_config
        attempts = [0]

        def changed_during_read(path):
            attempts[0] += 1
            result = original(path)
            if attempts[0] == 1:
                self.write({**self.approved, "admin": {"enabled": False, "max_calls_per_day": 13}})
            return result

        with patch.object(workspace_links, "_read_config", side_effect=changed_during_read):
            self.assert_closed(1)
            result = self.state()
        self.assertEqual(len(result["roots"]), 2)
        self.assertFalse(result["reload_required"])
        self.assertEqual(attempts[0], 2)


if __name__ == "__main__":
    unittest.main()
