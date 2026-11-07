"""Linked metadata participates in references and discardable delivery coverage."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.candidate_references import _environment_state, _environment_blocked
from agent_learning.context_dedup import ContextDedup


class LinkedReferenceConsumerTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build/local/rc62"
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for name in ("primary", "linked", "home", "tmp", "codex", "runtime-releases"):
            (self.base / name).mkdir()
        isolated = patch.dict(os.environ, {
            "ALP_HOME": str(self.base / "home"), "ALP_CONFIG": str(self.base / "config.json"),
            "ALP_RUNTIME_STORE": str(self.base / "runtime-releases"), "CODEX_HOME": str(self.base / "codex"),
            **{key: str(self.base / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.primary, self.linked = self.base / "primary", self.base / "linked"
        for folder in (self.primary, self.linked):
            (folder / "same.py").write_text("original", encoding="utf-8")
        initial = Runtime(self.base / "home", config={})
        try:
            self.project = initial.project(self.primary)
        finally:
            initial.close()
        self.r = Runtime(self.base / "home", config={"linked_workspaces": {self.project: [str(self.linked)]}})
        self.addCleanup(self.r.close)
        self.events = []
        for folder in (self.primary, self.linked):
            raw = {"tool_name": "read_file", "tool_input": {"path": str(folder / "same.py")},
                   "tool_response": {"exit_code": 0, "output": "Observed the original parser boundary"}}
            with self.r.transaction():
                event, _ = self.r._event(self.project, "source", "PostToolUse", raw, outcome="succeeded")
                self.r._work_capture_safely(self.project, event["id"], self.primary, raw=raw)
            self.events.append(event["id"])
        self.item = {"source_event_refs": self.events}

    def dedup(self):
        return ContextDedup(self.r, self.project, "reader", "", self.primary, {}, 0, "persistent", {})

    def test_candidate_metadata_retains_checkout_and_detects_linked_change(self):
        before = _environment_state(self.r, self.project, self.item, self.primary)
        self.assertEqual(len(before["files"]), 2)
        self.assertEqual(len({item["checkout_key"] for item in before["files"]}), 2)
        self.assertFalse(_environment_blocked(before))
        (self.linked / "same.py").write_text("changed linked parser", encoding="utf-8")
        after = _environment_state(self.r, self.project, self.item, self.primary)
        self.assertTrue(_environment_blocked(after))

    def test_linked_change_invalidates_source_coverage(self):
        before = self.dedup()._sources(self.item)
        self.assertIsNotNone(before)
        (self.linked / "same.py").write_text("changed linked parser", encoding="utf-8")
        self.assertNotEqual(before, self.dedup()._sources(self.item))

    def test_revocation_changes_coverage_without_inspecting_old_root(self):
        before = self.dedup()._sources(self.item)
        self.r.config["linked_workspaces"] = {}
        original = Path.stat
        def guarded(path, *args, **kwargs):
            if path == self.linked or self.linked in path.parents:
                raise AssertionError("revoked linked root was inspected")
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", guarded):
            state = _environment_state(self.r, self.project, self.item, self.primary)
            self.assertTrue(_environment_blocked(state))
            self.assertNotEqual(before, self.dedup()._sources(self.item))


if __name__ == "__main__":
    unittest.main()
