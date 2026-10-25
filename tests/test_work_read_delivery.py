"""MCP full-section receipts retain live source, identity and pagination checks."""
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode
from agent_learning.work_read_delivery import WorkReadDelivery
from agent_learning.work_memory import WORK_READ_FILE_CHECK_LIMIT, _read_file_version
from agent_learning.work_files import normalize_file_target


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

    def test_legacy_incomplete_observation_remains_limited_in_both_short_receipt_paths(self):
        payload = {"tool_name": "write_file", "tool_input": {"path": "marker.py"},
                   "tool_response": {"exit_code": 0, "output": "legacy recovery source"}}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "legacy", "PostToolUse", payload, outcome="succeeded")
            with patch.object(self.r, "_work_index", side_effect=sqlite3.OperationalError("synthetic failure")):
                self.r._work_capture_safely(self.p, event["id"], self.workspace, raw=payload)
            self.r.db.execute("UPDATE work_capture_pending SET capture='{}',next_try=0")
            self.r._work_repair_safely(self.p)
        self.memory_id = self.r.db.execute("SELECT id FROM work_memories WHERE event_id=?", (event["id"],)).fetchone()[0]
        for section in ("summary", "evidence"):
            with self.subTest(section=section):
                first = self.read(section=section, max_chars=10000)
                self.assertTrue(first["file_projection_incomplete"])
                self.assertFalse(first["automatic_eligible"])
                short = self.read(section=section, max_chars=1024)
                self.assertTrue(short["already_read"])
                self.assertTrue(short["file_projection_incomplete"])
                self.assertFalse(short["automatic_eligible"])
                self.assertFalse(short["coverage"]["file_projection_complete"])
                self.assertFalse(short["execution_authority"])
                self.assertLessEqual(len(encode(short)), 1024)
                with patch.object(self.server.work_read_delivery, "completed", return_value=None):
                    fallback = self.read(section=section, max_chars=10000)
                self.assertTrue(fallback["already_read"])
                self.assertTrue(fallback["file_projection_incomplete"])
                self.assertFalse(fallback["automatic_eligible"])
                self.assertTrue(self.read(section=section, max_chars=10000, reread=True)["file_projection_incomplete"])

    def test_partial_original_keeps_coverage_limits_after_section_was_fully_returned(self):
        payload = {"last_assistant_message": "Partial visible explanation. " * 80, "completion": "partial"}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "partial", "Stop", payload, outcome="reported")
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        self.memory_id = memory["memory_id"]
        self.read(section="summary", max_chars=10000)
        receipt = self.read(section="summary", max_chars=1024)
        self.assertTrue(receipt["already_read"])
        self.assertEqual(receipt["completion"], "partial")
        self.assertTrue(receipt["coverage"]["truncated"])
        self.assertFalse(receipt["coverage"]["full_original_available"])
        self.assertFalse(receipt["execution_authority"])
        self.assertLessEqual(len(encode(receipt)), 1024)
        with patch.object(self.server.work_read_delivery, "completed", return_value=None):
            fallback = self.read(section="summary", max_chars=1024)
        self.assertTrue(fallback["already_read"])
        self.assertEqual(fallback["completion"], "partial")
        self.assertEqual(fallback["coverage"], receipt["coverage"])
        self.assertLessEqual(len(encode(fallback)), 1024)

    def test_segment_limits_survive_paging_and_both_receipt_paths(self):
        payload = {"last_assistant_message": "Long public source. " * 500}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "segmented", "Stop", payload, outcome="reported")
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        self.memory_id = self.r.db.execute("SELECT id FROM work_memories WHERE project=? AND conversation=? "
            "AND json_extract(body,'$.coverage.segments')>1 ORDER BY id LIMIT 1", (self.p, "segmented")).fetchone()[0]
        full = self.read(section="summary", max_chars=10000)
        self.assertGreater(full["coverage"]["segments"], 1)
        receipt = self.read(section="summary", max_chars=1024)
        self.assertTrue(receipt["already_read"])
        with patch.object(self.server.work_read_delivery, "completed", return_value=None):
            fallback = self.read(section="summary", max_chars=1024)
        self.assertTrue(fallback["already_read"])
        self.assertEqual(fallback["coverage"], receipt["coverage"])
        self.assertEqual(fallback["coverage"]["segments"], full["coverage"]["segments"])
        self.assertLessEqual(len(encode(fallback)), 1024)
        page = self.read(section="summary", max_chars=1024, reread=True)
        self.assertEqual(page["coverage"], receipt["coverage"])
        self.assertTrue(page["content"])
        self.assertLessEqual(len(encode(page)), 1024)

    def test_complete_receipt_skips_evidence_assembly_but_keeps_live_guards(self):
        with patch.object(self.r, "_work_evidence_text", wraps=self.r._work_evidence_text) as assemble:
            first = self.read(max_chars=10000, section="evidence")
            self.assertEqual(assemble.call_count, 1)
            self.assertTrue(self.read(section="evidence")["already_read"])
            self.assertEqual(assemble.call_count, 1)
            with self.assertRaisesRegex(ContractError, "offset exceeds"):
                self.read(section="evidence", offset=first["section_length"] + 1, expected_revision=first["revision"])
            with self.assertRaisesRegex(ContractError, "revision changed"):
                self.read(section="evidence", expected_revision=first["revision"] + 1)
            self.assertEqual(assemble.call_count, 1)
            self.read(max_chars=10000, section="evidence", reread=True)
            self.assertEqual(assemble.call_count, 2)
            self.file.write_text("changed file state", encoding="utf-8")
            self.assertNotIn("already_read", self.read(max_chars=10000, section="evidence"))
            self.assertEqual(assemble.call_count, 3)

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

    def test_forced_replay_continues_without_repeating_flag(self):
        original = self.read(section="evidence", max_chars=10000)
        current = self.read(section="evidence", max_chars=1400, reread=True)
        self.assertTrue(current["truncated"])
        contents = current["content"]
        while current["next_offset"] is not None:
            current = self.read(section="evidence", max_chars=1400, offset=current["next_offset"],
                                expected_revision=original["revision"])
            self.assertNotIn("already_read", current)
            contents += current["content"]
        self.assertEqual(contents, original["content"])
        self.assertTrue(self.read(section="evidence")["already_read"])

    def test_legacy_replay_flag_on_each_page_keeps_round_coverage(self):
        original = self.read(section="evidence", max_chars=10000)
        current = self.read(section="evidence", max_chars=1400, reread=True)
        contents = current["content"]
        while current["next_offset"] is not None:
            current = self.read(section="evidence", max_chars=1400, offset=current["next_offset"],
                                expected_revision=original["revision"], reread=True)
            contents += current["content"]
        self.assertEqual(contents, original["content"])
        self.assertTrue(self.read(section="evidence")["already_read"])

    def add_files(self, count):
        paths = []
        for index in range(count):
            path = self.workspace / f"extra-{index:03}.py"
            path.write_text(f"file {index}", encoding="utf-8")
            target = normalize_file_target(str(path), self.workspace)
            with self.r.transaction():
                # Each source owns no more than the producer's sixteen links.
                if index % 16 == 0:
                    event, _ = self.r._event(self.p, "past", "PostToolUse", {"index": index},
                                             outcome="succeeded")
                    snapshot = self.r.work_source_snapshot(self.p, event["id"])
                    self.r.db.execute("INSERT INTO work_memory_sources VALUES(?,?,?,?,?)",
                        (self.p, self.memory_id, event["id"], snapshot["capture_digest"], snapshot["source_version"]))
                self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)",
                    (self.p, event["id"], target["checkout_key"], target["relative_path"],
                     target["path_key"], "write_target", json.dumps(target["version"])))
            paths.append(path)
        return paths

    def test_change_beyond_three_visible_files_invalidates_complete_receipt(self):
        paths = self.add_files(4)
        first = self.read(max_chars=10000)
        self.assertEqual(len(first["files"]), 3)
        hidden = next(path for path in paths if path.name not in
                      {item["relative_path"] for item in first["files"]})
        self.assertTrue(first["file_check"]["complete"])
        self.assertTrue(self.read()["already_read"])
        hidden.write_text("a larger changed file beyond display", encoding="utf-8")
        changed = self.read()
        self.assertIn("content", changed)
        self.assertNotEqual(first["file_check"], changed["file_check"])
        self.assertTrue(self.read()["already_read"])

    def test_file_check_limit_disables_receipt_without_unbounded_stats(self):
        self.add_files(WORK_READ_FILE_CHECK_LIMIT)
        with patch("agent_learning.work_memory._read_file_version", wraps=_read_file_version) as observed:
            first = self.read(max_chars=10000)
            repeated = self.read(max_chars=10000)
        self.assertEqual(observed.call_count, 0)
        self.assertEqual(first["file_check"], {"complete": False, "reason": "file_check_limit"})
        self.assertIn("content", repeated)
        self.assertFalse(self.server.work_read_delivery.receipts)

    def test_exact_file_check_limit_checks_all_files_and_reuses(self):
        self.add_files(WORK_READ_FILE_CHECK_LIMIT - 1)
        with patch("agent_learning.work_memory._read_file_version", wraps=_read_file_version) as observed:
            first = self.read(max_chars=10000)
        self.assertEqual(observed.call_count, WORK_READ_FILE_CHECK_LIMIT)
        self.assertTrue(first["file_check"]["complete"])
        self.assertEqual(first["files_omitted"], first["files_count"] - len(first["files"]))
        self.assertTrue(self.read(max_chars=10000)["already_read"])
        self.assertTrue(self.read(max_chars=1024)["already_read"])

    def test_small_budget_multi_file_reads_and_receipts_keep_full_live_check(self):
        self.add_files(2)
        for section in ("summary", "evidence"):
            with self.subTest(section=section):
                complete = self.read(section=section, max_chars=10000)
                receipt = self.read(section=section, max_chars=1024)
                self.assertTrue(receipt["already_read"])
                self.assertEqual(receipt["file_check"], complete["file_check"])
                self.assertLessEqual(len(encode(receipt)), 1024)
                replay = self.read(section=section, max_chars=1024, reread=True)
                self.assertIn("content", replay)
                self.assertTrue(replay["content"])
                self.assertGreater(replay["files_omitted"], 0)
                self.assertEqual(replay["files_omitted"], replay["files_count"] - len(replay["files"]))
                self.assertGreater(replay["file_state_omitted"], 0)
                self.assertEqual(replay["file_check"], complete["file_check"])
                self.assertLessEqual(len(encode(replay)), 1024)
                current, text = replay, replay["content"]
                while current["next_offset"] is not None:
                    current = self.read(section=section, max_chars=1024,
                        offset=current["next_offset"], expected_revision=complete["revision"])
                    self.assertNotIn("already_read", current)
                    text += current["content"]
                self.assertEqual(text, complete["content"])
                self.assertTrue(self.read(section=section, max_chars=10000)["already_read"])

    def test_small_budget_over_limit_does_not_reuse_but_can_read(self):
        self.add_files(WORK_READ_FILE_CHECK_LIMIT)
        for section in ("summary", "evidence"):
            with self.subTest(section=section):
                self.read(section=section, max_chars=10000)
                page = self.read(section=section, max_chars=1024)
                self.assertIn("content", page)
                self.assertTrue(page["content"])
                self.assertFalse(page["file_check"]["complete"])
                self.assertNotIn("already_read", page)
                self.assertLessEqual(len(encode(page)), 1024)
                self.assertEqual(page["files_omitted"], page["files_count"] - len(page["files"]))

    def test_small_budget_keeps_reference_risk_and_stable_full_source_version(self):
        self.add_files(1)
        risk = {"blocked": True, "reason": "unresolved " + "source details " * 300}
        with patch("agent_learning.candidate_references.lineage_risk", return_value=risk):
            for section in ("summary", "evidence"):
                with self.subTest(section=section):
                    complete = self.read(section=section, max_chars=50000)
                    receipt = self.read(section=section, max_chars=1024)
                    self.assertTrue(receipt["already_read"])
                    self.assertFalse(receipt["automatic_eligible"])
                    self.assertTrue(receipt["candidate_reference_risk"][0]["blocked"])
                    self.assertLessEqual(len(encode(receipt)), 1024)
                    replay = self.read(section=section, max_chars=1024, reread=True)
                    self.assertTrue(replay["content"])
                    self.assertFalse(replay["automatic_eligible"])
                    self.assertTrue(replay["candidate_reference_risk"][0]["details_omitted"])
                    self.assertEqual(replay["source_version"], complete["source_version"])
                    self.assertLessEqual(len(encode(replay)), 1024)

    def test_unreadable_file_does_not_reuse_or_keep_old_complete_receipt(self):
        self.read()
        with patch("agent_learning.work_memory._read_file_version", return_value={"status": "unavailable"}):
            changed = self.read()
            self.assertIn("content", changed)
            self.assertFalse(changed["file_check"]["complete"])
            self.assertIn("content", self.read())
            self.assertFalse(self.server.work_read_delivery.receipts)
        self.assertIn("content", self.read())
        self.assertTrue(self.read()["already_read"])

    def test_known_missing_file_invalidates_then_can_be_reused(self):
        self.read()
        self.file.unlink()
        changed = self.read()
        self.assertIn("content", changed)
        self.assertTrue(changed["file_check"]["complete"])
        self.assertTrue(self.read()["already_read"])
        self.file.write_text("restored file", encoding="utf-8")
        self.assertIn("content", self.read())

    def test_other_checkout_or_missing_workspace_returns_body_without_receipt(self):
        self.read()
        with self.r.transaction():
            self.r.db.execute("UPDATE work_file_links SET checkout_key='another-checkout' WHERE project=?",
                              (self.p,))
        self.assertEqual(self.read()["file_check"]["reason"], "checkout_unavailable")
        self.assertIn("content", self.read())
        no_workspace = self.r.work_read(self.p, self.memory_id)
        self.assertEqual(no_workspace["file_check"]["reason"], "workspace_unavailable")

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
                "section": "summary", "section_length": len(content), "offset": offset, "content": content,
                "file_check": {"complete": True, "version": "verified-no-files"}}

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

    def test_missing_file_check_never_creates_receipt(self):
        receipts = WorkReadDelivery()
        page = self.page()
        page.pop("file_check")
        self.assertIn("content", receipts.project(page, "scope"))
        self.assertIn("content", receipts.project(page, "scope"))
        self.assertFalse(receipts.receipts)

    def test_replay_from_middle_does_not_inherit_old_complete_ranges(self):
        receipts = WorkReadDelivery()
        full = self.page(content="abcdefghijklmnopqrst")
        receipts.project(full, "scope")
        middle = dict(full, offset=10, content="klmnopqrst")
        receipts.project(middle, "scope", reread=True)
        self.assertIn("content", receipts.project(middle, "scope"))
        receipts.project(dict(full, content="abcdefghij"), "scope")
        self.assertTrue(receipts.project(full, "scope")["already_read"])

    def test_replay_restart_at_zero_discards_previous_partial_round(self):
        receipts = WorkReadDelivery()
        full = self.page(content="abcdefghijklmnopqrst")
        receipts.project(full, "scope")
        receipts.project(dict(full, offset=10, content="klmnopqrst"), "scope", reread=True)
        first = dict(full, content="abcdefghij")
        receipts.project(first, "scope", reread=True)
        self.assertIn("content", receipts.project(first, "scope"))
        receipts.project(dict(full, offset=10, content="klmnopqrst"), "scope")
        self.assertTrue(receipts.project(full, "scope")["already_read"])


if __name__ == "__main__":
    unittest.main()
