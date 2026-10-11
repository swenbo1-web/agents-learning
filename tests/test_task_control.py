import contextlib
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.runtime import ContractError, Runtime


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    @staticmethod
    def expectation(task):
        return {"expected_task_id": task["id"], "expected_write_version": task["write_version"]}

    def test_reused_revision_cannot_overwrite_new_task(self):
        first = self.r.task_update(self.p, "chat", "task A")
        second = self.r.task_update(self.p, "chat", "task B", new_task=True, **self.expectation(first))
        self.assertEqual((first["revision"], second["revision"]), (1, 1))
        self.assertEqual(second["write_version"], first["write_version"] + 1)
        with self.assertRaisesRegex(ContractError, "stale task identity"):
            self.r.task_update(self.p, "chat", "stale A", expected_revision=1, **self.expectation(first))
        with self.assertRaisesRegex(ContractError, "task_expectation_required"):
            self.r.task_update(self.p, "chat", "stale A", expected_revision=1)
        self.assertEqual(self.r.task(self.p, "chat"), second)

    def test_cancelled_state_cannot_be_overwritten_by_stale_write(self):
        first = self.r.task_update(self.p, "chat", "task")
        cancelled = self.r.task_update(self.p, "chat", "task", status="cancelled", **self.expectation(first))
        self.assertEqual(cancelled["revision"], first["revision"])
        with self.assertRaisesRegex(ContractError, "stale task write version"):
            self.r.task_update(self.p, "chat", "task", status="active", resume=True, **self.expectation(first))
        self.assertEqual(self.r.task(self.p, "chat")["status"], "cancelled")
        self.assertEqual(cancelled["execution_epoch"], first["execution_epoch"] + 1)

    def test_omitted_status_preserves_inactive_task_and_resume_is_explicit(self):
        first = self.r.task_update(self.p, "chat", "task")
        paused = self.r.task_update(self.p, "chat", "task", status="paused", **self.expectation(first))
        corrected = self.r.task_update(self.p, "chat", "corrected task", **self.expectation(paused))
        self.assertEqual(corrected["status"], "paused")
        self.assertEqual(corrected["revision"], 2)
        self.assertEqual(corrected["execution_epoch"], paused["execution_epoch"] + 1)
        with self.assertRaisesRegex(ContractError, "resume_required"):
            self.r.task_update(self.p, "chat", "corrected task", status="active", **self.expectation(corrected))
        resumed = self.r.task_update(self.p, "chat", "corrected task", resume=True, **self.expectation(corrected))
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["revision"], corrected["revision"])
        self.assertEqual(resumed["write_version"], corrected["write_version"] + 1)
        self.assertEqual(resumed["execution_epoch"], corrected["execution_epoch"] + 1)

    def test_noop_keeps_all_versions_and_creates_no_event(self):
        first = self.r.task_update(self.p, "chat", "task", {"mode": "analysis"})
        event_count = self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        same = self.r.task_update(self.p, "chat", "task", **self.expectation(first))
        self.assertEqual(same, first)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM task_states").fetchone()[0], 1)

    def test_snapshot_preserves_contract_status_and_exact_event_time_state(self):
        first = self.r.task_update(self.p, "chat", "task")
        cancelled = self.r.task_update(self.p, "chat", "task", status="cancelled")
        default = self.r.task_snapshot(self.p, first["id"], first["revision"])
        at_start = self.r.task_snapshot(self.p, first["id"], first["revision"], first["write_version"])
        at_cancel = self.r.task_snapshot(self.p, first["id"], first["revision"], cancelled["write_version"])
        self.assertEqual(default["status"], "active")
        self.assertEqual(at_start["status"], "active")
        self.assertEqual(at_cancel["status"], "cancelled")
        self.assertEqual(at_cancel["write_version"], cancelled["write_version"])
        new = self.r.task_update(self.p, "chat", "new", new_task=True)
        self.assertIsNone(self.r.task_snapshot(self.p, first["id"], 1, new["write_version"]))
        self.assertEqual(self.r.task_snapshot(self.p, first["id"], 1)["status"], "active")
        payload = json.loads(self.r.db.execute("SELECT payload FROM events WHERE type='TaskUpdate' ORDER BY created DESC LIMIT 1").fetchone()[0])
        self.assertEqual(payload["write_version"], new["write_version"])
        self.assertEqual(payload["execution_epoch"], new["execution_epoch"])

    def test_mcp_handle_requires_identity_and_write_version_for_existing_task(self):
        handle = self.r.register_context(self.p, "chat")["context_handle"]
        first = self.r.task_update(self.p, "chat", "task", expected_revision=0, context_handle=handle)
        with self.assertRaisesRegex(ContractError, "task_expectation_required"):
            self.r.task_update(self.p, "chat", "changed", context_handle=handle)
        with self.assertRaisesRegex(ContractError, "task_expectation_required"):
            self.r.task_update(self.p, "chat", "changed", context_handle=handle, expected_task_id=first["id"])
        changed = self.r.task_update(self.p, "chat", "changed", context_handle=handle, **self.expectation(first))
        self.assertEqual(changed["goal"], "changed")
        self.assertEqual(changed["write_version"], 2)

    def test_initial_expectations_and_failed_session_binding_are_atomic(self):
        with self.assertRaisesRegex(ContractError, "stale task revision"):
            self.r.task_update(self.p, "chat", "task", expected_revision=1)
        self.assertIsNone(self.r.db.execute("SELECT project FROM session_projects WHERE conversation='chat'").fetchone())
        task = self.r.task_update(self.p, "chat", "task", expected_revision=0, expected_write_version=0)
        self.assertEqual(task["write_version"], 1)
        for value in (True, -1, "1"):
            with self.assertRaises(ContractError):
                self.r.task_update(self.p, "chat", "task", expected_write_version=value, expected_task_id=task["id"])

    def test_receipt_is_read_inside_its_own_transaction(self):
        first = self.r.task_update(self.p, "chat", "first")
        other = Runtime(self.home)
        original_transaction = self.r.transaction

        @contextlib.contextmanager
        def interleaved_commit():
            with original_transaction():
                yield
            # 模拟 COMMIT 后另一连接抢先改写当前行。
            other.task_update(self.p, "chat", "concurrent task", new_task=True)

        try:
            self.r.transaction = interleaved_commit
            receipt = self.r.task_update(self.p, "chat", "my update", **self.expectation(first))
            self.assertEqual(receipt["id"], first["id"])
            self.assertEqual(receipt["goal"], "my update")
            self.assertEqual(receipt["write_version"], 2)
            current = self.r.task(self.p, "chat")
            self.assertNotEqual(current["id"], receipt["id"])
            self.assertEqual(current["write_version"], 3)
        finally:
            self.r.transaction = original_transaction
            other.close()


if __name__ == "__main__":
    unittest.main()
