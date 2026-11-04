"""Complete public segments can aid comparison without becoming verified facts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint
from agent_learning.phase_local_triage import comparison_queries
from agent_learning.runtime import Runtime, digest


class PublicSegmentComparisonTests(unittest.TestCase):
    def setUp(self):
        self.problem = {"summary": "阶段公开记录：继续 [" + "a" * 32 + "] part 0",
                        "project": "project", "conversation": "chat", "task_id": "task", "task_revision": 2}
        self.task = {"goal": "继续"}
        self.report = "Conclusion: preserve SQLite v2 in D:/project/store.db; do not activate v19."

    def segments(self, text=None, *, parent="b" * 64, created=1):
        text = text if text is not None else self.report
        cut = max(1, len(text) // 2)
        chunks = (text[:cut], text[cut:])
        return [{"id": digest(["project", ["public-message", parent, digest(text), index]]),
                 "type": "PublicMessageSegment", "created": created,
                 "outcome": "reported", "health": "valid", "project": "project", "conversation": "chat",
                 "task_id": "task", "task_revision": 2, "task_write_version": 7,
                 "payload": {"text": chunk, "memory_type": "public_summary", "public_message_id": parent,
                             "segment": index, "segments": 2, "message_chars": len(text),
                             "completion": "complete", "capture_coverage": {}, "message_truncated": False,
                             "execution_authority": False}}
                for index, chunk in enumerate(chunks)]

    def query(self, events, **kwargs):
        return comparison_queries(self.problem, self.task, events, **kwargs)

    def test_complete_out_of_order_segments_recover_exact_public_text(self):
        events = self.segments()[::-1]
        original = deepcopy(events)
        result = self.query(events)
        self.assertEqual(result["fallback"], self.report)
        self.assertEqual(result["fallback_reason"], "complete_public_segments")
        self.assertEqual(events, original, "recall must not rewrite source events or manufacture a Stop")

    def test_original_goal_negation_paths_and_versions_are_preserved(self):
        self.task["goal"] = "Do not change v19; preserve D:/project/store.db for SQLite v2"
        result = self.query(self.segments())
        self.assertEqual(result["primary"], self.task["goal"])
        self.assertEqual(result["fallback"], self.task["goal"] + "\n" + self.report)

    def test_exact_parent_or_segment_identity_selects_the_same_complete_message(self):
        events = self.segments()
        for identity in ("b" * 64, events[0]["id"], events[1]["id"]):
            with self.subTest(identity=identity):
                self.assertEqual(self.query(events, latest_delivery_id=identity)["fallback"], self.report)
        self.assertEqual(self.query(events, latest_delivery_id="omitted")["fallback_reason"], "delivery_omitted")

    def test_missing_or_duplicated_segment_is_not_complete(self):
        events = self.segments()
        for value in (events[:1], events + [deepcopy(events[0])], [events[0], deepcopy(events[0])]):
            with self.subTest(count=len(value)):
                self.assertIsNone(self.query(value)["fallback"])

    def test_complete_groups_do_not_mix_public_message_identities(self):
        events = self.segments()
        events[1]["payload"]["public_message_id"] = "c" * 64
        self.assertIsNone(self.query(events)["fallback"])

    def test_equal_length_versions_cannot_mix_under_the_same_parent(self):
        earlier = self.segments()
        later = self.segments(self.report.replace("v2", "v3").replace("v19", "v20"))
        self.assertEqual(len(self.report), later[0]["payload"]["message_chars"])
        self.assertIsNone(self.query([earlier[0], later[1]])["fallback"])

    def test_visible_text_cannot_be_rewritten_under_unchanged_segment_ids(self):
        events = self.segments()
        events[0]["payload"]["text"] = events[0]["payload"]["text"].replace("v2", "v3")
        self.assertIsNone(self.query(events)["fallback"])

    def test_unknown_legacy_segment_id_format_is_left_for_normal_review(self):
        events = self.segments()
        events[0]["id"] = "unknown-legacy-id"
        self.assertIsNone(self.query(events)["fallback"])

    def test_malformed_or_ambiguous_metadata_is_not_complete(self):
        for field, value in (("segment", True), ("segment", -1), ("segment", 2), ("segments", True),
                             ("segments", 3), ("message_chars", True), ("message_chars", len(self.report) + 1),
                             ("completion", "partial"), ("message_truncated", True),
                             ("execution_authority", True), ("public_message_id", "not-a-source-identity"),
                             ("memory_type", "user_request"), ("text", ""),
                             ("capture_coverage", {"trajectory_identity": "unknown"})):
            with self.subTest(field=field, value=value):
                events = self.segments()
                events[1]["payload"][field] = value
                self.assertIsNone(self.query(events)["fallback"])

    def test_source_health_actor_assignment_and_task_boundaries_are_retained(self):
        for field, value in (("health", "conflicted"), ("outcome", "failed"), ("agent", "child"),
                             ("assignment_id", "assignment"), ("project", "other"),
                             ("conversation", "other"), ("task_id", "other"), ("task_revision", 3),
                             ("task_write_version", 8), ("execution_epoch", 9)):
            with self.subTest(field=field):
                events = self.segments()
                events[1][field] = value
                self.assertIsNone(self.query(events)["fallback"])

    def test_legacy_unknown_outcome_still_means_public_report_not_tool_success(self):
        events = self.segments()
        for event in events:
            event["outcome"] = "unknown"
        self.assertEqual(self.query(events)["fallback"], self.report)
        self.assertTrue(all(event["outcome"] == "unknown" for event in events))

    def test_long_latest_message_does_not_fall_back_to_older_complete_message(self):
        older = self.segments(parent="c" * 64)
        latest = self.segments("x" * 2049, created=2)
        result = self.query(older + latest)
        self.assertIsNone(result["fallback"])
        self.assertEqual(result["fallback_reason"], "delivery_too_long")

    def test_partial_latest_message_does_not_fall_back_to_older_stop(self):
        stop = {"id": "older", "type": "Stop", "created": 0, "outcome": "reported", "health": "valid",
                "payload": {"last_assistant_message": "An old but easily matched conclusion"}}
        self.assertIsNone(self.query([stop, self.segments()[0]])["fallback"])

    def test_omitted_latest_stop_cannot_use_an_older_segment_group(self):
        result = self.query(self.segments(), latest_delivery_id="c" * 64)
        self.assertEqual(result["fallback_reason"], "delivery_omitted")
        self.assertIsNone(result["fallback"])

    def test_latest_stop_blocks_older_complete_segment_group(self):
        latest = {"id": "c" * 64, "type": "Stop", "created": 2, "outcome": "reported", "health": "valid",
                  "payload": {"last_assistant_message": "New source omitted in part", "truncated": True}}
        self.assertIsNone(self.query(self.segments() + [latest])["fallback"])

    def test_complete_segments_can_replace_only_their_truncated_parent_preview(self):
        events = self.segments()
        parent = {"id": "b" * 64, "type": "Stop", "created": 1, "outcome": "reported", "health": "valid",
                  "payload": {"last_assistant_message": self.report[:20], "truncated": True}}
        self.assertEqual(self.query([parent] + events, latest_delivery_id=parent["id"])["fallback"], self.report)
        parent["health"] = "conflicted"
        self.assertIsNone(self.query([parent] + events, latest_delivery_id=parent["id"])["fallback"])

    def test_parent_and_complete_segments_cannot_disagree(self):
        parent = {"id": "b" * 64, "type": "Stop", "created": 1, "outcome": "reported", "health": "valid",
                  "payload": {"last_assistant_message": "Conflicting complete public statement"}}
        self.assertIsNone(self.query([parent] + self.segments())["fallback"])

    def test_truncated_parent_cannot_hide_scope_or_completion_conflict(self):
        for field, value in (("task_revision", 3), ("task_write_version", 8), ("conversation", "other"),
                             ("agent", "child"), ("completion", "partial"),
                             ("capture_coverage", {"trajectory_identity": "unknown"})):
            with self.subTest(field=field):
                parent = {"id": "b" * 64, "type": "Stop", "created": 1,
                          "outcome": "reported", "health": "valid",
                          "payload": {"last_assistant_message": self.report[:20], "truncated": True}}
                target = parent["payload"] if field in {"completion", "capture_coverage"} else parent
                target[field] = value
                self.assertIsNone(self.query([parent] + self.segments())["fallback"])

    def test_segmented_history_is_not_current_delivery(self):
        events = self.segments()
        for event in events:
            event["role"] = "history_related"
        self.assertEqual(self.query(events)["fallback_reason"], "no_delivery")

    def test_budget_and_identical_material_gates_remain(self):
        self.assertEqual(self.query(self.segments("x" * 2048))["fallback"], "x" * 2048)
        self.task["goal"] = self.report
        self.assertEqual(self.query(self.segments())["fallback_reason"], "same_material")
        self.task["goal"] = "x" * 1025
        self.assertEqual(self.query(self.segments())["fallback_reason"], "goal_too_long")


class SegmentComparisonPacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="segment-comparison-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()

    def test_real_capture_segments_reach_bounded_recall_on_all_hosts_without_writes(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                r = Runtime(self.root / (host + "-home"), host=host,
                            config={"admin": {"enabled": False}, "scheduler": {"enabled": False},
                                    "hindsight": {"enabled": False}})
                self.addCleanup(r.close)
                p = r.project(self.workspace)
                conversation = r.host_conversation("chat")
                task = r.task_update(p, conversation, "继续", new_task=True)
                report = "Checkpoint recovery requires bounded replay. " + "Preserve scoped facts. " * 65
                event, _ = r._event(p, conversation, "Stop", {"last_assistant_message": report},
                                    outcome="reported", task_id=task["id"], task_revision=task["revision"])
                r._work_capture_event(p, event["id"], self.workspace)
                rows = r.db.execute("SELECT id FROM events WHERE project=? AND type='PublicMessageSegment'",
                                    (p,)).fetchall()
                self.assertEqual(len(rows), 2)
                problem = r._problem(p, conversation, "阶段公开记录：继续 [" + "a" * 32 + "] part 0", "mixed",
                                     [row["id"] for row in rows], task_binding=task)
                changes = r.db.total_changes
                with patch.object(r, "_review_related", return_value=([], [])) as recall:
                    packet = r.review_packet(p, problem["id"], max_chars=12000)
                self.assertEqual(packet["comparison_retrieval"]["fallback"], "complete_public_segments")
                self.assertEqual(recall.call_count, 2)
                self.assertEqual(recall.call_args.args[-1], report)
                self.assertEqual(r.db.total_changes, changes)
                self.assertTrue(all(e["type"] == "PublicMessageSegment" for e in packet["events"]))
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)
                stripped = deepcopy(packet)
                stripped.pop("comparison_retrieval")
                self.assertEqual(_fingerprint(packet), _fingerprint(stripped))
                self.assertEqual(json.loads(r.db.execute("SELECT payload FROM events WHERE id=?", (event["id"],)).fetchone()[0])
                                 ["last_assistant_message"], report)

    def test_omitted_latest_segment_does_not_revive_earlier_visible_stop(self):
        r = Runtime(self.root / "omitted-home", config={"admin": {"enabled": False}})
        self.addCleanup(r.close)
        p = r.project(self.workspace)
        task = r.task_update(p, "chat", "continue", new_task=True)
        old, _ = r._event(p, "chat", "Stop", {"last_assistant_message": "Old checkpoint recovery conclusion"},
                          outcome="reported", task_id=task["id"], task_revision=task["revision"])
        body = {"text": "Changed scope " + "x" * 8000, "memory_type": "public_summary",
                "public_message_id": "c" * 64, "segment": 0, "segments": 2, "message_chars": 9000,
                "completion": "complete", "execution_authority": False}
        latest, _ = r._event(p, "chat", "PublicMessageSegment", body,
            outcome="reported", task_id=task["id"], task_revision=task["revision"])
        r.db.execute("UPDATE events SET created=?,payload=? WHERE id=?",
                     (old["created"] + 1, json.dumps(body), latest["id"]))
        problem = r._problem(p, "chat", "阶段公开记录：continue [" + "a" * 32 + "] part 0", "mixed",
                             [old["id"], latest["id"]], task_binding=task)
        with patch.object(r, "_review_related", return_value=([], [])) as recall:
            packet = r.review_packet(p, problem["id"], max_chars=5000)
        self.assertIn(old["id"], [event["id"] for event in packet["events"]])
        self.assertNotIn(latest["id"], [event["id"] for event in packet["events"]])
        self.assertEqual(recall.call_count, 1)
        self.assertEqual(packet["comparison_retrieval"]["fallback"], "delivery_omitted")


if __name__ == "__main__":
    unittest.main()
