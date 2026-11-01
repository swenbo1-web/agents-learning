"""Actual assembled cards, bounded metadata, and optional diagnostic failures."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agent_learning.context_observations import describe
from agent_learning.context_dedup import ContextDedup
from agent_learning.runtime import Runtime, encode, digest
from tests import test_context_dedup as fixtures


class ContextObservationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p

    def rows(self):
        return list(self.r.db.execute("SELECT * FROM context_output_observations "
            "WHERE project=? ORDER BY rowid", (self.p,)))

    def owner(self, *, runtime=None, conversation="reader", agent=None, epoch=1, delivery="persistent"):
        r = runtime or self.r
        return ContextDedup(r, self.p, conversation, agent, self.f.workspace,
                            r.task(self.p, self.f.conversation), epoch, delivery, {})

    def test_unknown_work_output_is_observed_without_becoming_dedup_coverage(self):
        source = self.f.source(file=True, observed=True)
        self.r.db.execute("UPDATE events SET outcome='unknown' WHERE id=?", (source["event"]["id"],))
        text = self.f.context(tool_only=True, raw={"tool_name": "read", "tool_input": {"path": "ledger.py"}})
        self.assertTrue(self.f.work_cards(text))
        row = self.rows()[-1]
        cards = json.loads(row["cards"])
        self.assertEqual(len(cards), len(self.f.work_cards(text)))
        self.assertEqual(row["output_chars"], len(text))
        self.assertEqual(row["output_digest"], digest(text))
        self.assertEqual(cards[0]["state"], "unknown")
        self.assertEqual(cards[0]["validation_level"], "tool_observed")

    def test_formal_candidate_and_work_are_separate_complete_card_kinds(self):
        lines = [encode({"knowledge_id": "formal", "revision": 1, "content_summary": "private-content"}),
                 '[待定参考 / unpublished; 非验证结论] ' + encode({"candidate_id": "candidate", "reference_version": "v1"}),
                 '[工作历史] ' + encode({"memory_id": "work", "revision": 2, "state": "unknown",
                     "reference_role": "source_update", "file_state_changed": True,
                     "related_memories": [{"memory_id": "other", "revision": 1}]})]
        output = "\n".join(lines)
        data = describe(output)
        self.assertEqual([card["kind"] for card in data["cards"]], ["formal", "candidate", "file_update"])
        self.assertEqual(data["cards"][-1]["member_count"], 2)
        self.assertEqual([card["chars"] for card in data["cards"]], list(map(len, lines)))
        self.assertNotIn("private-content", encode(data))
        self.assertNotIn("content_summary", encode(data))

    def test_source_risk_does_not_disappear_from_observations(self):
        for update in ({"source_changed": True}, {"published_knowledge_risk": ["revoked"]}):
            text = '[工作历史] ' + encode(dict(memory_id="risk", reference_role="source_update", **update))
            self.assertEqual(describe(text)["cards"][0]["kind"], "source_update")

    def test_repeated_outputs_are_distinct_observations_not_a_latest_state(self):
        owner = self.owner()
        text = '[工作历史] ' + encode({"memory_id": "one", "revision": 1, "read_required": True})
        owner.flush(output=text)
        owner.flush(output=text)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(rows[0]["output_digest"], rows[1]["output_digest"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)

    def test_empty_output_is_not_an_injection_and_metadata_only_is_explicit(self):
        owner = self.owner()
        owner.flush(output="")
        self.assertEqual(self.rows(), [])
        owner.flush(output="[Agent Learning] task=synthetic revision=1")
        self.assertEqual(json.loads(self.rows()[0]["cards"]), [])

    def test_optional_work_rollback_leaves_no_phantom_card(self):
        self.f.source(file=True)
        with patch.object(self.r, "work_record_exposure", side_effect=ValueError("synthetic failure")):
            text = self.f.context()
        self.assertEqual(self.f.work_cards(text), [])
        self.assertEqual(json.loads(self.rows()[-1]["cards"]), [])

    def test_tiny_budget_records_only_what_fits(self):
        self.f.source(file=True)
        self.r.config["injection_max_chars"] = 128
        text = self.f.context()
        row = self.rows()[-1]
        self.assertLessEqual(row["output_chars"], 128)
        self.assertEqual(row["output_chars"], len(text))
        self.assertEqual(json.loads(row["cards"]), [])

    def test_missing_observation_table_does_not_rollback_cards_or_cache(self):
        formal, _ = self.f.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.r.db.execute("DROP TABLE context_output_observations")
        self.assertIn(formal["knowledge_id"], self.f.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_delivery_cache").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 1)

    def test_missing_cache_table_still_allows_observation(self):
        formal, _ = self.f.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.r.db.execute("DROP TABLE context_delivery_cache")
        text = self.f.context()
        self.assertIn(formal["knowledge_id"], text)
        self.assertEqual(json.loads(self.rows()[-1]["cards"])[0]["item_id"], formal["knowledge_id"])

    def test_observation_transaction_rollback_cannot_undo_authoritative_delivery(self):
        formal, _ = self.f.published()
        self.r.db.execute("CREATE TRIGGER synthetic_observation_failure BEFORE INSERT ON context_output_observations "
                          "BEGIN SELECT RAISE(ROLLBACK,'synthetic'); END")
        self.assertIn(formal["knowledge_id"], self.f.context())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 1)

    def test_observation_abort_preserves_optional_cache_and_timeout(self):
        formal, _ = self.f.published()
        self.r.config["work_memory"]["automatic_context"] = False
        timeout = self.r.db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.r.db.execute("CREATE TRIGGER synthetic_observation_abort BEFORE INSERT ON context_output_observations "
                          "BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        self.assertIn(formal["knowledge_id"], self.f.context())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_delivery_cache").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("PRAGMA busy_timeout").fetchone()[0], timeout)

    def test_parser_failure_does_not_block_context_or_cache(self):
        formal, _ = self.f.published()
        with patch("agent_learning.context_observations.describe", side_effect=ValueError("synthetic")):
            self.assertIn(formal["knowledge_id"], self.f.context())
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.r.db.execute("SELECT COUNT(*) FROM context_delivery_cache").fetchone()[0])

    def test_bounded_per_owner_without_deleting_other_agent(self):
        other = self.owner(agent="child")
        other.flush(output="child-metadata")
        owner = self.owner()
        for i in range(260):
            owner.flush(output="metadata-%d" % i)
        rows = self.rows()
        self.assertEqual(sum(row["agent"] == "" for row in rows), 256)
        self.assertEqual(sum(row["agent"] == "child" for row in rows), 1)

    def test_host_task_epoch_and_ephemeral_boundaries_are_visible(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r = Runtime(self.f.root / "home", host=host, config=self.r.config)
                try:
                    owner = self.owner(runtime=r, conversation=r.host_conversation("metadata"), epoch=8,
                                       delivery="ephemeral")
                    owner.flush(output="metadata")
                    row = self.rows()[-1]
                    self.assertEqual(row["host_scope"], r.host_scope)
                    self.assertEqual(row["context_epoch"], 8)
                    self.assertEqual(row["delivery"], "ephemeral")
                    self.assertEqual(row["task_id"], self.r.task(self.p, self.f.conversation)["id"])
                finally:
                    r.close()

    def test_malformed_lines_are_not_complete_cards(self):
        text = '[工作历史] {"memory_id": "truncated"\nplain text\n{"unrelated":1}'
        self.assertEqual(describe(text)["cards"], [])


if __name__ == "__main__":
    unittest.main()
