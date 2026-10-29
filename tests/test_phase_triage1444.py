"""Bounded phase comparison recall; only synthetic SQLite, never model calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint
from agent_learning.admin_decisions import review_signature
from agent_learning.phase_local_triage import comparison_queries
from agent_learning.phase_review import configure, seal
from agent_learning.review_packet import review_instruction
from agent_learning.runtime import Runtime


class PhaseTriage1444Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-triage1444-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        with self.r.transaction():
            configure(self.r, self.p, {"mode": "phase", "every_phases": 1}, 0)
        self.serial = 0

    def published(self, title, content, *, project=None, limitations="Reported only"):
        project = project or self.p
        self.serial += 1
        conversation = "source-" + str(self.serial)
        task = self.r.task_update(project, conversation, title, new_task=True)
        with self.r.transaction():
            event, _ = self.r._event(project, conversation, "Stop", {"last_assistant_message": content},
                task_id=task["id"], task_revision=task["revision"], outcome="reported")
            problem = self.r._problem(project, conversation, title, "project", [event["id"]], task_binding=task)
        candidate = self.r.submit_candidate(project, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content, category="project", applicability="Same project and scope",
            limitations=limitations, knowledge_kind="fact", validation_level="reported", evidence_refs=[event["id"]])
        return self.r.publish(project, candidate["candidate_id"])["knowledge_id"]

    def phase(self, goal="继续推进", report="Checkpoint recovery requires bounded replay"):
        task = self.r.task_update(self.p, "phase", goal, new_task=True)
        self.r._event(self.p, "phase", "UserPromptSubmit", {"prompt": goal}, outcome="reported",
                      task_id=task["id"], task_revision=1)
        self.r.task_update(self.p, "phase", goal, status="delivered", expected_task_id=task["id"],
                           expected_write_version=task["write_version"])
        event, _ = self.r._event(self.p, "phase", "Stop", {"last_assistant_message": report},
                                 outcome="reported", task_id=task["id"], task_revision=1)
        with self.r.transaction():
            seal(self.r, self.p, "phase", event["id"])
        problem = self.r.db.execute("SELECT id FROM problems WHERE project=? AND task_id=? "
            "AND summary LIKE '阶段公开记录：%' ORDER BY created,id LIMIT 1", (self.p, task["id"])).fetchone()
        self.assertIsNotNone(problem)
        return problem["id"]

    @staticmethod
    def source(report="Conclusion: keep v2; do not activate the candidate."):
        return [{"id": "delivery", "type": "Stop", "created": 1, "outcome": "reported", "health": "valid",
                 "payload": {"last_assistant_message": report}}]

    def test_generated_template_removed_without_losing_goal_constraints(self):
        goal = "Do not activate v19; preserve D:/project/schema.json for SQLite v2"
        problem = {"summary": "阶段公开记录：" + goal + " [" + "a" * 32 + "] part 0"}
        plan = comparison_queries(problem, {"goal": goal}, self.source())
        self.assertEqual(plan["primary"], goal)
        self.assertTrue(plan["fallback"].startswith(goal + "\n"))
        self.assertIn("do not activate", plan["fallback"].casefold())
        self.assertNotIn("part 0", plan["fallback"])

    def test_plain_problem_keeps_existing_query_and_no_fallback(self):
        plan = comparison_queries({"summary": "Public project report"}, {"goal": "v2"}, self.source())
        self.assertEqual(plan["primary"], "Public project report v2")
        self.assertEqual(plan["fallback_reason"], "not_stage")
        self.assertIsNone(plan["fallback"])

    def test_incomplete_or_long_delivery_is_not_summarized_into_a_guess(self):
        problem = {"summary": "阶段公开记录：继续 [" + "a" * 32 + "] part 0"}
        for change in ("partial", "long", "conflicted", "child"):
            with self.subTest(change=change):
                events = self.source()
                if change == "partial":
                    events[0]["payload"]["truncated"] = True
                elif change == "long":
                    events[0]["payload"]["last_assistant_message"] = "x" * 2049
                elif change == "child":
                    events[0]["agent"] = "child"
                else:
                    events[0]["health"] = "conflicted"
                self.assertIsNone(comparison_queries(problem, {"goal": "继续"}, events)["fallback"])
        latest_partial = self.source() + [dict(self.source()[0], created=2,
            payload={"last_assistant_message": "A newer final statement", "truncated": True})]
        self.assertIsNone(comparison_queries(problem, {"goal": "继续"}, latest_partial)["fallback"])

    def test_complete_delivery_rescues_weak_goal_with_two_bounded_queries(self):
        kid = self.published("Checkpoint recovery", "Checkpoint recovery requires bounded replay")
        problem = self.phase()
        before = self.r.db.total_changes
        with patch.object(self.r, "_review_related", wraps=self.r._review_related) as recall:
            packet = self.r.review_packet(self.p, problem)
        self.assertEqual(recall.call_count, 2)
        self.assertEqual(self.r.db.total_changes, before, "recall diagnostics must remain read-only")
        self.assertEqual(packet["comparison_retrieval"]["primary"]["knowledge"], 0)
        self.assertEqual(packet["comparison_retrieval"]["fallback"], "complete_public_delivery")
        self.assertEqual([item["knowledge_id"] for item in packet["related_knowledge"]], [kid])
        self.assertEqual(packet["related_knowledge"][0]["validation_level"], "reported")
        self.assertEqual(packet["related_knowledge"][0]["source_health"], self.r.read(self.p, kid)["source_health"])

    def test_primary_hit_does_not_launch_fallback(self):
        self.published("Checkpoint recovery", "Checkpoint recovery requires bounded replay")
        problem = self.phase(goal="Checkpoint recovery", report="Checkpoint recovery now has a new scoped fact")
        with patch.object(self.r, "_review_related", wraps=self.r._review_related) as recall:
            packet = self.r.review_packet(self.p, problem)
        self.assertEqual(recall.call_count, 1)
        self.assertEqual(packet["comparison_retrieval"]["fallback"], "not_needed")

    def test_original_version_and_positive_identifier_gate_are_retained(self):
        self.published("SQLite v1 checkpoint recovery", "SQLite v1 checkpoint recovery uses bounded replay",
                       limitations="Not applicable to SQLite v2")
        problem = self.phase(goal="SQLite v2 checkpoint recovery", report="Checkpoint recovery uses bounded replay")
        packet = self.r.review_packet(self.p, problem)
        self.assertEqual(packet["related_knowledge"], [], "v2 only in a restriction is not a positive match")
        self.assertEqual(packet["comparison_retrieval"]["returned"]["knowledge"], 0)

    def test_other_project_delivery_does_not_enter_comparison(self):
        other = self.root / "other-project"
        other.mkdir()
        project = self.r.project(other)
        self.published("Checkpoint recovery", "Checkpoint recovery requires bounded replay", project=project)
        packet = self.r.review_packet(self.p, self.phase())
        self.assertEqual(packet["related_knowledge"], [])

    def test_no_formal_match_is_not_claimed_to_mean_no_reusable_value(self):
        report = "已完成V1核心集成；181项测试仅为报告，完整V1未完成。v19候选未激活，下一步注册接线。"
        packet = self.r.review_packet(self.p, self.phase(report=report))
        self.assertIn(report, [event["payload"].get("last_assistant_message") for event in packet["events"]])
        self.assertEqual(packet["comparison_retrieval"]["returned"], {"knowledge": 0, "candidates": 0})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)
        self.assertNotEqual(self.r.db.execute("SELECT state FROM review_phases").fetchone()[0], "routine_deferred")

    def test_omitted_latest_stop_cannot_recall_from_an_older_visible_conclusion(self):
        self.published("Checkpoint recovery", "Checkpoint recovery requires bounded replay")
        problem = self.phase()
        previous = self.r.db.execute("SELECT MAX(created) FROM events WHERE project=?", (self.p,)).fetchone()[0]
        latest, _ = self.r._event(self.p, "phase", "Stop", {"last_assistant_message": "Changed scope " + "x" * 8000},
                                  outcome="reported")
        self.r.db.execute("UPDATE events SET created=?,payload=? WHERE id=?", (previous + 1,
            json.dumps({"last_assistant_message": "Changed scope " + "x" * 8000}), latest["id"]))
        self.r.db.execute("INSERT INTO problem_events(problem,event,role) VALUES(?,?,?)",
                          (problem, latest["id"], "delivery"))
        with patch.object(self.r, "_review_related", wraps=self.r._review_related) as recall:
            packet = self.r.review_packet(self.p, problem, max_chars=5000)
        self.assertTrue(any(event["type"] == "Stop" for event in packet["events"]))
        self.assertNotIn(latest["id"], [event["id"] for event in packet["events"]])
        self.assertEqual(recall.call_count, 1, "an omitted latest source must not use an earlier Stop")
        self.assertEqual(packet["comparison_retrieval"]["fallback"], "delivery_omitted")
        self.assertEqual(packet["related_knowledge"], [])

    def test_visible_counts_refresh_after_optional_tool_context_displaces_references(self):
        self.published("Checkpoint recovery", "Checkpoint recovery requires bounded replay")
        problem = self.phase(goal="Checkpoint recovery")
        baseline = self.r.review_packet(self.p, problem, max_chars=5000)
        self.assertTrue(baseline["related_knowledge"])
        extra = 5000 - len(review_instruction(baseline)) + 100
        with patch("agent_learning.phase_tool_evidence.packet_selection", return_value={"note": "x" * extra}):
            packet = self.r.review_packet(self.p, problem, max_chars=5000)
        self.assertIn("phase_tool_context", packet)
        self.assertEqual(packet["related_knowledge"], [])
        self.assertEqual(packet["comparison_retrieval"]["returned"]["knowledge"], 1)
        self.assertEqual(packet["comparison_retrieval"]["visible"], {"knowledge": 0, "candidates": 0})
        self.assertLessEqual(len(review_instruction(packet)), 5000)

    def test_diagnostics_do_not_change_evidence_or_review_reuse_identity(self):
        packet = self.r.review_packet(self.p, self.phase())
        removed = deepcopy(packet)
        removed.pop("comparison_retrieval")
        self.assertEqual(_fingerprint(packet), _fingerprint(removed))
        cfg = self.r.config["admin"]
        self.assertEqual(review_signature(packet, cfg, _fingerprint(packet)),
                         review_signature(removed, cfg, _fingerprint(removed)))

    def test_small_packet_budget_preserves_evidence_and_never_overflows(self):
        self.published("Checkpoint recovery", "Checkpoint recovery requires bounded replay")
        problem = self.phase()
        for limit in (1500, 2200, 3000, 5000):
            with self.subTest(limit=limit):
                packet = self.r.review_packet(self.p, problem, max_chars=limit)
                self.assertLessEqual(len(review_instruction(packet)), limit)


if __name__ == "__main__":
    unittest.main()
