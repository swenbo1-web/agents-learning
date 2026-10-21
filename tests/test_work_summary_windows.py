"""Frozen summary windows continue locally; stale policies cannot retry paid unknown calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_lifecycle import claim_call
from agent_learning.runtime import Runtime
from tests.test_module6_queue import fake_reviewer


class WorkSummaryWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-summary-window-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(enabled=True, command=[sys.executable], timeout_seconds=10,
                                      max_input_chars=16000, max_calls_per_day=100)
        self.r.config["work_memory"].update(summary_min_events=2, summary_max_events=2,
                                            summary_strategy="all_windows")
        self.r.configure_work_summary(self.p, True)

    def capture(self, text, kind="Stop"):
        payload = {"last_assistant_message" if kind == "Stop" else "prompt": text}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", kind, payload, outcome="reported")
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return event

    def queue(self):
        return self.r._work_queue_summary(self.p, "chat", self.workspace)

    def sources(self):
        self.capture("Use explicit optical units.", "UserPromptSubmit")
        self.capture("Compared optical calibration; hardware validation remains pending.")

    def link(self, problem):
        return dict(self.r.db.execute("SELECT * FROM work_summary_links WHERE problem_id=?",
                                     (problem["problem_id"],)).fetchone())

    def review(self, problem, decision="candidate"):
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer(decision)) as model:
            result = admin_once(self.r, self.p, problem["problem_id"])
        return result, model.call_count

    def toggle(self):
        self.r.configure_work_summary(self.p, False)
        self.r.configure_work_summary(self.p, True)

    def reserve_unknown(self, problem, state="timeout_unknown"):
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["problem_id"], 1, "fixture-window",
                              self.r.host_epoch(), 10)
            self.r.db.execute("UPDATE work_summary_links SET call_id=? WHERE problem_id=?",
                              (call["call_id"], problem["problem_id"]))
            self.r.db.execute("UPDATE admin_calls SET state=? WHERE id=?", (state, call["call_id"]))
        return call

    def test_unstarted_window_can_requeue_after_policy_lifecycle_change(self):
        self.sources()
        first = self.queue()
        self.toggle()
        second = self.queue()
        self.assertEqual(second["status"], "pending", second)
        self.assertNotEqual(first["problem_id"], second["problem_id"])
        self.assertEqual(self.link(first)["state"], "stale")
        result, calls = self.review(second)
        self.assertEqual(result["status"], "candidate", result)
        self.assertEqual(calls, 1)

    def test_unstarted_window_can_requeue_after_account_epoch_change(self):
        self.sources()
        first = self.queue()
        self.r.account_changed("fixture-new-session")
        second = self.queue()
        self.assertEqual(second["status"], "pending", second)
        self.assertNotEqual(first["problem_id"], second["problem_id"])
        self.assertEqual(self.link(first)["state"], "stale")

    def test_unknown_paid_sources_remain_blocked_after_policy_lifecycle_change(self):
        self.sources()
        first = self.queue()
        self.reserve_unknown(first)
        self.toggle()
        with patch("agent_learning.admin.subprocess.Popen") as model:
            second = self.queue()
        self.assertEqual(second["status"], "insufficient_delta", second)
        self.assertEqual(self.link(first)["state"], "stale")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        model.assert_not_called()

    def test_known_not_started_sources_can_requeue_with_new_policy(self):
        self.sources()
        first = self.queue()
        self.reserve_unknown(first, "not_started")
        self.toggle()
        second = self.queue()
        self.assertEqual(second["status"], "pending", second)
        self.assertNotEqual(first["problem_id"], second["problem_id"])

    def test_acceptance_queues_exactly_one_next_window_without_another_hook(self):
        self.capture("Public optical analysis. " * 800)
        first = self.queue()
        first_refs = set(json.loads(self.link(first)["source_record_ids"]))
        result, calls = self.review(first)
        self.assertEqual(result["status"], "candidate", result)
        self.assertEqual(calls, 1)
        next_batch = result["continuation"]
        self.assertEqual(next_batch["status"], "pending", next_batch)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links WHERE state='pending'").fetchone()[0], 1)
        next_refs = set(json.loads(self.link(next_batch)["source_record_ids"]))
        self.assertFalse(first_refs & next_refs)
        summary_memory = result["memory_id"]
        self.assertNotIn(summary_memory, next_refs)
        self.assertTrue(all(self.r.db.execute("SELECT event_id FROM work_memories WHERE id=?", (ref,)).fetchone()[0]
                            for ref in next_refs))

    def test_skip_consumes_window_and_queues_next_without_summary_or_publication(self):
        self.capture("Public optical analysis. " * 800)
        first = self.queue()
        result, calls = self.review(first, "skip")
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(calls, 1)
        self.assertEqual(self.link(first)["state"], "skipped")
        self.assertEqual(result["continuation"]["status"], "pending")
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_small_accepted_window_does_not_requeue_its_own_summary(self):
        self.sources()
        result, calls = self.review(self.queue())
        self.assertEqual(result["status"], "candidate", result)
        self.assertEqual(calls, 1)
        self.assertEqual(result["continuation"]["status"], "insufficient_delta")
        self.assertEqual(self.queue()["status"], "insufficient_delta")

    def test_consumed_public_segments_do_not_requeue_the_parent_preview(self):
        self.r.config["work_memory"].update(summary_max_events=4)
        self.capture("Public optical analysis still requires hardware validation. " * 45)
        result, calls = self.review(self.queue())
        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "candidate", result)
        self.assertEqual(result["continuation"]["status"], "insufficient_delta", result)
        self.assertEqual(self.queue()["status"], "insufficient_delta")

    def test_covered_previews_cannot_starve_new_work_beyond_scan_limit(self):
        self.r.config["work_memory"].update(summary_max_events=40)
        self.r.config["admin"].update(max_input_chars=200000)
        for index in range(9):
            self.capture(f"Phase {index}: " + "Public optical results pending hardware validation. " * 30)
        first, calls = self.review(self.queue())
        self.assertEqual(calls, 1)
        self.assertEqual(first["continuation"]["status"], "insufficient_delta", first)
        self.r.config["work_memory"].update(summary_max_events=2)
        self.capture("New optical correction with an explicit unit convention.", "UserPromptSubmit")
        self.capture("New optical result still needs hardware validation.")
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        self.assertEqual(queued["source_count"], 2, queued)

    def test_last_single_observation_continues_without_waiting_for_another_hook(self):
        self.sources()
        self.capture("The optical calibration still requires a separate hardware comparison.")
        result, calls = self.review(self.queue())
        next_batch = result["continuation"]
        self.assertEqual(calls, 1)
        self.assertEqual(next_batch["status"], "pending", result)
        self.assertEqual(next_batch["source_count"], 1)
        second, calls = self.review(next_batch)
        self.assertEqual(second["status"], "candidate", second)
        self.assertEqual(calls, 1)
        self.assertEqual(second["continuation"]["status"], "insufficient_delta")

    def test_drain_advances_multiple_windows_with_shared_call_limit(self):
        from agent_learning.admin import admin_drain
        self.capture("Public optical analysis. " * 800)
        self.queue()
        self.r.config["admin"].update(max_calls_per_batch=2, max_calls_per_day=3)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            result = admin_drain(self.r, self.p, max_seconds=30)
        self.assertEqual(result["invocations"], 3, result)
        self.assertEqual(model.call_count, 3)
        self.assertGreaterEqual(result["batches"], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links WHERE state='pending'").fetchone()[0], 1)

    def test_needs_evidence_window_recovers_with_replacement_when_new_observation_arrives(self):
        self.r.config["work_memory"].update(summary_min_events=2, summary_max_events=4)
        self.sources()
        first = self.queue()
        res1, calls1 = self.review(first, "needs_evidence")
        self.assertEqual(res1["status"], "needs_evidence")
        self.assertEqual(calls1, 1)
        self.assertEqual(self.link(first)["state"], "awaiting_evidence")
        # 无新观察记录时，再次排队不会创建重复窗口
        same = self.queue()
        self.assertEqual(same["status"], "awaiting_evidence")
        self.assertTrue(same["reused"])
        # 新增补充观察记录后，自动生成包含旧观察与新补证的替代窗口
        self.capture("Supplementary hardware calibration log confirming unit conversion.")
        replacement = self.queue()
        self.assertEqual(replacement["status"], "pending", replacement)
        self.assertNotEqual(replacement["problem_id"], first["problem_id"])
        self.assertEqual(self.link(first)["state"], "superseded")
        res2, calls2 = self.review(replacement, "candidate")
        self.assertEqual(res2["status"], "candidate", res2)
        self.assertEqual(calls2, 1)
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 1)

    def test_small_input_budget_skips_oversize_first_item_and_reports_budget_exceeded_when_blocked(self):
        self.r.config["admin"]["max_input_chars"] = 4000
        self.capture("Oversize observation payload " * 120, "UserPromptSubmit")
        all_blocked = self.r._work_queue_summary(self.p, "chat", self.workspace, _continuation=True)
        self.assertEqual(all_blocked["status"], "input_budget_exceeded", all_blocked)
        self.assertGreater(all_blocked["required_max_input_chars"], 4000)
        # 后续加入两条短记录后，首条超长记录不会堵死后续短记录成窗
        self.capture("Short prompt note.", "UserPromptSubmit")
        self.capture("Short stop note.")
        queued = self.queue()
        self.assertEqual(queued["status"], "pending", queued)
        self.assertEqual(queued["source_count"], 2)

    def test_mixed_task_and_untasked_observations_deduplicate_by_original_snapshot_binding(self):
        self.capture("Untasked initial observation A.", "UserPromptSubmit")
        self.r.task_update(self.p, "chat", "Optical task goal")
        self.capture("Tasked observation B.")
        first = self.queue()
        self.assertEqual(first["status"], "pending")
        res, calls = self.review(first, "candidate")
        self.assertEqual(res["status"], "candidate")
        self.assertEqual(calls, 1)
        # 删除 source_record_ids 模拟跨记录 ID 场景，仅靠 source_snapshots 的 business_digest 去重
        self.r.db.execute("UPDATE work_summary_links SET source_record_ids='[]' WHERE problem_id=?", (first["problem_id"],))
        again = self.r._work_queue_summary(self.p, "chat", self.workspace, _continuation=True)
        self.assertEqual(again["status"], "insufficient_delta", again)

    def test_work_capture_persists_when_optional_summary_queue_raises(self):
        before = self.r.work_memory_status(self.p)["memories"]
        with patch.object(self.r, "_work_queue_summary", side_effect=RuntimeError("unexpectederror") if False else ValueError("summaryboom")):
            with self.r.transaction():
                ev, _ = self.r._event(self.p, "chat", "Stop", {"last_assistant_message": " public delivery note "}, outcome="reported")
                self.r._work_capture_safely(self.p, ev["id"], self.workspace, raw={"last_assistant_message": " public delivery note "},
                                            summarize=True, conversation="chat")
        after = self.r.work_memory_status(self.p)["memories"]
        self.assertEqual(after, before + 1)
        self.assertEqual(self.r.status(self.p)["metrics"].get("work_summary_queue_failed"), 1)


if __name__ == "__main__":
    unittest.main()
