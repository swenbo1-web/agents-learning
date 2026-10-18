"""覆盖真实审查中出现的遗漏与错误覆盖，而不是重复实现细节。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.hindsight import HindsightError
from agent_learning.runtime import ContractError, Runtime, SCHEMA


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "诊断模块加载", {"mode": "analysis"})

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def hook(self, kind, **extra):
        return self.r.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.workspace), **extra})

    def candidate(self, content="核对模块路径", applicability="Module A", summary="加载失败", metadata=None):
        problem = self.r.feedback(self.p, "chat", summary, category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title="模块加载诊断",
                    content=content, category="operation", applicability=applicability,
                    limitations="仅有报告证据", evidence_refs=[e["id"] for e in packet["events"]], validation_level="reported")
        if metadata is not None:
            body["metadata"] = metadata
        return self.r.submit_candidate(self.p, **body)["candidate_id"]

    def test_long_log_keeps_identity_tail_and_success_link(self):
        self.hook("PreToolUse", tool_use_id="fail", tool_name="Bash", tool_input={"command": "compile module"})
        self.hook("PostToolUse", tool_use_id="fail", tool_name="Bash", tool_input={"command": "compile module"},
                  tool_response={"exit_code": 1, "output": "header " + "x" * 9000 + " FATAL missing include"})
        problem = self.r.problems(self.p)[0]
        packet = self.r.review_packet(self.p, problem["id"])
        payload = next(event["payload"] for event in packet["events"] if event["type"] == "PostToolUse")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertIn("FATAL missing include", payload["tool_response"]["output"])
        self.assertTrue(packet["evidence_truncated"])
        self.assertFalse(packet["incomplete"])
        self.hook("PreToolUse", tool_use_id="repair", tool_name="Bash", tool_input={"command": "compile module"})
        self.hook("PostToolUse", tool_use_id="repair", tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.review_packet(self.p, problem["id"])["problem"]["revision"], problem["revision"] + 1)

    def test_long_knowledge_gets_summary_and_continuation_restores_after_compact(self):
        item = self.r.publish(self.p, self.candidate(content="模块路径 " + "详细步骤。" * 1000))
        first = self.hook("UserPromptSubmit", prompt="模块路径")
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn(item["knowledge_id"], context)
        self.assertIn("summary_only", context)
        self.assertLessEqual(len(context), 2400)
        self.hook("UserPromptSubmit", prompt="继续")
        restored = self.hook("SessionStart", source="compact")
        self.assertIn(item["knowledge_id"], restored["hookSpecificOutput"]["additionalContext"])

    def test_revocation_tracking_survives_scope_change(self):
        item = self.r.publish(self.p, self.candidate())
        self.hook("UserPromptSubmit", prompt="模块加载")
        self.r.task_update(self.p, "chat", "另一项任务", new_task=True)
        self.r.revoke(self.p, item["knowledge_id"], expected_revision=self.r.read(self.p, item["knowledge_id"])["revision"])
        result = self.hook("PreToolUse", tool_use_id="next", tool_name="Bash")
        self.assertIn(item["knowledge_id"], result["hookSpecificOutput"]["additionalContext"])

    def test_same_title_different_environment_coexist_and_updates_compare_revision(self):
        a = self.r.publish(self.p, self.candidate(metadata={"environment": "A"}))
        b = self.r.publish(self.p, self.candidate(applicability="Module B", summary="另一模块加载失败", metadata={"environment": "B"}))
        self.assertNotEqual(a["knowledge_id"], b["knowledge_id"])
        self.assertEqual(len(self.r.search(self.p, "模块加载")["items"]), 2)
        self.assertEqual(self.r.search(self.p, "模块加载", filters={"environment": "A"})["items"][0]["knowledge_id"], a["knowledge_id"])
        revised = self.candidate(content="更新的解决步骤", summary="新增证据")
        with self.assertRaises(ContractError):
            self.r.publish(self.p, revised, knowledge_id=a["knowledge_id"])
        update = self.r.publish(self.p, revised, knowledge_id=a["knowledge_id"], expected_revision=1)
        self.assertEqual(update["revision"], 2)
        self.assertEqual(self.r.publish(self.p, revised, knowledge_id=a["knowledge_id"], expected_revision=1), update)
        with self.assertRaises(ContractError):
            self.r.publish(self.p, revised, knowledge_id=a["knowledge_id"], expected_revision=2)

    def test_exact_conclusion_reuses_active_knowledge_across_cases(self):
        a = self.r.publish(self.p, self.candidate(summary="第一次报错"))
        b = self.r.publish(self.p, self.candidate(summary="第二次相同报错"))
        self.assertEqual(a["knowledge_id"], b["knowledge_id"])
        self.assertTrue(b["deduplicated"])
        self.assertEqual(self.r.status(self.p)["counts"]["active"], 1)

    def test_summary_and_candidate_secrets_are_redacted(self):
        problem = self.r.feedback(self.p, "chat", "password=SYNTHETIC_SECRET failure")
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(packet))
        item = self.r.publish(self.p, self.candidate(content="api_key=SYNTHETIC_KEY; check path"))
        self.assertNotIn("SYNTHETIC_KEY", json.dumps(self.r.read(self.p, item["knowledge_id"])))

    def test_cwd_child_keeps_cancelled_task_and_rejects_unrelated_session(self):
        self.r.config["enforce_task_policy"] = True
        self.r.task_update(self.p, "chat", "诊断模块加载", status="cancelled")
        result = self.hook("PreToolUse", cwd=str(self.workspace / "src"), tool_name="Bash", tool_use_id="denied")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        other = self.r.project(Path(self.temp.name) / "unrelated")
        with self.assertRaises(ContractError):
            self.r.task_update(other, "chat", "另一个项目")
        nested = self.workspace / "independent"
        (nested / ".git").mkdir(parents=True)
        self.assertNotEqual(self.r.project(nested), self.p)

    def test_delivery_result_creates_one_reported_case_per_task_revision(self):
        self.r.task_update(self.p, "chat", "诊断模块加载", status="delivered")
        self.hook("Stop", turn_id="one", last_assistant_message="已完成路径检查；业务验收待确认。")
        self.hook("Stop", turn_id="two", last_assistant_message="补充说明。")
        problems = self.r.problems(self.p)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["category"], "mixed")
        packet = self.r.review_packet(self.p, problems[0]["id"])
        self.assertEqual(packet["task"]["status"], "delivered")
        self.assertTrue(all(e["outcome"] != "succeeded" for e in packet["events"]))

    def test_hindsight_backfill_and_timeout_change_do_not_change_destination(self):
        self.r.publish(self.p, self.candidate())
        self.r.config["hindsight"].update(enabled=True, bank_id="test")
        self.assertEqual(self.r.enqueue_sync(self.p)["enqueued"], 1)
        self.assertEqual(self.r.enqueue_sync(self.p)["enqueued"], 0)
        self.r.config["hindsight"]["timeout_seconds"] = 15
        with patch("agent_learning.hindsight.HindsightClient.retain", return_value={"status": "retained"}):
            self.assertEqual(self.r.sync_once(self.p)["status"], "synced")

    def test_sync_keeps_safe_error_details_and_does_not_retry(self):
        self.r.config["hindsight"].update(enabled=True, bank_id="test")
        self.r.publish(self.p, self.candidate())
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=HindsightError("safe", kind="missing_credentials", write_outcome="not_sent")):
            result = self.r.sync_once(self.p)
        self.assertEqual(result["result"]["kind"], "missing_credentials")
        self.assertEqual(result["result"]["write_outcome"], "not_sent")
        self.assertEqual(self.r.sync_once(self.p)["status"], "idle")

    def test_sync_lease_loss_reports_actual_state(self):
        self.r.config["hindsight"].update(enabled=True, bank_id="test")
        self.r.publish(self.p, self.candidate())
        second = Runtime(self.home)
        def late_result(*args, **kwargs):
            second.db.execute("UPDATE outbox SET state='blocked',lease=NULL,error='other worker'")
            return {"status": "retained"}
        try:
            with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=late_result):
                result = self.r.sync_once(self.p)
            self.assertEqual(result["status"], "lease_lost")
            self.assertEqual(result["current_state"], "blocked")
        finally:
            second.close()

    def test_v1_database_migration_restores_old_task_snapshot(self):
        legacy = Path(self.temp.name) / "legacy"
        legacy.mkdir()
        db = sqlite3.connect(legacy / "runtime.sqlite3")
        db.executescript(SCHEMA)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO projects VALUES('p',0,0)")
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("e", "p", "old-chat", "old-task", 1, None, None, "TaskUpdate", "unknown",
                    json.dumps({"goal": "旧任务", "scope": {"mode": "analysis"}, "status": "delivered"}), 1))
        db.execute("INSERT INTO contexts(project,conversation,injected) VALUES('p','old-chat','[\"knowledge@1\"]')")
        db.execute("INSERT INTO outbox(id,project,kind,payload) VALUES('legacy-sync','p','published_sync',?)",
                   (json.dumps({"body": {"content": "password=SYNTHETIC_OLD_KEY"}, "target": {}}),))
        db.commit()
        db.close()
        migrated = Runtime(legacy)
        try:
            self.assertEqual(migrated.task_snapshot("p", "old-task", 1)["goal"], "旧任务")
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 20)
            self.assertEqual(migrated.db.execute("SELECT shown FROM contexts").fetchone()[0], '["knowledge@1"]')
            self.assertNotIn("SYNTHETIC_OLD_KEY", migrated.db.execute("SELECT payload FROM outbox").fetchone()[0])
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
