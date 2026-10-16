"""增量工作摘要复用管理员账本，默认关闭；仅隔离 SQLite 与假模型。"""
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_queue import claim_dispatch
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class WorkSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-work-summary-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "state", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "animation mapping")
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=100,
                                      max_input_chars=16000, timeout_seconds=10, auto_publish=True)
        self.r.config["work_memory"].update(summary_enabled=True)

    def event(self, kind="PostToolUse", *, outcome="succeeded", **payload):
        task = self.r.task(self.p, "chat")
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", kind, payload, outcome=outcome,
                                     task_id=task["id"], task_revision=task["revision"],
                                     task_write_version=task["write_version"], execution_epoch=task["execution_epoch"])
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return event

    def sources(self):
        written = self.event(tool_name="write_file", tool_input={"path": "src/mapping.py"},
                             tool_response={"exit_code": 0, "output": "animation map updated"})
        checked = self.event(tool_name="test", tool_input={"command": "pytest"},
                             tool_response={"exit_code": 0, "output": "unit checks passed, gameplay not accepted"})
        return [written, checked]

    def queue(self):
        return self.r._work_queue_summary(self.p, "chat", self.workspace, expected_epoch=self.r.host_epoch())

    def counts(self):
        return {table: self.r.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("admin_calls", "candidates", "active", "work_summary_links")}

    def run_review(self, problem, reviewer=None):
        with patch("agent_learning.admin.subprocess.Popen", reviewer or fake_reviewer("candidate")):
            return admin_once(self.r, self.p, problem["problem_id"])

    def test_default_is_off_and_queue_never_invokes_a_model(self):
        self.sources()
        self.r.config["work_memory"]["summary_enabled"] = False
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = self.queue()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.counts()["admin_calls"], 0)
        self.assertEqual(self.counts()["work_summary_links"], 0)
        popen.assert_not_called()

    def test_disabled_administrator_or_master_switch_prevents_queue(self):
        self.sources()
        self.r.config["admin"]["enabled"] = False
        self.assertEqual(self.queue()["status"], "disabled")
        self.r.config["admin"]["enabled"] = True
        self.r.config["work_memory"]["enabled"] = False
        self.assertEqual(self.queue()["status"], "disabled")

    def test_read_only_work_does_not_create_a_summary_request(self):
        self.event(tool_name="read_file", tool_input={"path": "src/mapping.py"}, tool_response={"exit_code": 0})
        self.assertEqual(self.queue()["status"], "insufficient_delta")

    def test_request_sources_are_exact_and_do_not_duplicate_original_payload(self):
        events = self.sources()
        result = self.queue()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["invocations"], 0)
        problem_events = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?",
                                                           (result["problem_id"],))]
        self.assertEqual(problem_events, [result["request_event_id"]])
        frozen = self.r.work_summary_packet(self.p, result["problem_id"])
        self.assertEqual({event["id"] for event in frozen["events"]}, {event["id"] for event in events})
        requested = json.loads(self.r.db.execute("SELECT payload FROM events WHERE id=?",
                                                (result["request_event_id"],)).fetchone()[0])
        self.assertEqual(requested["source_event_refs"], [event["id"] for event in events])
        self.assertNotIn("tool_response", requested)
        self.assertEqual(self.queue()["status"], "insufficient_delta")
        self.assertEqual(self.counts()["work_summary_links"], 1)

    def test_repeated_business_observations_do_not_spend_another_call(self):
        self.sources()
        first = self.queue()
        self.run_review(first)
        self.sources()
        self.assertEqual(self.queue()["status"], "insufficient_delta")
        self.assertEqual(self.counts()["admin_calls"], 1)

    def test_accepted_summary_is_searchable_and_never_a_knowledge_candidate(self):
        self.sources()
        result = self.run_review(self.queue())
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["purpose"], "work_summary")
        self.assertEqual(result["validation_level"], "model_reported")
        self.assertNotIn("candidate_id", result)
        self.assertEqual(result["publication"]["status"], "not_published")
        self.assertEqual(self.counts()["candidates"], 0)
        self.assertEqual(self.counts()["active"], 0)
        card = self.r.work_read(self.p, result["memory_id"])
        self.assertEqual(card["type"], "work_summary")
        self.assertEqual(card["validation_level"], "model_reported")
        file_matches = self.r.work_search(self.p, file_path="src/mapping.py", workspace=self.workspace)["items"]
        self.assertIn(result["memory_id"], [item["memory_id"] for item in file_matches])
        self.assertEqual(result["usage"]["purpose"], "work_summary")
        self.assertEqual(self.r.db.execute("SELECT state FROM work_summary_links").fetchone()[0], "accepted")
        self.assertEqual(self.r.db.execute("SELECT state FROM review_states").fetchone()[0], "work_summary_ready")

    def test_summary_off_does_not_dispatch_or_consume_queued_work(self):
        self.sources()
        queued = self.queue()
        self.r.config["work_memory"]["summary_enabled"] = False
        self.assertIsNone(claim_dispatch(self.r, self.p))
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertNotEqual(result["status"], "candidate")
        popen.assert_not_called()
        self.assertEqual(self.counts()["admin_calls"], 0)

    def test_task_change_before_select_supersedes_work_without_model_call(self):
        self.sources()
        queued = self.queue()
        self.r.task_update(self.p, "chat", "new task purpose")
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertEqual(result["status"], "needs_evidence")
        popen.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT state FROM work_summary_links").fetchone()[0], "stale")

    def test_task_change_during_model_run_refuses_summary_writeback(self):
        self.sources()
        queued = self.queue()
        def change(packet, process):
            self.r.task_update(self.p, "chat", "new task purpose")
        result = self.run_review(queued, fake_reviewer("candidate", change))
        self.assertNotEqual(result["status"], "candidate")
        self.assertEqual(self.counts()["admin_calls"], 1)
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 0)

    def test_source_change_during_model_run_refuses_summary_writeback(self):
        events = self.sources()
        queued = self.queue()
        def change(packet, process):
            with self.r.transaction():
                self.r.db.execute("UPDATE events SET payload=? WHERE id=?", ('{"changed":true}', events[0]["id"]))
        result = self.run_review(queued, fake_reviewer("candidate", change))
        self.assertNotEqual(result["status"], "candidate")
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 0)

    def test_projected_source_can_only_produce_a_reported_work_summary(self):
        self.event(tool_name="write_file", tool_input={"path": "src/mapping.py"}, tool_response={"output": "x" * 16000})
        self.event("Stop", outcome="reported", last_assistant_message="changed, validation missing")
        queued = self.queue()
        packet = self.r.work_summary_packet(self.p, queued["problem_id"])
        self.assertTrue(packet["evidence_truncated"])
        self.assertFalse(packet["incomplete"])
        result = self.run_review(queued)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["validation_level"], "model_reported")
        self.assertEqual(self.counts()["candidates"], 0)
        self.assertEqual(self.counts()["active"], 0)

    def test_same_global_budget_applies_to_summaries(self):
        self.sources()
        queued = self.queue()
        self.r.config["admin"]["max_calls_per_day"] = 0
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(self.counts()["admin_calls"], 0)
        popen.assert_not_called()

    def test_governance_proposal_in_summary_response_is_rejected(self):
        self.sources()
        queued = self.queue()
        base = fake_reviewer("candidate")
        class Reviewer(base):
            def communicate(process, data=None, timeout=None):
                base.communicate(process, data, timeout)
                response = json.loads(process.output.read_text(encoding="utf-8"))
                response["proposal"] = {"action": "publish", "knowledge_id": None, "expected_revision": None}
                process.output.write_text(json.dumps(response), encoding="utf-8")
                return None, None
        result = self.run_review(queued, Reviewer)
        self.assertNotEqual(result["status"], "candidate")
        self.assertEqual(self.counts()["candidates"], 0)
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 0)

    def test_untrusted_claim_of_tool_success_is_rejected(self):
        self.sources()
        queued = self.queue()
        base = fake_reviewer("candidate")
        class Reviewer(base):
            def communicate(process, data=None, timeout=None):
                base.communicate(process, data, timeout)
                response = json.loads(process.output.read_text(encoding="utf-8"))
                response["candidate"]["validation_level"] = "tool_success"
                process.output.write_text(json.dumps(response), encoding="utf-8")
                return None, None
        result = self.run_review(queued, Reviewer)
        self.assertNotEqual(result["status"], "candidate")
        self.assertEqual(self.counts()["active"], 0)

    def test_historical_material_and_other_checkout_are_not_queued(self):
        self.event("HistoryImport", content="paused historical materials")
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.sources()
        result = self.r._work_queue_summary(self.p, "chat", other)
        self.assertEqual(result["status"], "insufficient_delta")
        self.assertEqual(self.counts()["work_summary_links"], 0)

    def test_old_summary_receipt_cannot_trigger_an_extra_call(self):
        self.sources()
        queued = self.queue()
        self.run_review(queued)
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertNotEqual(result["status"], "reserved")
        self.assertEqual(self.counts()["admin_calls"], 1)
        popen.assert_not_called()

    def test_changed_write_version_cannot_retry_an_unknown_equivalent_summary(self):
        self.sources()
        queued = self.queue()
        def change(packet, process):
            self.r.task_update(self.p, "chat", "animation mapping", status="paused")
            raise subprocess.TimeoutExpired("controlled-review", process.timeout)
        first = self.run_review(queued, fake_reviewer("candidate", change))
        self.assertEqual(first["status"], "timeout_unknown")
        replacement = self.queue()
        self.assertEqual(replacement["status"], "insufficient_delta")
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p, queued["problem_id"])
        self.assertIn(result["status"], {"review_required", "idle"})
        self.assertEqual(self.counts()["admin_calls"], 1)
        popen.assert_not_called()

    def test_another_host_cannot_dispatch_the_queued_summary(self):
        self.sources()
        queued = self.queue()
        other = Runtime(self.r.home, config={"admin": {"enabled": True, "command": [sys.executable],
                        "antigravity_command": [sys.executable]}, "work_memory": {"summary_enabled": True}},
                        host="antigravity")
        self.addCleanup(other.close)
        self.assertIsNone(claim_dispatch(other, self.p))
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(other, self.p, queued["problem_id"])
        self.assertNotEqual(result["status"], "candidate")
        popen.assert_not_called()

    def test_long_summary_keeps_limitations_in_its_bounded_body(self):
        self.sources()
        queued = self.queue()
        base = fake_reviewer("candidate")
        class Reviewer(base):
            def communicate(process, data=None, timeout=None):
                base.communicate(process, data, timeout)
                response = json.loads(process.output.read_text(encoding="utf-8"))
                response["candidate"]["content"] = "valid work observation " * 500
                response["candidate"]["limitations"] = "gameplay outcome has not been accepted"
                process.output.write_text(json.dumps(response), encoding="utf-8")
                return None, None
        result = self.run_review(queued, Reviewer)
        self.assertEqual(result["status"], "candidate")
        body = json.loads(self.r.db.execute("SELECT body FROM work_memories WHERE project=? AND id=?",
                         (self.p, result["memory_id"])).fetchone()[0])
        self.assertLessEqual(len(body["summary"]), 4000)
        self.assertIn("gameplay outcome has not been accepted", body["summary"])


if __name__ == "__main__":
    unittest.main()
