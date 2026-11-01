"""Compact visible patch receipts without claiming the source is complete."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime, encode
from tests import test_context_dedup as context_fixture
from tests import test_file_notice_versions1452 as version_fixture


class FileReceiptPreviewTests(unittest.TestCase):
    # Reuse the real source/notice builders, but keep this acceptance's data
    # under its own root rather than the older fixture's generation directory.
    source = version_fixture.FileNoticeVersionTests.source
    context = version_fixture.FileNoticeVersionTests.context

    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "accept1453" / "notice-tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "checkout"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(config),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"), "CODEX_HOME": str(self.root / "codex"),
            "PYTHONDONTWRITEBYTECODE": "1"}
        environment.update({key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.f = context_fixture.ContextDedupTests()
        self.f.r, self.f.workspace = self.r, self.root / "checkout"
        self.p = self.f.p = self.r.project(self.f.workspace)
        self.f.conversation = "reader"
        self.r.task_update(self.p, self.f.conversation, "ledger ordering preview", {"mode": "analysis"})
        self.f.handle = self.r.register_context(self.p, self.f.conversation)["context_handle"]
        self.path = self.f.workspace / "ledger.py"
        self.path.write_text("ledger = 1\n", encoding="utf-8")
        self.original_stat = self.path.stat()
        self.sequence, self.now = 0, time.time() + 1
        self.r.config["work_memory"]["max_items"] = 1

    def row(self, identity):
        return self.r.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?",
                                 (self.p, identity)).fetchone()

    def unseen_context(self, **options):
        self.r.db.execute("DELETE FROM work_exposures WHERE project=?", (self.p,))
        return self.context(raw={"tool_name": "read_file", "tool_input": {"path": str(self.path)}}, **options)

    def set_summary(self, identity, text):
        body = json.loads(self.row(identity)["body"])
        body["summary"] = json.dumps(text)
        self.r.db.execute("UPDATE work_memories SET body=? WHERE project=? AND id=?",
                          (encode(body), self.p, identity))

    def test_partial_receipt_card_is_compact_but_manual_source_is_unchanged(self):
        identity, event = self.source(truncated=True)
        before = self.row(identity)["body"]
        item = self.r._work_item(self.p, self.row(identity), workspace=self.f.workspace)
        old_summary = item["summary"][:80]
        card = self.f.work_cards(self.unseen_context())[0]
        self.assertEqual(card["title"], "文件修改索引")
        self.assertNotIn("summary", card)
        self.assertEqual(card["source_coverage"], "partial")
        self.assertTrue(card["read_required"])
        self.assertEqual(card["state"], "unknown")
        self.assertEqual(card["validation_level"], "tool_observed")
        self.assertEqual(card["files"][0]["path"], "ledger.py")
        # Even with an explicit coverage warning this is smaller than the old
        # acknowledgement preview; characters are not measured model tokens.
        old = dict(card, title=item["title"][:50], summary=old_summary)
        old.pop("source_coverage")
        self.assertLess(len(encode(card)), len(encode(old)))
        result = self.r.work_read(self.p, identity, workspace=self.f.workspace)
        self.assertEqual(result["title"], item["title"])
        self.assertIn("Success. Updated", result["content"])
        self.assertIn(event, result["source_event_refs"])
        self.assertTrue(result["coverage"]["truncated"])
        self.assertFalse(result["coverage"]["full_original_available"])
        self.assertEqual(self.row(identity)["body"], before)

    def test_visible_receipt_fragment_does_not_prove_receipt_only_original(self):
        identity, _ = self.source(truncated=True)
        item = self.r._work_item(self.p, self.row(identity), workspace=self.f.workspace)
        current = self.r.work_validate_items(self.p, [item], workspace=self.f.workspace)[0]
        self.assertTrue(current["_file_receipt_preview"])
        self.assertNotIn("_file_receipt_only", current)
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        card = self.f.work_cards(self.context())[0]
        self.assertTrue(card["read_required"])
        self.assertEqual(card["source_coverage"], "partial")
        self.assertTrue(card["file_state_changed"])

    def test_only_full_stored_visible_fragment_can_select_the_compact_preview(self):
        identity, _ = self.source(truncated=True)
        # The public index is normally shortened to 240 characters. A known
        # prefix alone must not hide a substantive tail in the stored projection.
        text = "Success. Updated the following files:\n" + "\n".join("M file_%d.py" % n for n in range(80))
        self.set_summary(identity, text)
        card = self.f.work_cards(self.unseen_context())[0]
        self.assertEqual(card["title"], "文件修改索引")
        self.assertTrue(card["read_required"])
        self.set_summary(identity, text + "\nWARNING: recovered output is still incomplete")
        card = self.f.work_cards(self.unseen_context())[0]
        self.assertNotEqual(card["title"], "文件修改索引")
        self.assertIn("summary", card)

    def test_structured_write_result_is_not_treated_as_a_patch_text_fragment(self):
        identity, _ = self.source(truncated=True)
        body = json.loads(self.row(identity)["body"])
        body["summary"] = json.dumps({"path": "ledger.py", "written": True, "chars": 10})
        self.r.db.execute("UPDATE work_memories SET body=? WHERE id=?", (encode(body), identity))
        item = self.r._work_item(self.p, self.row(identity), workspace=self.f.workspace)
        current = self.r.work_validate_items(self.p, [item], workspace=self.f.workspace)[0]
        self.assertNotIn("_file_receipt_preview", current)
        self.assertNotIn("_file_receipt_only", current)
        self.assertEqual(current["title"], item["title"])

    def test_ephemeral_display_keeps_the_same_partial_read_contract(self):
        identity, _ = self.source(truncated=True)
        first = self.f.work_cards(self.unseen_context(delivery="ephemeral"))[0]
        second = self.f.work_cards(self.context(delivery="ephemeral", raw={
            "tool_name": "read_file", "tool_input": {"path": str(self.path)}}))[0]
        for card in (first, second):
            self.assertEqual(card["memory_id"], identity)
            self.assertEqual(card["title"], "文件修改索引")
            self.assertEqual(card["source_coverage"], "partial")
            self.assertTrue(card["read_required"])

    def test_source_change_warning_is_not_replaced_by_the_compact_title(self):
        identity, event = self.source(truncated=True)
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.assertIn(identity, self.context())
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (
            '{"tool_response":"corrected source"}', event))
        card = self.f.work_cards(self.context(1))[0]
        self.assertEqual(card["memory_id"], identity)
        self.assertTrue(card["source_changed"])
        self.assertEqual(card["title"], "关联来源或文件状态变化")
        self.assertTrue(card["read_required"])
        self.assertIn("重新核对", card["recheck"])

    def test_explanation_warning_error_and_partial_line_keep_original_preview(self):
        identity, _ = self.source(truncated=True)
        receipt = "Success. Updated the following files:\nM ledger.py\n"
        for text in (receipt + "The invariant was restored.", receipt + "WARNING: still unsafe",
                     "Exit code: 1\n" + receipt, receipt + "Failure: E_BROKEN", receipt + "M ",
                     "Important explanation\n" + receipt):
            with self.subTest(text=text):
                self.set_summary(identity, text)
                card = self.f.work_cards(self.unseen_context())[0]
                self.assertNotEqual(card["title"], "文件修改索引")
                self.assertIn("summary", card)
                self.assertNotIn("source_coverage", card)
                self.assertTrue(card["read_required"])

    def test_partial_preview_is_not_an_additional_suppression_rule(self):
        identity, _ = self.source(truncated=True)
        first = self.unseen_context()
        self.assertIn(identity, first)
        before = dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone())
        self.assertNotIn(identity, self.context(raw={"tool_name": "read_file", "tool_input": {"path": str(self.path)}}))
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone()), before)
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.assertIn(identity, self.context(31))

    def test_legacy_unknown_original_coverage_retains_read_requirement(self):
        identity, _ = self.source()
        body = json.loads(self.row(identity)["body"])
        body["coverage"].pop("full_original_available")
        self.r.db.execute("UPDATE work_memories SET body=? WHERE id=?", (encode(body), identity))
        item = self.r._work_item(self.p, self.row(identity), workspace=self.f.workspace)
        self.r.work_record_exposure(self.p, self.f.conversation, [item], method="mcp")
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        card = self.f.work_cards(self.context())[0]
        self.assertTrue(card["read_required"])
        self.assertEqual(card["source_coverage"], "unknown")

    def test_coverage_label_cannot_displace_previously_fitting_path_or_risk(self):
        identity, _ = self.source(truncated=True)
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        original_validate = self.r.work_validate_items

        def old_view(*args, **kwargs):
            items = original_validate(*args, **kwargs)
            for item in items:
                item.pop("_file_receipt_preview", None)
            return items

        tables = ("work_exposures", "work_notice_delivery", "context_delivery_cache")
        saved = {name: [dict(row) for row in self.r.db.execute("SELECT * FROM " + name + " WHERE project=?",
                  (self.p,))] for name in tables}
        with patch.object(self.r, "work_validate_items", side_effect=old_view):
            old_text = self.context()
        for name, rows in saved.items():
            self.r.db.execute("DELETE FROM " + name + " WHERE project=?", (self.p,))
            for row in rows:
                self.r.db.execute("INSERT INTO " + name + "(" + ",".join(row) + ") VALUES(" +
                    ",".join("?" for _ in row) + ")", tuple(row.values()))
        old_card = self.f.work_cards(old_text)[0]
        header = "工作历史索引（非正式解法/执行授权；仅相关且索引不足时用 work_memory_read）："
        self.r.config["work_memory"]["max_context_chars"] = len(header) + 1 + len("[工作历史] " + encode(old_card)) + 1
        card = self.f.work_cards(self.context())[0]
        self.assertEqual(card["memory_id"], identity)
        self.assertEqual(card["files"], old_card["files"])
        self.assertEqual(card["recheck"], old_card["recheck"])
        self.assertTrue(card["file_state_changed"])
        self.assertTrue(card["read_required"])

    def test_tiny_budget_does_not_acknowledge_hidden_preview(self):
        identity, _ = self.source(truncated=True)
        before = dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone())
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        self.r.config["injection_max_chars"] = 128
        self.assertNotIn(identity, self.context())
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM work_exposures WHERE memory_id=?", (identity,)).fetchone()), before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
