"""Public conversation continuity uses reported evidence, never task permissions or real models."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class ConversationContinuityTests(unittest.TestCase):
    def setUp(self):
        self.area = tempfile.TemporaryDirectory(prefix="alp-continuity-")
        self.addCleanup(self.area.cleanup)
        self.workspace = Path(self.area.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.area.name) / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def capture(self, kind, conversation="earlier-chat", task=None, **payload):
        task = task or {}
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, kind, payload, outcome="reported",
                task_id=task.get("id"), task_revision=task.get("revision"),
                task_write_version=task.get("write_version"), execution_epoch=task.get("execution_epoch"))
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return event

    def enable(self):
        self.r.config["admin"].update(enabled=True, command=[sys.executable], timeout_seconds=10,
                                      max_input_chars=16000, auto_publish=True)
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        self.r.configure_work_summary(self.p, True)

    def test_requirement_and_public_analysis_are_searchable_without_tasks_or_approval(self):
        self.capture("UserPromptSubmit", prompt="The renderer must preserve spatial scale and physical units.")
        self.capture("Stop", last_assistant_message="Chosen architecture: separate projection from visibility.")
        items = self.r.work_search(self.p, "projection visibility")["items"]
        self.assertTrue(items)
        self.assertEqual(items[0]["validation_level"], "reported")
        self.assertFalse(items[0]["execution_authority"])
        for table in ("tasks", "candidates", "active", "admin_calls"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_long_public_reply_middle_is_preserved_and_replay_is_idempotent(self):
        text = "opening " * 500 + " OPTICAL_MIDDLE_DECISION " + "ending " * 1200
        event = self.capture("Stop", last_assistant_message=text)
        first = self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0]
        self.assertGreater(first, 3)
        self.assertTrue(self.r.work_search(self.p, "OPTICAL_MIDDLE_DECISION")["items"])
        with self.r.transaction():
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw={"last_assistant_message": text})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], first)

    def test_segment_coverage_retains_host_truncation_and_partial_state(self):
        self.capture("Stop", last_assistant_message="report " * 600, completion="partial",
                     capture_coverage={"finalModelOutput": {"truncated": True}})
        items = self.r.work_search(self.p, "report")["items"]
        self.assertTrue(items)
        self.assertTrue(all(item["completion"] == "partial" and item["coverage"]["truncated"] for item in items))
        self.enable()
        self.assertEqual(self.r._work_queue_summary(self.p, "earlier-chat", self.workspace)["status"], "insufficient_delta")

    def test_segment_coverage_retains_truncated_public_user_prompt(self):
        self.capture("UserPromptSubmit", prompt="optical requirement " * 200, prompt_truncated=True)
        bodies = [json.loads(row[0]) for row in self.r.db.execute("SELECT body FROM work_memories")]
        self.assertGreater(len(bodies), 2)
        self.assertTrue(all(body["coverage"]["truncated"] for body in bodies))
        self.assertTrue(all(not body["coverage"]["full_original_available"] for body in bodies))

    def test_relevance_precedes_recency_of_repeated_errors(self):
        self.capture("Stop", last_assistant_message="render architecture: preserve angular resolution")
        for index in range(35):
            self.capture("Stop", conversation="noisy-chat", last_assistant_message=f"render failure attempt {index}")
        items = self.r.work_search(self.p, "render architecture", limit=3)["items"]
        self.assertIn("architecture", items[0]["summary"])

    def test_new_chat_has_a_same_checkout_handoff_without_old_task_authority(self):
        self.capture("Stop", last_assistant_message="Next step is to compare the optical calibration branches.")
        self.assertEqual(self.r.work_context_items(self.p, "new-chat", workspace=self.workspace), [])
        items = self.r.work_context_items(self.p, "new-chat", query="optical calibration",
                                          workspace=self.workspace)
        self.assertTrue(items)
        self.assertFalse(items[0]["permission_restored"])
        self.assertFalse(items[0]["execution_authority"])
        other = Path(self.area.name) / "different-checkout"
        other.mkdir()
        self.assertEqual(self.r.work_context_items(self.p, "new-chat", workspace=other), [])
        other_project = self.r.project(other)
        self.assertEqual(self.r.work_context_items(other_project, "new-chat", query="optical calibration",
                                                 workspace=other), [])

    def test_no_task_daily_summary_can_be_reviewed_without_creating_formal_knowledge(self):
        self.capture("UserPromptSubmit", prompt="Keep the optical assumptions explicit.")
        self.capture("Stop", last_assistant_message="Compared projections; physical calibration is still unverified.")
        self.enable()
        queued = self.r._work_queue_summary(self.p, "earlier-chat", self.workspace)
        self.assertEqual(queued["status"], "pending")
        packet = self.r.work_summary_packet(self.p, queued["problem_id"])
        self.assertEqual(packet["scope"], "conversation")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertEqual(result["status"], "candidate", result)
        self.assertEqual(result["validation_level"], "model_reported")
        for table in ("tasks", "candidates", "active"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_mixed_observed_tools_do_not_acquire_current_task_identity(self):
        task = self.r.task_update(self.p, "earlier-chat", "optical calibration")
        self.capture("PostToolUse", tool_name="test", tool_response={"success": True, "output": "calibration checked"})
        self.capture("Stop", task=task, last_assistant_message="Calibration checks completed, user acceptance pending.")
        self.enable()
        queued = self.r._work_queue_summary(self.p, "earlier-chat", self.workspace)
        self.assertEqual(queued["status"], "pending", queued)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertEqual(result["status"], "candidate", result)
        row = self.r.db.execute("SELECT task_id,body FROM work_memories WHERE id=?", (result["memory_id"],)).fetchone()
        self.assertIsNone(row["task_id"])
        self.assertEqual(json.loads(row["body"])["scope"], "conversation")

    def test_disable_then_enable_does_not_revive_an_old_summary_request(self):
        self.capture("UserPromptSubmit", prompt="Use explicit calibration units.")
        self.capture("Stop", last_assistant_message="Selected metric units; calibration remains unverified.")
        self.enable()
        queued = self.r._work_queue_summary(self.p, "earlier-chat", self.workspace)
        self.r.configure_work_summary(self.p, False)
        self.r.configure_work_summary(self.p, True)
        with self.assertRaisesRegex(ContractError, "policy_changed"):
            self.r.work_summary_guard(self.p, queued["problem_id"])

    def test_status_progress_does_not_discard_a_frozen_task_summary(self):
        task = self.r.task_update(self.p, "earlier-chat", "optical calibration")
        self.capture("UserPromptSubmit", task=task, prompt="Keep test assumptions explicit.")
        self.capture("Stop", task=task, last_assistant_message="Calibration compared; hardware validation pending.")
        self.enable()
        queued = self.r._work_queue_summary(self.p, "earlier-chat", self.workspace)
        self.r.task_update(self.p, "earlier-chat", "optical calibration", status="paused")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate")):
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertEqual(result["status"], "candidate", result)
