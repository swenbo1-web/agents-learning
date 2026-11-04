"""Real producer/seal/packet paths keep complete small replies atomic."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint
from agent_learning.phase_review import configure, seal
from agent_learning.review_packet import review_instruction
from agent_learning.runtime import Runtime


class AtomicPublicDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atomic-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0

    def stage(self, host="codex", *, request_chars=1500, reply_chars=1800, text=None, before_seal=None):
        self.counter += 1
        root = self.root / str(self.counter)
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        r = Runtime(root / "home", host=host, config={"admin": {"enabled": False}, "scheduler": {"enabled": False}})
        self.addCleanup(r.close)
        project = r.project(workspace)
        configure(r, project, {"mode": "phase"}, 0)
        conversation = r.host_conversation("reader")
        task = r.task_update(project, conversation, "continue", new_task=True)
        r._event(project, conversation, "UserPromptSubmit", {"prompt": "Request " + "x" * request_chars},
            outcome="reported", task_id=task["id"], task_revision=task["revision"])
        r.task_update(project, conversation, "continue", status="delivered", expected_task_id=task["id"],
                      expected_write_version=task["write_version"])
        text = text if text is not None else ("Result " + "保留原限制。" * reply_chars)[:reply_chars]
        stop, _ = r._event(project, conversation, "Stop", {"last_assistant_message": text},
            outcome="reported", task_id=task["id"], task_revision=task["revision"])
        r._work_capture_event(project, stop["id"], workspace, raw={"last_assistant_message": text})
        if before_seal:
            before_seal(r, project, stop)
        with r.transaction():
            seal(r, project, conversation, stop["id"])
        return r, project, conversation, stop, text

    def packets(self, r, project, conversation, max_chars=16000):
        rows = r.db.execute("SELECT id FROM problems WHERE project=? AND conversation=? ORDER BY summary",
                            (project, conversation)).fetchall()
        before = r.db.total_changes
        values = [r.review_packet(project, row[0], max_chars=max_chars) for row in rows]
        self.assertEqual(before, r.db.total_changes, "reading grouping/coverage must not change source records")
        return values

    def assert_chunk_limits(self, r):
        for row in r.db.execute("SELECT p.problem,COUNT(*) n,SUM(length(e.payload)) chars "
                "FROM problem_events p JOIN events e ON e.id=p.event GROUP BY p.problem"):
            self.assertLessEqual(row["n"], 6)
            # Preserve legacy single oversized source behavior; never combine
            # other sources with it to evade the old per-case budget.
            if row["n"] > 1:
                self.assertLessEqual(row["chars"], 4000)

    def test_default_three_host_long_request_small_reply_matrix_is_complete(self):
        for host in ("codex", "antigravity", "dsh"):
            for chars in (1800, 2048):
                with self.subTest(host=host, chars=chars):
                    r, project, conversation, _, _ = self.stage(host, reply_chars=chars)
                    packets = self.packets(r, project, conversation)
                    deliveries = [item for item in packets if any(e["type"] == "PublicMessageSegment" for e in item["events"])]
                    self.assertEqual(len(deliveries), 1)
                    packet = deliveries[0]
                    self.assertEqual(len(packet["events"]), 2)
                    self.assertEqual(packet["comparison_retrieval"]["fallback"], "complete_public_segments")
                    self.assertTrue(packet["public_delivery_coverage"]["complete"])
                    self.assertFalse(packet["public_delivery_coverage"]["read_required"])
                    self.assert_chunk_limits(r)

    def test_negation_and_qualifier_in_second_part_survive_the_actual_query(self):
        tail = " SQLite SQLITE_BUSY: do not use this plan in production; the trial failed."
        text = "Conclusion: use this plan only for testing. " + "padding " * 190
        text = text[:1800 - len(tail)] + tail
        r, project, conversation, _, _ = self.stage(text=text)
        with patch.object(r, "_review_related", wraps=r._review_related) as calls:
            packets = self.packets(r, project, conversation)
        self.assertTrue(any(tail in args.args[2] for args in calls.call_args_list))
        selected = next(item for item in packets if item.get("public_delivery_coverage"))
        self.assertTrue(selected["public_delivery_coverage"]["complete"])
        self.assertEqual("".join(e["payload"]["text"] for e in sorted(selected["events"],
            key=lambda e: e["payload"].get("segment", -1)) if e["type"] == "PublicMessageSegment"), text)

    def test_large_message_remains_bounded_with_explicit_partial_semantics(self):
        r, project, conversation, _, _ = self.stage(reply_chars=9000)
        packets = self.packets(r, project, conversation)
        deliveries = [item for item in packets if item.get("public_delivery_coverage")]
        self.assertGreater(len(deliveries), 1)
        for item in deliveries:
            self.assertFalse(item["public_delivery_coverage"]["complete"])
            self.assertTrue(item["public_delivery_coverage"]["read_required"])
            self.assertFalse(item["incomplete"], "whole-message semantics must not deadlock otherwise visible sources")
            self.assertIn("limitations必须保留", review_instruction(item))
            self.assertLessEqual(len(review_instruction(item)), 16000)
        self.assert_chunk_limits(r)

    def test_packet_budget_cannot_remove_a_part_without_semantic_warning(self):
        r, project, conversation, _, _ = self.stage()
        identity = next(item["problem"]["id"] for item in self.packets(r, project, conversation)
                        if item.get("public_delivery_coverage"))
        for limit in (3000, 4000, 6000, 16000):
            with self.subTest(limit=limit):
                packet = r.review_packet(project, identity, max_chars=limit)
                self.assertLessEqual(len(review_instruction(packet)), limit)
                segments = [e for e in packet["events"] if e["type"] == "PublicMessageSegment"]
                if len(segments) == 1:
                    self.assertTrue(packet["public_delivery_coverage"]["read_required"])
                    self.assertFalse(packet["public_delivery_coverage"]["complete"])
                    self.assertTrue(packet["incomplete"])

    def test_parent_risk_changes_coverage_and_review_identity_without_new_sources(self):
        r, project, conversation, stop, _ = self.stage()
        packet = next(item for item in self.packets(r, project, conversation) if item.get("public_delivery_coverage"))
        refs = {e["id"] for e in packet["events"]}
        self.assertNotIn(stop["id"], refs, "validation context is not model-citable case evidence")
        r.db.execute("UPDATE events SET outcome='failed' WHERE project=? AND id=?", (project, stop["id"]))
        risky = r.review_packet(project, packet["problem"]["id"], max_chars=16000)
        self.assertFalse(risky["public_delivery_coverage"]["complete"])
        self.assertTrue(risky["public_delivery_coverage"]["read_required"])
        self.assertEqual(risky["comparison_retrieval"]["fallback"], "incomplete_delivery")
        self.assertNotEqual(_fingerprint(packet), _fingerprint(risky))
        self.assertEqual(refs, {e["id"] for e in risky["events"]})

    def test_missing_segment_never_becomes_an_atomic_or_complete_reply(self):
        def remove_part(r, project, stop):
            r.db.execute("DELETE FROM events WHERE project=? AND type='PublicMessageSegment' "
                "AND json_extract(payload,'$.public_message_id')=? AND json_extract(payload,'$.segment')=1",
                (project, stop["id"]))
        r, project, conversation, _, _ = self.stage(before_seal=remove_part)
        for packet in self.packets(r, project, conversation):
            coverage = packet.get("public_delivery_coverage")
            if coverage:
                self.assertFalse(coverage["complete"])
                self.assertTrue(coverage["read_required"])
                self.assertNotEqual(packet["comparison_retrieval"]["fallback"], "complete_public_segments")

    def test_replayed_stop_does_not_rewrite_cases_or_register_another_phase(self):
        r, project, conversation, stop, _ = self.stage()
        original = list(r.db.execute("SELECT id,revision,summary FROM problems ORDER BY id"))
        with r.transaction():
            seal(r, project, conversation, stop["id"])
        self.assertEqual(original, list(r.db.execute("SELECT id,revision,summary FROM problems ORDER BY id")))
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 1)

    def test_coverage_parent_alias_is_transport_while_semantic_risk_remains_business(self):
        r, project, conversation, _, _ = self.stage()
        packet = next(item for item in self.packets(r, project, conversation) if item.get("public_delivery_coverage"))
        aliased = deepcopy(packet)
        aliased["public_delivery_coverage"]["messages"][0]["public_message_id"] = "different-parent-alias"
        self.assertEqual(_fingerprint(packet), _fingerprint(aliased))
        partial = deepcopy(packet)
        partial["public_delivery_coverage"].update(complete=False, read_required=True)
        partial["public_delivery_coverage"]["messages"][0].update(complete=False, reason="incomplete_delivery")
        self.assertNotEqual(_fingerprint(packet), _fingerprint(partial))


if __name__ == "__main__":
    unittest.main()
