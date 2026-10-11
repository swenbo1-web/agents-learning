"""第五模块公开入口与知识变化提醒的真实隔离集成检查。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.cli import main
from agent_learning.event_capture import prepare_event
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime


class Module5IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module5-integration-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "compiler error", {"mode": "analysis"})
        handle = self.r.register_context(self.p, "chat")["context_handle"]
        self.worker = Server(self.r, self.p, context_handle=handle)

    def feedback_args(self, **extra):
        return dict(summary="compiler error", expected_task_id=self.task["id"],
                    expected_task_revision=self.task["revision"], **extra)

    def publish(self):
        problem = self.r.feedback(self.p, "chat", "compiler error")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(
            self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler error", content="Check compiler paths", category="operation",
            applicability="The recorded compiler environment", limitations="Reported only",
            evidence_refs=[event["id"] for event in packet["events"]], validation_level="reported")
        return self.r.publish(self.p, candidate["candidate_id"])

    def test_mcp_feedback_retry_returns_first_receipt_without_new_event(self):
        args = self.feedback_args(idempotency_key="feedback-1")
        first = self.worker.call("case_feedback", args)
        self.assertEqual(first, self.worker.call("case_feedback", args))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Feedback'").fetchone()[0], 1)
        with self.assertRaises(ContractError):
            self.worker.call("case_feedback", dict(args, summary="different observation"))

    def test_counterexample_survives_bounded_read_and_reaches_hook_notification(self):
        published = self.publish()
        knowledge_id = published["knowledge_id"]
        before = self.worker.call("knowledge_read", {"knowledge_id": knowledge_id})
        self.worker.call("case_feedback", self.feedback_args(
            knowledge_id=knowledge_id, knowledge_revision=published["revision"],
            feedback_kind="counterexample", idempotency_key="counterexample-1"))
        after = self.r.read(self.p, knowledge_id)
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertEqual(after["source_health"], "valid")
        self.assertEqual(after["pending_counterexample_count"], 1)
        note = self.r.handle_hook(dict(hook_event_name="PreToolUse", session_id="chat",
                                      cwd=str(self.workspace), tool_name="Read", tool_use_id="notice"))
        self.assertIn("待核实反例=1", note["hookSpecificOutput"]["additionalContext"])
        page = self.worker.call("knowledge_read", {"knowledge_id": knowledge_id})
        self.assertEqual(page["feedback_status"], "pending_counterexample")
        preview = self.worker.call("knowledge_search", {"query": "compiler error"})
        self.assertEqual(preview["items"][0]["pending_counterexample_count"], 1)

    def test_public_packet_pages_require_same_material_revision(self):
        for number in range(5):
            problem = self.r.feedback(self.p, "chat", "compiler error", resolution=f"attempt {number} " + "x" * 500)
        admin = Server(self.r, self.p, role="admin")
        packet = admin.call("review_packet", {"problem_id": problem["id"], "max_chars": 3000})
        self.assertTrue(packet["incomplete"])
        self.assertGreater(packet["next_offset"], 0)
        args = dict(problem_id=problem["id"], offset=packet["next_offset"],
                    expected_revision=packet["problem"]["revision"], max_chars=3000)
        self.assertTrue(admin.call("review_packet", args)["events"])
        self.r.feedback(self.p, "chat", "compiler error", resolution="new fact")
        with self.assertRaises(ContractError):
            admin.call("review_packet", args)

    def test_cli_feedback_retries_use_the_same_durable_receipt(self):
        arguments = ["--home", str(self.r.home), "--workspace", str(self.workspace), "feedback",
                     "--conversation", "chat", "--summary", "CLI observation", "--idempotency-key", "cli-1"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(arguments), 0)
            self.assertEqual(main(arguments), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM feedback_receipts").fetchone()[0], 1)

    def test_event_retry_checks_full_digest_when_visible_projection_is_equal(self):
        self.r.config["event_max_chars"] = 128
        first = {"output": "head " + "x" * 500 + "a" * 9000 + "x" * 500 + " tail"}
        second = {"output": "head " + "x" * 500 + "b" * 9000 + "x" * 500 + " tail"}
        self.assertEqual(prepare_event(first, 128)[0], prepare_event(second, 128)[0])
        self.r.record_event(self.p, "chat", "PostToolUse", first, event_id="stable-id")
        with self.assertRaises(ContractError):
            self.r.record_event(self.p, "chat", "PostToolUse", second, event_id="stable-id")


if __name__ == "__main__":
    unittest.main()
