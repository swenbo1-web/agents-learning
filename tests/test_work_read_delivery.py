"""MCP full-section receipts retain live source, identity and pagination checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode
from agent_learning.work_read_delivery import WorkReadDelivery


class WorkReadDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-read-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.file = self.workspace / "marker.py"
        self.file.write_text("original", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "inspect marker", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]
        self.server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        payload = {"tool_name": "write_file", "tool_input": {"path": "marker.py"},
                   "tool_response": {"exit_code": 0, "output": "marker evidence " + "a" * 2400}}
        with self.r.transaction():
            self.event, _ = self.r._event(self.p, "past", "PostToolUse", payload, outcome="succeeded")
            memory = self.r._work_capture_event(self.p, self.event["id"], self.workspace, raw=payload)
        self.memory_id = memory["memory_id"]

    def read(self, **kwargs):
        return self.server.call("work_memory_read", {"memory_id": self.memory_id, **kwargs})

    def test_complete_then_larger_budget_returns_small_receipt_and_force_reread(self):
        first = self.read(max_chars=10000, section="evidence")
        self.assertFalse(first["truncated"])
        repeated = self.read(max_chars=12000, section="evidence")
        self.assertEqual(repeated["status"], "ok")
        self.assertTrue(repeated["already_read"])
        self.assertTrue(repeated["content_omitted"])
        self.assertNotIn("content", repeated)
        self.assertFalse(repeated["model_read_confirmed"])
        self.assertLess(len(encode(repeated)), len(encode(first)) / 2)
        self.assertEqual(first, self.read(max_chars=10000, section="evidence", reread=True))
        self.assertTrue(self.read(max_chars=1024, section="evidence")["already_read"])
        self.assertLessEqual(len(encode(self.read(max_chars=1024, section="evidence"))), 1024)

    def test_partial_read_never_counts_as_complete_and_contiguous_pages_do(self):
        first = self.read(section="evidence", max_chars=1300)
        self.assertTrue(first["truncated"])
        again = self.read(section="evidence", max_chars=1300)
        self.assertEqual(first, again)
        contents = first["content"]
        current = first
        while current["next_offset"] is not None:
            current = self.read(section="evidence", max_chars=1300,
                offset=current["next_offset"], expected_revision=first["revision"])
            self.assertNotIn("already_read", current)
            contents += current["content"]
        self.assertIn(self.event["id"], contents)
        self.assertTrue(self.read(section="evidence")["already_read"])
        with self.assertRaisesRegex(ContractError, "expected_revision"):
            self.read(section="evidence", offset=1)

    def test_sections_have_independent_receipts(self):
        self.read(section="summary")
        self.assertNotIn("already_read", self.read(section="evidence", max_chars=10000))
        self.assertTrue(self.read(section="summary")["already_read"])
        self.assertTrue(self.read(section="evidence")["already_read"])

    def test_source_state_change_returns_body_even_after_complete(self):
        self.read()
        self.assertTrue(self.read()["already_read"])
        with self.r.transaction():
            self.r.db.execute("UPDATE events SET outcome='failed' WHERE id=?", (self.event["id"],))
        changed = self.read()
        self.assertEqual(changed["state"], "failed")
        self.assertIn("content", changed)

    def test_file_metadata_change_returns_body_and_current_file_state(self):
        first = self.read()
        self.assertFalse(first["files"][0]["version_changed"])
        self.assertTrue(self.read()["already_read"])
        self.file.write_text("a new substantially longer file version", encoding="utf-8")
        changed = self.read()
        self.assertIn("content", changed)
        self.assertTrue(changed["files"][0]["version_changed"])
        self.assertNotEqual(first["file_state"], changed["file_state"])

    def test_source_payload_change_and_revision_change_are_not_cached(self):
        first = self.read()
        with self.r.transaction():
            self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (
                json.dumps({"tool_response": {"output": "updated source"}}), self.event["id"]))
        changed = self.read()
        self.assertIn("content", changed)
        self.assertTrue(changed["source_changed"])
        with self.r.transaction():
            self.r.db.execute("UPDATE work_memories SET revision=revision+1 WHERE id=?", (self.memory_id,))
        with self.assertRaisesRegex(ContractError, "revision changed"):
            self.read(expected_revision=first["revision"])
        self.assertIn("content", self.read())

    def test_compaction_restores_full_read(self):
        self.read()
        self.assertTrue(self.read()["already_read"])
        self.r._context(self.p, "reader", query="marker", context_handle=self.handle,
                        workspace=self.workspace, compact=True)
        self.assertIn("content", self.read())

    def test_task_change_restores_full_read(self):
        self.read()
        self.r.task_update(self.p, "reader", "different task", {"mode": "analysis"})
        self.assertIn("content", self.read())

    def test_new_connection_has_no_receipt_and_close_clears_receipts(self):
        self.read()
        other = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        self.assertIn("content", other.call("work_memory_read", {"memory_id": self.memory_id}))
        self.assertTrue(self.server.work_read_delivery.receipts)
        self.server.close()
        self.assertFalse(self.server.work_read_delivery.receipts)

    def test_identity_and_account_checks_still_run_on_complete_reads(self):
        self.read()
        other = self.r.register_context(self.p, "other")["context_handle"]
        with self.assertRaisesRegex(ContractError, "context_mismatch"):
            self.read(context_handle=other)
        original = self.r.work_read
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.r.account_changed("test-account-change")
            return result
        with patch.object(self.r, "work_read", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.read()

    def test_failed_exposure_does_not_issue_a_completed_receipt(self):
        with patch.object(self.r, "work_record_exposure", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.read()
        self.assertFalse(self.server.work_read_delivery.receipts)
        self.assertIn("content", self.read())

    def test_disabled_work_channel_does_not_reuse_receipt(self):
        self.read()
        self.r.config["work_memory"]["enabled"] = False
        self.assertEqual(self.read()["status"], "disabled")

    def test_agent_handles_cannot_share_receipts(self):
        self.read()
        child_handle = self.r.register_context(self.p, "reader", "child")["context_handle"]
        child = Server(self.r, self.p, context_handle=child_handle, workspace=self.workspace)
        result = child.call("work_memory_read", {"memory_id": self.memory_id})
        self.assertIn("content", result)
        self.assertTrue(child.call("work_memory_read", {"memory_id": self.memory_id})["already_read"])

    def test_antigravity_reuses_only_within_one_verified_invocation(self):
        ag = Runtime(self.root / "home", host="antigravity", config={})
        self.addCleanup(ag.close)
        conversation = ag.host_conversation("native-reader")
        handle = ag.register_context(self.p, conversation)["context_handle"]
        server = Server(ag, self.p, workspace=self.workspace)
        args = {"memory_id": self.memory_id, "context_handle": handle}
        self.assertIn("content", server.call("work_memory_read", args))
        self.assertIn("content", server.call("work_memory_read", args))
        with ag.transaction():
            ag.db.execute("INSERT INTO antigravity_trajectories(project,conversation,last_invocation) VALUES(?,?,0)",
                          (self.p, conversation))
        self.assertIn("content", server.call("work_memory_read", args))
        self.assertTrue(server.call("work_memory_read", args)["already_read"])
        with ag.transaction():
            ag.db.execute("UPDATE antigravity_trajectories SET last_invocation=1 WHERE project=? AND conversation=?",
                          (self.p, conversation))
        self.assertIn("content", server.call("work_memory_read", args))
        other = ag.register_context(self.p, ag.host_conversation("different-reader"))["context_handle"]
        self.assertIn("content", server.call("work_memory_read", dict(args, context_handle=other)))

    def test_dsh_per_request_contexts_do_not_share_receipts(self):
        dsh = Runtime(self.root / "home", host="dsh", config={})
        self.addCleanup(dsh.close)
        handles = [dsh.register_context(self.p, dsh.host_conversation(name))["context_handle"]
                   for name in ("native-one", "native-two")]
        server = Server(dsh, self.p, workspace=self.workspace)
        args = {"memory_id": self.memory_id, "context_handle": handles[0]}
        self.assertIn("content", server.call("work_memory_read", args))
        self.assertTrue(server.call("work_memory_read", args)["already_read"])
        self.assertIn("content", server.call("work_memory_read", dict(args, context_handle=handles[1])))


class WorkReadReceiptBoundsTests(unittest.TestCase):
    def page(self, memory_id="id", *, offset=0, content="secret observation"):
        return {"status": "ok", "memory_id": memory_id, "revision": 1, "source_version": "v1",
                "state": "reported", "source_changed": False, "validation_level": "reported",
                "section": "summary", "section_length": len(content), "offset": offset, "content": content}

    def test_receipts_are_bounded_and_store_no_evidence_text(self):
        receipts = WorkReadDelivery()
        for i in range(receipts.MAX_RECEIPTS + 1):
            receipts.project(self.page(str(i)), ("scope",))
        self.assertEqual(len(receipts.receipts), receipts.MAX_RECEIPTS)
        self.assertNotIn("secret observation", repr(receipts.receipts))
        self.assertIn("content", receipts.project(self.page("0"), ("scope",)))

    def test_missing_ranges_cannot_become_complete(self):
        receipts = WorkReadDelivery()
        first = dict(self.page(content="aaaaa"), section_length=15)
        last = dict(first, offset=10)
        receipts.project(first, "scope")
        receipts.project(last, "scope")
        self.assertIn("content", receipts.project(first, "scope"))
        receipts.project(dict(first, offset=5), "scope")
        self.assertTrue(receipts.project(first, "scope")["already_read"])


if __name__ == "__main__":
    unittest.main()
