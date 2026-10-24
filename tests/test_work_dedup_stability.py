"""Display deduplication survives progress writes without weakening fences."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, digest, encode


class WorkDedupStabilityTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build/local/work-dedup-tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tmp", "home", "runtime-releases", "codex"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex")}
        environment.update({key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        (self.workspace / "ledger.py").write_text("revision_fence = True\n", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "ledgerfence preserve revision ordering", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]

    def observe(self, *, file=False, message=None):
        task = self.r.task(self.p, "reader")
        if file:
            payload = {"tool_name": "Write", "tool_input": {"file_path": "ledger.py"},
                "tool_response": {"success": True, "output": message or "ledgerfence implementation observed"}}
            kind, outcome = "PostToolUse", "succeeded"
        else:
            payload = {"last_assistant_message": message or "ledgerfence preserve revision ordering; acceptance remains pending."}
            kind, outcome = "Stop", "reported"
        with self.r.transaction():
            event, _ = self.r._event(self.p, "reader", kind, payload, outcome=outcome,
                task_id=task["id"], task_revision=task["revision"], task_write_version=task["write_version"])
            result = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return result["memory_id"]

    def items(self, task=None, **kwargs):
        return self.r.work_context_items(self.p, "reader", "ledgerfence", workspace=self.workspace,
            task_state=task if task is not None else self.r.task(self.p, "reader"), **kwargs)

    def show(self, *, task=None, **kwargs):
        selected = self.items(task, **kwargs)
        self.assertTrue(selected)
        self.r.work_record_exposure(self.p, "reader", selected,
            task_state=task if task is not None else self.r.task(self.p, "reader"),
            delivery=kwargs.get("delivery", "persistent"), context_epoch=kwargs.get("context_epoch", 0))
        return selected

    def context(self, **kwargs):
        return self.r._context(self.p, "reader", query="ledgerfence", context_handle=self.handle,
            workspace=self.workspace, **kwargs)

    def test_progress_status_write_does_not_reinject_unchanged_history(self):
        ident = self.observe()
        self.assertIn(ident, self.context())
        before = self.r.task(self.p, "reader")
        after = self.r.task_update(self.p, "reader", before["goal"], status="delivered")
        self.assertEqual(after["revision"], before["revision"])
        self.assertGreater(after["write_version"], before["write_version"])
        self.assertNotIn(ident, self.context())

    def test_legacy_receipt_ignores_only_progress_write_version(self):
        self.observe()
        before = self.r.task(self.p, "reader")
        self.show()
        legacy = {key: before.get(key) for key in ("id", "revision", "write_version", "assignment_id")}
        self.r.db.execute("UPDATE work_exposures SET task_key=?", (json.dumps(legacy),))
        current = dict(before, write_version=before["write_version"] + 9)
        self.assertEqual(self.items(current), [])

    def test_healthy_legacy_source_fingerprint_does_not_create_upgrade_notice(self):
        ident = self.observe()
        self.show()
        row = self.r.db.execute("SELECT * FROM work_memories WHERE id=?", (ident,)).fetchone()
        sources = self.r.db.execute("SELECT event_id FROM work_memory_sources WHERE memory_id=? ORDER BY event_id", (ident,)).fetchall()
        snapshots = [self.r.work_source_snapshot(self.p, source["event_id"]) for source in sources]
        legacy_version = digest([row["revision"], row["body"], snapshots, []])
        self.r.db.execute("UPDATE work_exposures SET source_version=? WHERE memory_id=?", (legacy_version, ident))
        self.assertEqual(self.r.work_exposure_updates(self.p, "reader", workspace=self.workspace), [])
        self.assertEqual(self.items(), [])

    def test_task_contract_revision_change_can_present_history_again(self):
        ident = self.observe()
        self.assertIn(ident, self.context())
        before = self.r.task(self.p, "reader")
        after = self.r.task_update(self.p, "reader", before["goal"], {"mode": "implementation"})
        self.assertGreater(after["revision"], before["revision"])
        self.assertIn(ident, self.context())

    def test_different_task_identity_can_present_history_again(self):
        self.observe()
        current = self.r.task(self.p, "reader")
        self.show(task=current)
        self.assertTrue(self.items(dict(current, id="different-task")))

    def test_assignment_change_can_present_history_again(self):
        self.observe()
        current = dict(self.r.task(self.p, "reader"), assignment_id="assignment-one")
        self.show(task=current)
        self.assertEqual(self.items(dict(current, write_version=current["write_version"] + 1)), [])
        self.assertTrue(self.items(dict(current, assignment_id="assignment-two")))

    def test_missing_assignment_is_not_equal_to_a_known_assignment(self):
        self.observe()
        current = self.r.task(self.p, "reader")
        self.show(task=current)
        self.assertTrue(self.items(dict(current, assignment_id="assignment-one")))

    def test_corrupt_legacy_task_key_does_not_suppress_history(self):
        self.observe()
        self.show()
        self.r.db.execute("UPDATE work_exposures SET task_key='not-json'")
        self.assertTrue(self.items())

    def test_legacy_assignment_is_not_ignored_with_progress_version(self):
        self.observe()
        current = self.r.task(self.p, "reader")
        self.show(task=current)
        legacy = dict(current, assignment_id="previous-assignment")
        self.r.db.execute("UPDATE work_exposures SET task_key=?", (json.dumps(legacy),))
        self.assertTrue(self.items(current))

    def test_new_context_epoch_can_present_history_again(self):
        self.observe()
        self.show(context_epoch=3)
        self.assertEqual(self.items(context_epoch=3), [])
        self.assertTrue(self.items(context_epoch=4))

    def test_real_compaction_can_restore_the_current_handoff(self):
        ident = self.observe()
        self.assertIn(ident, self.context())
        self.assertNotIn(ident, self.context())
        self.assertIn(ident, self.context(compact=True))

    def test_ephemeral_delivery_is_presented_on_each_invocation(self):
        ident = self.observe()
        self.assertIn(ident, self.context(delivery="ephemeral"))
        self.assertIn(ident, self.context(delivery="ephemeral"))

    def test_changed_source_is_still_reported_after_progress_write(self):
        ident = self.observe()
        self.show()
        self.r.db.execute("UPDATE work_memory_sources SET source_hash='source-mutated' WHERE memory_id=?", (ident,))
        current = self.r.task(self.p, "reader")
        found = self.items(dict(current, write_version=current["write_version"] + 1))
        changed = next(item for item in found if item["memory_id"] == ident)
        self.assertTrue(changed["source_changed"])
        self.assertEqual(changed["state"], "source_changed")
        self.r.work_record_exposure(self.p, "reader", found, task_state=current)
        self.assertEqual(self.items(current), [])

    def test_missing_source_is_still_reported_after_progress_write(self):
        ident = self.observe()
        self.show()
        self.r.db.execute("DELETE FROM events WHERE id=(SELECT event_id FROM work_memories WHERE id=?)", (ident,))
        current = self.r.task(self.p, "reader")
        found = self.items(dict(current, write_version=current["write_version"] + 1))
        changed = next(item for item in found if item["memory_id"] == ident)
        self.assertTrue(changed["source_changed"])
        self.assertEqual(changed["state"], "missing")

    def test_repaired_capture_health_is_not_hidden_by_the_old_healthy_version(self):
        ident = self.observe()
        self.show()
        original = self.r.db.execute("SELECT source_hash FROM work_memory_sources WHERE memory_id=?", (ident,)).fetchone()[0]
        self.r.db.execute("UPDATE work_memory_sources SET source_hash='source-mutated' WHERE memory_id=?", (ident,))
        self.r.work_record_exposure(self.p, "reader", self.items(), task_state=self.r.task(self.p, "reader"))
        self.r.db.execute("UPDATE work_memory_sources SET source_hash=? WHERE memory_id=?", (original, ident))
        found = self.items()
        repaired = next(item for item in found if item["memory_id"] == ident)
        self.assertTrue(repaired["source_changed"])
        self.assertEqual(repaired["state"], "reported")
        self.r.work_record_exposure(self.p, "reader", found, task_state=self.r.task(self.p, "reader"))
        self.assertEqual(self.items(), [])

    def test_changed_file_is_still_reported_after_progress_write(self):
        ident = self.observe(file=True)
        self.show()
        (self.workspace / "ledger.py").write_text("revision_fence = False\nchanged = True\n", encoding="utf-8")
        current = self.r.task(self.p, "reader")
        found = self.items(dict(current, write_version=current["write_version"] + 1))
        changed = next(item for item in found if item["memory_id"] == ident)
        self.assertTrue(changed["file_state_changed"])

    def test_stale_progress_fence_is_still_rejected_by_task_updates(self):
        self.observe()
        self.show()
        before = self.r.task(self.p, "reader")
        self.r.task_update(self.p, "reader", before["goal"], status="delivered")
        with self.assertRaisesRegex(ContractError, "stale task write version"):
            self.r.task_update(self.p, "reader", before["goal"], status="paused", context_handle=self.handle,
                expected_task_id=before["id"], expected_revision=before["revision"],
                expected_write_version=before["write_version"])

    def test_mcp_completed_sections_keep_small_receipts_after_progress_only_write(self):
        ident = self.observe(message="ledgerfence preserve the observed condition. " * 25)
        server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        self.addCleanup(server.close)
        originals = {}
        for section in ("summary", "evidence"):
            originals[section] = server.call("work_memory_read", {
                "memory_id": ident, "section": section, "max_chars": 10000})
            self.assertFalse(originals[section]["truncated"])
        before = self.r.task(self.p, "reader")
        after = self.r.task_update(self.p, "reader", before["goal"], status="delivered")
        self.assertEqual(after["revision"], before["revision"])
        self.assertGreater(after["write_version"], before["write_version"])
        for section in ("summary", "evidence"):
            with self.subTest(section=section):
                receipt = server.call("work_memory_read", {
                    "memory_id": ident, "section": section, "max_chars": 10000})
                self.assertTrue(receipt["already_read"])
                self.assertTrue(receipt["content_omitted"])
                self.assertNotIn("content", receipt)
                self.assertFalse(receipt["model_read_confirmed"])
                self.assertLess(len(encode(receipt)), len(encode(originals[section])))

    def test_mcp_explicit_reread_still_returns_full_section_after_progress_write(self):
        ident = self.observe(message="ledgerfence explicit replay preserves the complete original. " * 20)
        server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        self.addCleanup(server.close)
        arguments = {"memory_id": ident, "section": "evidence", "max_chars": 10000}
        original = server.call("work_memory_read", arguments)
        before = self.r.task(self.p, "reader")
        self.r.task_update(self.p, "reader", before["goal"], status="delivered")
        self.assertTrue(server.call("work_memory_read", arguments)["already_read"])
        replay = server.call("work_memory_read", dict(arguments, reread=True))
        self.assertEqual(replay["content"], original["content"])
        self.assertNotIn("content_omitted", replay)
        self.assertTrue(server.call("work_memory_read", arguments)["already_read"])

    def test_mcp_progress_stability_does_not_hide_a_later_file_change(self):
        ident = self.observe(file=True)
        server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        self.addCleanup(server.close)
        arguments = {"memory_id": ident, "section": "evidence", "max_chars": 10000}
        original = server.call("work_memory_read", arguments)
        before = self.r.task(self.p, "reader")
        self.r.task_update(self.p, "reader", before["goal"], status="delivered")
        self.assertTrue(server.call("work_memory_read", arguments)["already_read"])
        (self.workspace / "ledger.py").write_text("changed_file_after_status = True\n", encoding="utf-8")
        changed = server.call("work_memory_read", arguments)
        self.assertIn("content", changed)
        self.assertNotIn("already_read", changed)
        self.assertNotEqual(changed["file_check"], original["file_check"])


if __name__ == "__main__":
    unittest.main()
