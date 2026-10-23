"""Automatic history selects useful evidence without changing the audit store."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.runtime import Runtime


class WorkAutomaticSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-auto-work-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        (self.workspace / "ledger.py").write_text("ledgerfence = True\n", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "ledgerfence revision invariants", {"mode": "analysis"})

    def record(self, kind="Stop", text="ledgerfence: preserve revision ordering.", *,
               response=None, workspace=None, conversation="reader", completion="complete", task=None):
        workspace = workspace or self.workspace
        task = task or self.r.task(self.p, conversation)
        payload = {"last_assistant_message": text, "completion": completion} if kind == "Stop" else {
            "tool_name": "Write" if kind == "write" else "run_public_tests",
            "tool_input": {"file_path": "ledger.py"}, "tool_response": response}
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, "Stop" if kind == "Stop" else "PostToolUse",
                payload, task_id=task["id"] if task else None, task_revision=task["revision"] if task else None,
                task_write_version=task.get("write_version") if task else None,
                outcome="reported" if kind == "Stop" else "succeeded")
            created = self.r._work_capture_event(self.p, event["id"], workspace, raw=payload)
        return created["memory_id"]

    def items(self, query="", **kwargs):
        return self.r.work_context_items(self.p, "reader", query, workspace=self.workspace,
            task_state=self.r.task(self.p, "reader"), **kwargs)

    @staticmethod
    def receipt(index=0):
        return {"written": True, "exit_code": 0, "path": "ledger.py", "chars": 123,
                "sha256": str(index).zfill(64)}

    def test_write_receipt_remains_searchable_without_automatic_file_injection(self):
        ident = self.record("write", response=self.receipt())
        manual = self.r.work_search(self.p, file_path="ledger.py", workspace=self.workspace)
        self.assertIn(ident, [item["memory_id"] for item in manual["items"]])
        items = self.items(file_only=True, raw={"tool_name": "Read", "tool_input": {"path": "ledger.py"}})
        self.assertNotIn(ident, [item["memory_id"] for item in items])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 1)

    def test_receipt_burst_does_not_hide_older_file_verification(self):
        verified = self.record("verify", response={"exit_code": 0, "stdout": "ledgerfence rollback: 12 tests passed"})
        receipts = {self.record("write", response=self.receipt(index)) for index in range(35)}
        items = self.items(limit=1, file_only=True,
            raw={"tool_name": "Read", "tool_input": {"path": "ledger.py"}})
        self.assertEqual([item["memory_id"] for item in items], [verified])
        self.assertFalse(receipts & {item["memory_id"] for item in items})

    def test_substantive_write_output_and_unknown_fields_are_not_discarded(self):
        for response in ({"written": True, "output": "ledgerfence: reject late revision conflicts"},
                         {"written": True, "warning": "ledgerfence rollback remains unverified"},
                         {"written": True, "updated": "ledgerfence: preserve rollback invariants"},
                         {"written": True, "success": False, "exit_code": 0}):
            with self.subTest(response=response):
                ident = self.record("write", response=response)
                items = self.items(file_only=True, raw={"tool_name": "Read", "tool_input": {"path": "ledger.py"}})
                self.assertIn(ident, [item["memory_id"] for item in items])

    def test_public_result_wins_an_equal_match_over_write_output(self):
        result = self.record(text="ledgerfence: preserve revision ordering and reject conflicts.")
        self.record("write", response={"exit_code": 0, "output": "ledgerfence implementation written"})
        items = self.items("ledgerfence", limit=1)
        self.assertEqual(items[0]["memory_id"], result)
        self.assertEqual(items[0]["validation_level"], "reported")

    def test_compaction_restores_current_phase_handoff_without_new_topic_match(self):
        ident = self.record()
        self.assertNotIn(ident, [item["memory_id"] for item in self.items("settlementnewtopic")])
        restored = self.items("settlementnewtopic", compact=True)
        self.assertIn(ident, [item["memory_id"] for item in restored])
        self.assertEqual(restored[0]["validation_level"], "reported")
        self.assertFalse(restored[0]["execution_authority"])

    def test_handoff_does_not_cross_task_revision_or_checkout(self):
        old = self.record()
        self.r.task_update(self.p, "reader", "unrelated phase", new_task=True)
        sibling = self.root / "sibling"
        sibling.mkdir()
        other_checkout = self.record(workspace=sibling)
        restored = self.items("unmatchedtopic", compact=True)
        self.assertFalse({old, other_checkout} & {item["memory_id"] for item in restored})

    def test_partial_handoff_and_unbound_agent_are_not_restored(self):
        partial = self.record(completion="partial")
        self.assertNotIn(partial, [item["memory_id"] for item in self.items("unmatchedtopic", compact=True)])
        complete = self.record()
        rows = self.r._work_search_rows(self.p, "", workspace=self.workspace, conversation="reader",
            agent_id="child", task_state=self.r.task(self.p, "reader"), automatic=True, handoff=True)
        self.assertEqual(rows, [])
        self.assertNotEqual(complete, partial)

    def test_query_excerpt_survives_final_source_validation(self):
        ident = self.record(text="phase notes " * 30 + "\nHALF_EVEN: preserve exact settlement rounding.\n" + "remaining notes " * 25)
        items = self.items("HALF_EVEN")
        self.assertEqual(items[0]["memory_id"], ident)
        refreshed = self.r.work_validate_items(self.p, items, workspace=self.workspace)
        self.assertIn("HALF_EVEN", refreshed[0]["summary"])
        self.assertLessEqual(len(refreshed[0]["summary"]), 80)
        self.assertEqual(refreshed[0]["source_version"], items[0]["source_version"])

    def test_tool_projection_marks_truncation_and_preserves_original_result_tail(self):
        ident = self.record("verify", response={"exit_code": 0, "stdout": "progress " * 250 + "\nRan 12 tests: OK"})
        row = self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (ident,)).fetchone()
        body = json.loads(row["body"])
        self.assertTrue(body["coverage"]["truncated"])
        self.assertLessEqual(len(body["summary"]), 700)
        self.assertIn("Ran 12 tests: OK", body["summary"])
        self.assertEqual(body["validation_level"], "tool_observed")

    def test_derived_summary_is_not_excluded_with_the_current_prompt(self):
        original = self.record()
        source = self.r.db.execute("SELECT event_id FROM work_memories WHERE id=?", (original,)).fetchone()[0]
        task = self.r.task(self.p, "reader")
        summary = self.r.record_work_summary(self.p, "derived-phase", {
            "title": "ledgerfence phase summary", "summary": "ledgerfence: preserve revision ordering."},
            [self.r.work_source_snapshot(self.p, source)], "reader", task_id=task["id"], task_revision=task["revision"])
        found = self.items("ledgerfence", exclude_event_id="current-prompt-event")
        self.assertIn(summary["memory_id"], [item["memory_id"] for item in found])
        self.assertEqual(next(item for item in found if item["memory_id"] == summary["memory_id"])["validation_level"], "model_reported")

    def test_previously_exposed_receipt_still_warns_when_its_file_changes(self):
        ident = self.record("write", response=self.receipt())
        shown = self.r.work_search(self.p, file_path="ledger.py", workspace=self.workspace)["items"]
        self.r.work_record_exposure(self.p, "reader", shown)
        (self.workspace / "ledger.py").write_text("new and different file version\n", encoding="utf-8")
        items = self.items(file_only=True, raw={"tool_name": "Read", "tool_input": {"path": "ledger.py"}})
        changed = next(item for item in items if item["memory_id"] == ident)
        self.assertTrue(changed["file_state_changed"])
        self.r.work_record_exposure(self.p, "reader", items)
        self.assertEqual(self.items(file_only=True, raw={"tool_name": "Read", "tool_input": {"path": "ledger.py"}}), [])

    def test_small_budget_does_not_acknowledge_unrendered_phase_handoff(self):
        ident = self.record()
        self.r.config["injection_max_chars"] = 128
        handle = self.r.register_context(self.p, "reader")["context_handle"]
        text = self.r._context(self.p, "reader", query="unmatchedtopic", compact=True,
            context_handle=handle, workspace=self.workspace)
        self.assertNotIn(ident, text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)
        self.r.config["injection_max_chars"] = 2400
        text = self.r._context(self.p, "reader", query="unmatchedtopic", compact=True,
            context_handle=handle, workspace=self.workspace)
        self.assertIn(ident, text)
        self.assertLessEqual(len(text), 2400)


if __name__ == "__main__":
    unittest.main()
