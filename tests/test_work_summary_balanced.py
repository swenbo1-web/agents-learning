"""Balanced AI summaries reuse raw public history without consuming its coverage."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_drain, admin_once
from agent_learning.admin_lifecycle import claim_call
from agent_learning.admin_queue import claim_dispatch, dispatch_diagnostic
from agent_learning.runtime import ContractError, Runtime
from agent_learning.work_memory import validate_work_memory_config
from tests.test_module6_queue import fake_reviewer


class BalancedWorkSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-balanced-summary-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "Optical calibration with hardware validation pending")
        self.r.config["admin"].update(enabled=True, command=[sys.executable], timeout_seconds=10,
                                      max_input_chars=16000, max_calls_per_day=100)
        self.r.configure_work_summary(self.p, True)

    def capture(self, kind="Stop", *, text="Public optical calibration is still unverified.",
                outcome="reported", conversation="chat", workspace=None, agent=None, **payload):
        task = self.r.task(self.p, conversation) or {}
        if kind in {"Stop", "UserPromptSubmit"}:
            payload["last_assistant_message" if kind == "Stop" else "prompt"] = text
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, kind, payload, outcome=outcome,
                                     agent_id=agent, task_id=task.get("id"), task_revision=task.get("revision"),
                                     task_write_version=task.get("write_version"),
                                     execution_epoch=task.get("execution_epoch"))
            self.r._work_capture_event(self.p, event["id"], workspace or self.workspace, raw=payload)
        return event

    def tool(self, *, outcome="succeeded", **payload):
        return self.capture("PostToolUse", outcome=outcome, tool_name="write_file",
                            tool_input={"path": "src/calibration.py"},
                            tool_response={"output": "calibration edited, physical validation pending"}, **payload)

    def queue(self, workspace=None):
        return self.r._work_queue_summary(self.p, "chat", workspace or self.workspace)

    def sources(self, queued):
        row = self.r.db.execute("SELECT source_snapshots FROM work_summary_links WHERE problem_id=?",
                                (queued["problem_id"],)).fetchone()
        return json.loads(row[0])

    def review(self, queued, decision="candidate"):
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer(decision)) as model:
            result = admin_once(self.r, self.p, queued["problem_id"])
        return result, model.call_count

    def test_strategy_default_validation_and_read_only_policy(self):
        self.assertEqual(validate_work_memory_config({})["summary_strategy"], "balanced")
        self.assertEqual(self.r.work_summary_policy(self.p)["strategy"], "balanced")
        self.assertEqual(validate_work_memory_config({"summary_strategy": "all_windows"})[
            "summary_strategy"], "all_windows")
        for value in ("everything", [], {}, None, True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_work_memory_config({"summary_strategy": value})
        before = self.r.work_summary_policy(self.p)
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        after = self.r.work_summary_policy(self.p)
        self.assertTrue(after["enabled"])
        self.assertEqual(after["version"], before["version"])
        self.r.configure_work_summary(self.p, False)
        self.r.config["work_memory"]["summary_strategy"] = "balanced"
        self.assertFalse(self.r.work_summary_policy(self.p)["enabled"])

    def test_public_only_long_delivery_stays_searchable_without_a_model_request(self):
        original = "Optical calibration and unresolved physical validation. " * 400
        self.capture("UserPromptSubmit", text="Preserve the optical calibration units.")
        delivery = self.capture(text=original)
        before = self.r.work_memory_status(self.p)["memories"]
        with patch("agent_learning.admin.subprocess.Popen") as model:
            for _ in range(3):
                self.assertEqual(self.queue()["status"], "raw_reference_only")
            self.assertIsNone(claim_dispatch(self.r, self.p))
        model.assert_not_called()
        self.assertEqual(self.r.work_memory_status(self.p)["memories"], before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        memories = self.r.db.execute("SELECT * FROM work_memories WHERE project=? AND "
            "json_extract(body,'$.public_message_id')=? AND event_id!=? ORDER BY "
            "json_extract(body,'$.coverage.segment')", (self.p, delivery["id"], delivery["id"])).fetchall()
        self.assertEqual("".join(json.loads(row["body"])["summary"] for row in memories), original)
        card = self.r.work_read(self.p, memories[0]["id"])
        self.assertEqual(card["validation_level"], "reported")
        self.assertTrue(self.r.work_search(self.p, query="Optical calibration")["items"])
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "no_increment")

    def test_later_file_change_is_not_hidden_by_a_long_public_prefix(self):
        self.r.config["work_memory"]["summary_max_events"] = 2
        for number in range(12):
            recent = self.capture(text=f"Public optical note {number}: " + "unverified calibration details. " * 90)
        changed = self.tool()
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        self.assertIn(changed["id"], {item["event_id"] for item in self.sources(queued)})
        self.assertTrue(any(item["projection"].get("public_message_id") == recent["id"]
                            for item in self.sources(queued)))
        self.assertEqual(queued["source_count"], 2)
        result, calls = self.review(queued)
        self.assertEqual((result["status"], calls), ("candidate", 1), result)
        self.assertEqual(result["continuation"]["status"], "raw_reference_only")

    def test_failure_and_user_correction_remain_exact_reported_sources(self):
        self.capture("UserPromptSubmit", text="Preserve the optical calibration units.")
        failure = self.tool(outcome="failed")
        correction = self.capture("UserPromptSubmit", text="更正：这些标定结果尚未通过硬件验收。")
        self.capture(text="The optical calibration remains unverified despite the observed edit.")
        queued = self.queue()
        ids = {item["event_id"] for item in self.sources(queued)}
        self.assertTrue({failure["id"], correction["id"]} <= ids)
        result, calls = self.review(queued)
        self.assertEqual((result["status"], calls), ("candidate", 1), result)
        self.assertEqual(result["validation_level"], "model_reported")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        failure_memory = self.r.db.execute("SELECT body FROM work_memories WHERE event_id=?", (failure["id"],)).fetchone()
        self.assertEqual(json.loads(failure_memory[0])["type"], "tool_failure")

    def test_new_public_supplement_can_replace_an_awaiting_evidence_window(self):
        self.tool(outcome="failed")
        self.capture(text="Optical calibration failed; need physical measurements.")
        first = self.queue()
        result, calls = self.review(first, "needs_evidence")
        self.assertEqual((result["status"], calls), ("needs_evidence", 1))
        self.assertEqual(self.queue()["status"], "awaiting_evidence")
        supplement = self.capture(text="Newly reported physical measurements: calibration still requires independent validation.")
        replacement = self.queue()
        self.assertEqual(replacement["status"], "pending", replacement)
        self.assertNotEqual(first["problem_id"], replacement["problem_id"])
        self.assertIn(supplement["id"], {item["event_id"] for item in self.sources(replacement)})

    def test_unfinished_public_message_does_not_trigger_a_new_summary(self):
        self.capture("UserPromptSubmit", text="Preserve optical units.")
        self.capture(text="Partial optical analysis that cannot be treated as a completed delivery.", completion="partial")
        result = self.queue()
        self.assertEqual(result["status"], "raw_reference_only")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links").fetchone()[0], 0)

    def test_unfitting_material_does_not_pay_to_summarize_only_public_context(self):
        self.r.config["admin"]["max_input_chars"] = 4000
        event = self.tool()
        memory = self.r.db.execute("SELECT id,body FROM work_memories WHERE event_id=?", (event["id"],)).fetchone()
        body = json.loads(memory["body"])
        body["summary"] = "Bounded-test oversize substantive observation " * 200
        self.r.db.execute("UPDATE work_memories SET body=? WHERE id=?", (json.dumps(body), memory["id"]))
        self.capture(text="Short public calibration context.")
        self.capture("UserPromptSubmit", text="Keep unverified status explicit.")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.queue()
        self.assertEqual(result["status"], "input_budget_exceeded", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links").fetchone()[0], 0)
        model.assert_not_called()

    def test_switching_strategy_cannot_retry_a_paid_unknown_window(self):
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        self.capture("UserPromptSubmit", text="Preserve optical units.")
        self.capture(text="Optical results remain unverified.")
        first = self.queue()
        with self.r.transaction():
            call = claim_call(self.r, self.p, first["problem_id"], 1, "fixture", self.r.host_epoch(), 10)
            self.r.db.execute("UPDATE admin_calls SET state='timeout_unknown' WHERE id=?", (call["call_id"],))
            self.r.db.execute("UPDATE work_summary_links SET call_id=? WHERE problem_id=?", (call["call_id"], first["problem_id"]))
        self.r.config["work_memory"]["summary_strategy"] = "balanced"
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.queue()
        self.assertEqual(result["status"], "insufficient_delta", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls").fetchone()[0], "timeout_unknown")
        model.assert_not_called()

    def test_unstarted_public_window_is_retired_without_model_or_coverage_receipt(self):
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        self.capture("UserPromptSubmit", text="Keep optical units explicit.")
        self.capture(text="Public optical analysis remains unverified.")
        first = self.queue()
        self.r.config["work_memory"]["summary_strategy"] = "balanced"
        self.assertEqual(self.queue()["status"], "raw_reference_only")
        self.assertEqual(self.r.db.execute("SELECT state FROM work_summary_links WHERE problem_id=?",
            (first["problem_id"],)).fetchone()[0], "stale")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)

    def test_same_timestamp_retains_original_event_order_in_both_strategies(self):
        for strategy in ("balanced", "all_windows"):
            with self.subTest(strategy=strategy):
                self.r.config["work_memory"]["summary_strategy"] = strategy
                self.r.task_update(self.p, "chat", "Timestamp test " + strategy)
                first = self.tool()
                second = self.capture(text="Unverified optical calibration after the file change.")
                self.r.db.execute("UPDATE work_memories SET created=100 WHERE event_id IN (?,?)",
                                  (first["id"], second["id"]))
                queued = self.queue()
                self.assertEqual([source["event_id"] for source in self.sources(queued)],
                                 [first["id"], second["id"]])

    def unknown_tool_window(self):
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        self.tool()
        self.capture(text="Public optical result remains unverified.")
        queued = self.queue()
        with self.r.transaction():
            call = claim_call(self.r, self.p, queued["problem_id"], 1, "fixture", self.r.host_epoch(), 10)
            self.r.db.execute("UPDATE admin_calls SET state='timeout_unknown' WHERE id=?", (call["call_id"],))
            self.r.db.execute("UPDATE work_summary_links SET call_id=? WHERE problem_id=?", (call["call_id"], queued["problem_id"]))
        self.r.config["work_memory"]["summary_strategy"] = "balanced"
        self.assertEqual(self.queue()["status"], "insufficient_delta")
        return call

    def test_replayed_business_observations_cannot_bypass_stale_unknown_lock(self):
        self.unknown_tool_window()
        repeat = self.tool()
        self.capture(text="Public optical result remains unverified.")
        self.assertEqual(self.queue()["status"], "insufficient_delta")
        independent = self.capture("PostToolUse", outcome="succeeded", tool_name="write_file",
            tool_input={"path": "src/calibration.py"}, tool_response={"output": "New independent optical measurement."})
        self.capture(text="New independent optical measurement still needs physical validation.")
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        ids = {source["event_id"] for source in self.sources(queued)}
        self.assertIn(independent["id"], ids)
        self.assertNotIn(repeat["id"], ids)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_changed_outcome_is_an_independent_observation_despite_same_payload(self):
        self.unknown_tool_window()
        failure = self.tool(outcome="failed")
        self.capture(text="Optical validation now failed and needs diagnosis.")
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        self.assertIn(failure["id"], {source["event_id"] for source in self.sources(queued)})

    def test_explicit_retry_and_a_new_task_preserve_their_own_eligibility(self):
        call = self.unknown_tool_window()
        self.r.db.execute("UPDATE admin_calls SET retry_allowed=1 WHERE id=?", (call["call_id"],))
        self.assertEqual(self.queue()["status"], "pending")
        self.r.task_update(self.p, "chat", "A separate optical measurement task")
        current = self.tool()
        self.capture(text="Public optical result remains unverified.")
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        self.assertIn(current["id"], {source["event_id"] for source in self.sources(queued)})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_other_checkout_task_and_subagent_deltas_do_not_authorize_a_window(self):
        other = Path(self.temp.name) / "other-checkout"
        other.mkdir()
        self.tool(workspace=other)
        self.tool(agent="worker-1")
        self.r.task_update(self.p, "chat", "Another unrelated calibration task")
        self.capture("UserPromptSubmit", text="Keep the new task separate.")
        self.capture(text="New task has only public optical analysis.")
        self.assertEqual(self.queue()["status"], "raw_reference_only")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links").fetchone()[0], 0)

    def test_stage_mode_uses_phase_seal_instead_of_work_summary_queue(self):
        with self.r.transaction(), patch("agent_learning.phase_review.scope", return_value="fixture-phase"), \
                patch("agent_learning.phase_review.seal") as seal, patch.object(self.r, "_work_queue_summary") as queue:
            event, _ = self.r._event(self.p, "chat", "Stop", {"last_assistant_message": "Optical work delivered."}, outcome="reported")
            self.r._work_capture_safely(self.p, event["id"], self.workspace,
                raw={"last_assistant_message": "Optical work delivered."}, summarize=True, conversation="chat")
        seal.assert_called_once()
        queue.assert_not_called()

    def test_all_windows_still_drains_long_public_segments_and_balanced_spends_zero(self):
        self.r.config["work_memory"]["summary_max_events"] = 2
        self.capture(text="Public optical calibration requires hardware validation. " * 220)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            self.assertEqual(self.queue()["status"], "raw_reference_only")
            balanced = admin_drain(self.r, self.p, max_seconds=30)
        self.assertEqual((balanced["invocations"], model.call_count), (0, 0), balanced)
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        self.assertEqual(self.queue()["status"], "pending")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            old = admin_drain(self.r, self.p, max_seconds=30)
        self.assertGreater(old["invocations"], 1, old)
        self.assertEqual(old["invocations"], model.call_count)
        self.assertGreater(self.r.work_memory_status(self.p)["model_summaries"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
