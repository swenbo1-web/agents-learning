"""File facts may share delivery coverage; source records never share an ack."""
import json
import os
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import encode
from agent_learning.work_files import normalize_file_target
from agent_learning.work_notice_delivery import FileNoticeGate
from tests import test_context_dedup as fixtures


class FileNoticeVersionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p
        self.path = self.f.workspace / "ledger.py"
        self.path.write_text("ledger = 1\n", encoding="utf-8")
        self.original_stat = self.path.stat()
        self.sequence = 0
        self.now = time.time() + 1
        self.r.config["work_memory"]["max_items"] = 1

    def source(self, *, truncated=False, outcome="unknown", meaningful=False):
        self.sequence += 1
        text = "Success. Updated the following files:\nM ledger.py\n"
        if meaningful:
            text += "The ordering invariant was restored and its regression passed.\n"
        payload = {"tool_name": "apply_patch", "tool_input": {
            "patch": "*** Begin Patch\n*** Update File: ledger.py\n@@\n-ledger = 0\n+ledger = 1\n*** End Patch"},
            "tool_response": text}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "writer-%d" % self.sequence, "PostToolUse", payload,
                                    outcome=outcome)
            target = normalize_file_target(str(self.path), self.f.workspace)
            self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
                self.p, event["id"], target["checkout_key"], target["relative_path"],
                target["path_key"], "write_target", encode(target["version"])))
            captured = self.r._work_capture_event(self.p, event["id"], self.f.workspace, raw=payload)
            memory = captured["memory_id"]
            if truncated:
                body = json.loads(self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (memory,)).fetchone()[0])
                body["coverage"].update(truncated=True, full_original_available=False)
                self.r.db.execute("UPDATE work_memories SET body=? WHERE id=?", (encode(body), memory))
        row = self.r.db.execute("SELECT * FROM work_memories WHERE id=?", (memory,)).fetchone()
        item = self.r._work_item(self.p, row, workspace=self.f.workspace)
        self.r.work_record_exposure(self.p, self.f.conversation, [item], method="mcp")
        return memory, event["id"]

    def context(self, seconds=0, **options):
        with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now + seconds):
            return self.f.context(tool_only=True, **options)

    def test_old_ids_sharing_a_notified_file_version_do_not_drain_as_new_warnings(self):
        records = [self.source(truncated=True)[0] for _ in range(6)]
        before = {row["memory_id"]: dict(row) for row in self.r.db.execute("SELECT * FROM work_exposures")}
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        first = self.f.work_cards(self.context())
        self.assertEqual(len(first), 1)
        shown = first[0]["memory_id"]
        self.assertIn("ledger.py", encode(first[0].get("files")))
        for second in (1, 31, 300):
            self.assertEqual(self.f.work_cards(self.context(second)), [])
        # Coverage of the file fact cannot pretend that the other source IDs
        # or their historical contents were returned to the model.
        for identity in set(records) - {shown}:
            after = dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone())
            self.assertEqual(after, before[identity])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 6)

    def test_complete_receipt_warning_does_not_request_a_useless_source_read(self):
        identity, _ = self.source()
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        card = self.f.work_cards(self.context())[0]
        self.assertEqual(card["memory_id"], identity)
        self.assertFalse(card["read_required"])
        self.assertEqual(card["state"], "unknown")
        self.assertEqual(card["validation_level"], "tool_observed")

    def test_truncated_or_meaningful_source_keeps_its_read_index(self):
        for options in ({"truncated": True}, {"meaningful": True}):
            with self.subTest(options=options):
                self.r.db.execute("DELETE FROM work_exposures")
                identity, _ = self.source(**options)
                self.path.write_text("ledger revision %d\n" % self.sequence, encoding="utf-8")
                card = self.f.work_cards(self.context(self.sequence * 31))[0]
                self.assertEqual(card["memory_id"], identity)
                self.assertTrue(card["read_required"])

    def test_legacy_receipt_without_original_completeness_keeps_read_index(self):
        identity, _ = self.source()
        row = self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (identity,)).fetchone()
        body = json.loads(row[0])
        body["coverage"].pop("full_original_available")
        self.r.db.execute("UPDATE work_memories SET body=? WHERE id=?", (encode(body), identity))
        # Preserve the observed source version without inventing any new
        # original-coverage claim; this reproduces a legacy exposure.
        stored = self.r.db.execute("SELECT * FROM work_memories WHERE id=?", (identity,)).fetchone()
        item = self.r._work_item(self.p, stored, workspace=self.f.workspace)
        self.r.work_record_exposure(self.p, self.f.conversation, [item], method="mcp")
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.assertTrue(self.f.work_cards(self.context())[0]["read_required"])

    def test_mixed_receipt_and_substantive_group_keeps_the_source_read_hint(self):
        self.r.config["work_memory"]["max_items"] = 3
        self.r.config["work_memory"]["max_context_chars"] = 1800
        substantive, _ = self.source(meaningful=True)
        receipt, _ = self.source()
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        card = self.f.work_cards(self.context())[0]
        self.assertEqual(card["memory_id"], receipt)
        self.assertIn(substantive, [m["memory_id"] for m in card["related_memories"]])
        self.assertTrue(card["read_required"])

    def test_new_version_and_restoration_stay_visible(self):
        identity, _ = self.source()
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.assertIn(identity, self.context())
        self.path.write_text("ledger = 30000\n", encoding="utf-8")
        self.assertNotIn(identity, self.context(1))  # Existing bounded cooldown.
        self.assertIn(identity, self.context(31))
        self.path.write_text("ledger = 1\n", encoding="utf-8")
        os.utime(self.path, ns=(self.original_stat.st_atime_ns, self.original_stat.st_mtime_ns))
        self.assertIn(identity, self.context(32))
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.assertIn(identity, self.context(63))

    def test_coalesced_default_budget_prefers_a_file_path_over_unshown_member_ids(self):
        self.r.config["work_memory"]["max_items"] = 4
        records = [self.source(truncated=True)[0] for _ in range(5)]
        before = {row["memory_id"]: dict(row) for row in self.r.db.execute("SELECT * FROM work_exposures")}
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        card = self.f.work_cards(self.context())[0]
        self.assertEqual(card["files"][0]["path"], "ledger.py")
        visible = {card["memory_id"], *[m["memory_id"] for m in card.get("related_memories", [])]}
        self.assertLess(len(visible), 4)
        for identity in set(records) - visible:
            self.assertEqual(dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone()), before[identity])
        self.assertEqual(self.f.work_cards(self.context(31)), [])

    def test_unrendered_associated_file_does_not_borrow_visible_path_coverage(self):
        gate = self.gate()
        item = self.notice()
        item["file_state"]["hidden.py"] = "new"
        item["_changed_file_paths"].append("hidden.py")
        item["files"].append({"relative_path": "hidden.py", "version_changed": True,
                              "current_version": {"status": "stat_observed"}})
        gate.record(item, {"files": [{"path": "ledger.py"}]})
        with patch("agent_learning.work_notice_delivery.time.time", return_value=time.time() + 31):
            self.assertTrue(gate.deferred(self.notice()))
            self.assertFalse(gate.deferred(item))

    def test_visible_risk_or_restoration_invalidates_earlier_version_coverage(self):
        for updates in ({"source_changed": True}, {"state": "missing"},
                        {"files": [{"relative_path": "ledger.py", "version_changed": False}]}):
            with self.subTest(updates=updates):
                gate = self.gate()
                card = {"files": [{"path": "ledger.py"}]}
                gate.record(self.notice("first"), card)
                gate.record(self.notice("restored", **updates), card)
                with patch("agent_learning.work_notice_delivery.time.time", return_value=time.time() + 31):
                    self.assertFalse(gate.deferred(self.notice("first")))

    def test_source_change_bypasses_coverage_for_the_same_current_file_version(self):
        records = [self.source(truncated=True) for _ in range(3)]
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        shown = self.f.work_cards(self.context())[0]["memory_id"]
        identity, event = next(record for record in records if record[0] != shown)
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", ('{"tool_response":"corrected source"}', event))
        card = self.f.work_cards(self.context(31))[0]
        self.assertEqual(card["memory_id"], identity)
        self.assertTrue(card["source_changed"])
        self.assertTrue(card["read_required"])

    def test_tiny_budget_does_not_cover_an_unseen_file_or_source(self):
        identity, _ = self.source()
        before = dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone())
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.r.config["injection_max_chars"] = 128
        self.assertNotIn(identity, self.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery").fetchone()[0], 0)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone()), before)
        self.r.config["injection_max_chars"] = 2400
        self.assertIn(identity, self.context(31))

    @staticmethod
    def notice(version="new", **updates):
        return dict({"memory_id": "one", "revision": 1, "source_version": "source", "state": "unknown",
            "validation_level": "tool_observed", "file_state_changed": True,
            "file_state": {"ledger.py": version}, "_changed_file_paths": ["ledger.py"],
            "files": [{"relative_path": "ledger.py", "version_changed": True,
                       "current_version": {"status": "stat_observed"}}]}, **updates)

    def gate(self, scope="one", delivery="persistent"):
        return FileNoticeGate(self.r, self.p, self.f.conversation, None, scope, delivery)

    def test_file_version_coverage_is_current_not_a_set_of_all_past_versions(self):
        gate = self.gate()
        card = {"files": [{"path": "ledger.py"}]}
        for version in ("first", "second", "first"):
            with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now):
                gate.record(self.notice(version), card)
            with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now + 31):
                self.assertTrue(gate.deferred(self.notice(version)))
                self.assertFalse(gate.deferred(self.notice("other")))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery WHERE path_key LIKE 'version:%'").fetchone()[0], 1)

    def test_unrendered_or_clipped_path_does_not_gain_version_coverage(self):
        for card in ({}, {"files": []}, {"files": [{"path": "ledger..."}]}):
            with self.subTest(card=card):
                self.r.db.execute("DELETE FROM work_notice_delivery")
                self.gate().record(self.notice(), card)
                with patch("agent_learning.work_notice_delivery.time.time", return_value=time.time() + 31):
                    self.assertFalse(self.gate().deferred(self.notice()))

    def test_risk_recovery_ephemeral_and_scope_boundaries_do_not_borrow_coverage(self):
        gate = self.gate()
        gate.record(self.notice(), {"files": [{"path": "ledger.py"}]})
        with patch("agent_learning.work_notice_delivery.time.time", return_value=time.time() + 31):
            self.assertTrue(gate.deferred(self.notice()))
            for updates in ({"source_changed": True}, {"state": "missing"}, {"state": "denied"},
                            {"state": "conflicted"}, {"candidate_reference_risk": ["risk"]},
                            {"published_knowledge_risk": ["risk"]}, {"pending_counterexample_count": 1},
                            {"completion": "partial"}, {"_unseen_file_paths": ["new.py"]},
                            {"files": [{"relative_path": "ledger.py", "version_changed": False}]},
                            {"file_projection_incomplete": True}):
                with self.subTest(updates=updates):
                    self.assertFalse(gate.deferred(self.notice(**updates)))
            self.assertFalse(self.gate(scope="different-task-or-context").deferred(self.notice()))
            self.assertFalse(self.gate(delivery="ephemeral").deferred(self.notice()))

    def test_optional_coverage_cache_failure_preserves_notices(self):
        gate = self.gate()
        gate.record(self.notice(), {"files": [{"path": "ledger.py"}]})
        self.r.db.execute("DROP TABLE work_notice_delivery")
        self.assertFalse(gate.deferred(self.notice()))


if __name__ == "__main__":
    unittest.main()
