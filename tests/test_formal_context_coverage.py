"""Formal coverage and file notices remain independent, bounded and provisional."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import encode
from tests import test_context_dedup as fixtures


class FormalContextCoverageTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.r.config["work_memory"]["automatic_context"] = False

    def test_index_upgrades_to_body_then_long_body_covers_smaller_presentations(self):
        f = self.f
        formal, _ = f.published(applicability="dedupfocus scope " + "c" * 700,
            limitations="reported limits " + "l" * 700)
        f.r.config["injection_max_chars"] = 750
        first = f.formal_cards(f.context())
        self.assertTrue(first[0]["read_required"])
        self.assertEqual(f.formal_cards(f.context()), [])
        f.r.config["injection_max_chars"] = 6000
        full = f.formal_cards(f.context())
        self.assertEqual(full[0]["knowledge_id"], formal["knowledge_id"])
        self.assertEqual(full[0]["content_summary"], f.CONTENT)
        self.assertEqual(f.formal_cards(f.context()), [])
        f.r.config["injection_max_chars"] = 750
        self.assertEqual(f.formal_cards(f.context()), [])
        f.r.config["injection_max_chars"] = 6000
        self.assertEqual(f.formal_cards(f.context()), [])
        self.assertTrue(f.formal_cards(f.context(compact=True)))

    def test_formal_file_notice_survives_disabled_work_and_unrelated_query(self):
        f = self.f
        source = f.source(file=True)
        formal, _ = f.published(source)
        f.context()
        f.r.config["work_memory"]["enabled"] = False
        (f.workspace / "ledger.py").write_text("changed larger source\n", encoding="utf-8")
        text = f.context("unrelated astronomy observation")
        self.assertIn("关联文件已变化", text)
        self.assertIn(formal["knowledge_id"], text)
        self.assertEqual(f.formal_cards(text), [])
        self.assertNotIn("关联文件已变化", f.context("unrelated astronomy observation"))
        (f.workspace / "ledger.py").write_text("another source with an even larger size\n", encoding="utf-8")
        self.assertIn("关联文件已变化", f.context("unrelated astronomy observation"))
        self.assertEqual(f.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_equal_length_excerpts_from_distinct_passages_do_not_cover_each_other(self):
        f = self.f
        content = "alphalimit first constraint. " + "a" * 950 + "\nomegalimit different constraint. " + "b" * 700
        f.published(content=content)
        first = f.formal_cards(f.context("alphalimit"))
        second = f.formal_cards(f.context("omegalimit"))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertIn("omegalimit", second[0]["content_summary"])
        self.assertNotEqual(first[0]["content_summary"], second[0]["content_summary"])
        self.assertEqual(f.formal_cards(f.context("omegalimit")), [])
        self.assertEqual(f.formal_cards(f.context("alphalimit")), [])

    def test_nested_head_tail_clipping_records_only_actual_original_characters(self):
        from agent_learning.context_dedup import _formal_ranges
        from agent_learning.knowledge_output import _summary
        from agent_learning.runtime import clip
        content = "".join(chr(0x4000 + index) for index in range(2000))
        for anchor in (0, 250, 900):
            item = {"content": content, "match": {"matched_terms": [content[anchor]]}}
            rendered = _summary(item, 500)
            for limit in (500, 249, 124, 62, 31, 24):
                if len(rendered) > limit:
                    rendered = clip(rendered, max(24, len(rendered) // 2))
                ranges = _formal_ranges(item, {"content_summary": rendered})
                observed = {index for start, end in ranges for index in range(start, end)}
                actual = {index for index, character in enumerate(content) if character in rendered}
                self.assertEqual(observed, actual)

    def test_manual_exposure_receives_notice_and_tiny_budget_does_not_ack_it(self):
        f = self.f
        formal, _ = f.published(f.source(file=True))
        server = Server(f.r, f.p, context_handle=f.handle, workspace=f.workspace)
        server.call("knowledge_read", {"knowledge_id": formal["knowledge_id"]})
        before = json.loads(f.r.db.execute("SELECT state FROM context_exposures WHERE knowledge_id=?",
            (formal["knowledge_id"],)).fetchone()[0])["_files"]["version"]
        (f.workspace / "ledger.py").write_text("changed after manual read\n", encoding="utf-8")
        f.r.config["injection_max_chars"] = 128
        self.assertNotIn("关联文件已变化", f.context())
        state = json.loads(f.r.db.execute("SELECT state FROM context_exposures WHERE knowledge_id=?",
            (formal["knowledge_id"],)).fetchone()[0])
        self.assertEqual(state["_files"]["version"], before)
        note = f.context(tool_only=True)
        self.assertIn("关联文件已变化", note)
        self.assertLessEqual(len(note), 128)
        self.assertNotIn("关联文件已变化", f.context(tool_only=True))

    def test_unassociated_front_rows_do_not_starve_later_file_exposure(self):
        f = self.f
        server = Server(f.r, f.p, context_handle=f.handle, workspace=f.workspace)
        for index in range(16):
            formal, _ = f.published(title=f"dedupfocus unrelated source {index}", content=f"dedupfocus record {index}")
            server.call("knowledge_read", {"knowledge_id": formal["knowledge_id"]})
        target, _ = f.published(f.source(file=True), title="dedupfocus file target")
        server.call("knowledge_read", {"knowledge_id": target["knowledge_id"]})
        (f.workspace / "ledger.py").write_text("modified file target\n", encoding="utf-8")
        output = f.context("unrelated astronomy") + f.context("unrelated astronomy")
        self.assertIn(target["knowledge_id"], output)
        self.assertIn("关联文件已变化", output)

    def test_optional_file_storage_failure_keeps_revocation_and_formal_delivery(self):
        f = self.f
        old, _ = f.published(f.source(file=True))
        f.context()
        f.r.revoke(f.p, old["knowledge_id"], expected_revision=old["revision"])
        new, _ = f.published(title="dedupfocus new useful knowledge", content="dedupfocus next safe reference")
        original = __import__("agent_learning.context_files", fromlist=["notifications"]).notifications
        def failed(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic file-cache write fault")
        with patch("agent_learning.context_files.notifications", side_effect=failed):
            text = f.context()
        self.assertIn("已撤销", text)
        self.assertIn(new["knowledge_id"], text)
        self.assertNotIn(old["knowledge_id"], {card["knowledge_id"] for card in f.formal_cards(text)})

    def test_file_marker_is_not_misclassified_as_semantic_source_change(self):
        f = self.f
        formal, _ = f.published(f.source(file=True))
        f.context()
        with f.r.transaction():
            f.r.db.execute("UPDATE knowledge_source_versions SET version=version+1 WHERE knowledge_id=?",
                           (formal["knowledge_id"],))
        text = f.context()
        self.assertNotIn("来源已变", text)
        self.assertNotIn("关联文件已变化", text)
