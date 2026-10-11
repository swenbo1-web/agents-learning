"""隔离SQLite与内存传输验证分发安全；不访问真实Hindsight或模型。"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.hindsight import HindsightClient, HindsightError
from agent_learning.knowledge_sync import KnowledgeSyncMixin, install_sync
from agent_learning.runtime import ContractError, Runtime, SCHEMA, encode


class Module7SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.r = Runtime(Path(self.temp.name) / "home")
        self.addCleanup(self.r.close)
        self.p = self.r.project(Path(self.temp.name) / "project")
        self.r.task_update(self.p, "chat", "分析知识分发", {"mode": "analysis"})
        self.r.config["hindsight"].update(enabled=True, bank_id="published")

    def publish(self, summary="案例", content="已审核的解决步骤", knowledge_id=None, expected_revision=None):
        problem = self.r.feedback(self.p, "chat", summary, category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="知识分发", content=content, category="operation", applicability="测试环境", limitations="仅报告证据",
            evidence_refs=[e["id"] for e in packet["events"]], validation_level="reported")["candidate_id"]
        return self.r.publish(self.p, candidate, knowledge_id=knowledge_id, expected_revision=expected_revision)

    def operation(self, state="pending"):
        rows = self.r.sync_list(self.p, state=state)["items"]
        self.assertEqual(len(rows), 1)
        return rows[0]["operation_id"]

    def success(self):
        return patch("agent_learning.hindsight.HindsightClient.retain", return_value={
            "success": True, "async": False, "bank_id": "published", "items_count": 1})

    def test_state_envelope_matches_authoritative_snapshot(self):
        item = self.publish()
        row = self.r.db.execute("SELECT payload FROM outbox").fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload["envelope"], self.r.knowledge_state_snapshot(self.p, item["knowledge_id"], item["revision"]))
        with self.success() as mocked:
            result = self.r.sync_once(self.p)
        args = mocked.call_args.args
        self.assertEqual(json.loads(args[1]), payload["envelope"])
        self.assertIn(payload["snapshot_digest"], args[0])
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.r.sync_read(self.p, result["operation_id"])["attempts"], 1)

    def test_not_sent_failure_can_recover_after_multiple_preflights(self):
        self.publish()
        operation = self.operation()
        error = HindsightError("不输出敏感配置", kind="missing_credentials", write_outcome="not_sent")
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=error) as mocked:
            self.r.sync_once(self.p)
            self.assertEqual(self.r.sync_once(self.p)["status"], "idle")
            for _ in range(4):
                self.assertEqual(self.r.sync_once(self.p, retry=operation)["status"], "blocked")
        self.assertEqual(mocked.call_count, 5)
        row = self.r.sync_read(self.p, operation)
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["prepare_attempts"], 5)
        with self.success():
            self.assertEqual(self.r.sync_once(self.p, retry=operation)["status"], "synced")
        self.assertEqual(self.r.sync_read(self.p, operation)["prepare_attempts"], 6)

    def test_unknown_failure_never_automatically_retries_and_caps_explicit_attempts(self):
        self.publish()
        operation = self.operation()
        error = HindsightError("安全错误", kind="transport_error", write_outcome="unknown")
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=error) as mocked:
            self.r.sync_once(self.p)
            self.assertEqual(self.r.sync_once(self.p)["status"], "idle")
            for _ in range(2):
                self.r.sync_once(self.p, retry=operation)
            with self.assertRaises(ContractError):
                self.r.sync_once(self.p, retry=operation)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(self.r.sync_read(self.p, operation)["attempts"], 3)

    def test_expired_lease_is_unknown_and_clears_lease(self):
        self.publish()
        operation = self.operation()
        self.r.db.execute("UPDATE outbox SET state='running',lease='crashed',lease_until=1,prepare_attempts=1,lease_counted=0")
        with self.success() as mocked:
            self.assertEqual(self.r.sync_once(self.p)["status"], "idle")
        mocked.assert_not_called()
        row = self.r.sync_read(self.p, operation)
        self.assertEqual((row["state"], row["attempts"], row["write_outcome"]), ("blocked", 1, "unknown"))
        self.assertEqual(tuple(self.r.db.execute("SELECT lease,lease_until FROM outbox").fetchone()), (None, 0))

    def test_late_success_cannot_overwrite_blocked_state_even_same_lease(self):
        self.publish()
        operation = self.operation()
        def late(*args):
            self.r.db.execute("UPDATE outbox SET state='blocked',error='other worker'")
            return {"success": True, "async": False, "items_count": 1, "bank_id": "published"}
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=late):
            result = self.r.sync_once(self.p)
        self.assertEqual(result["status"], "lease_lost")
        read = self.r.sync_read(self.p, operation)
        self.assertEqual(read["state"], "blocked")
        self.assertEqual(read["receipts"][0]["state"], "late_synced")

    def test_other_worker_expiration_records_late_confirmation_without_overwrite(self):
        self.publish()
        operation = self.operation()
        second = Runtime(self.r.home)
        self.addCleanup(second.close)
        second.config["hindsight"].update(enabled=True, bank_id="published")
        def late(*args):
            second.db.execute("UPDATE outbox SET lease_until=1")
            self.assertEqual(second.sync_once(self.p)["status"], "idle")
            return {"success": True}
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=late):
            self.assertEqual(self.r.sync_once(self.p)["status"], "lease_lost")
        read = self.r.sync_read(self.p, operation)
        self.assertEqual(read["attempts"], 1)
        self.assertEqual(read["write_outcome"], "unknown")
        self.assertEqual(read["receipts"][0]["state"], "late_synced")

    def test_new_source_same_revision_supersedes_unsent_state(self):
        first = self.publish(summary="第一例")
        old = self.operation()
        second = self.publish(summary="第二独立例")
        self.assertEqual(second["knowledge_id"], first["knowledge_id"])
        operation = self.operation()
        self.assertNotEqual(operation, old)
        self.assertEqual(self.r.sync_read(self.p, old)["state"], "superseded")
        payload = json.loads(self.r.db.execute("SELECT payload FROM outbox WHERE id=?", (operation,)).fetchone()[0])
        self.assertEqual(payload["envelope"]["source_count"], 2)
        self.assertEqual(payload["body"], payload["envelope"]["body"])

    def test_unknown_old_state_keeps_separate_identity_after_source_change(self):
        self.publish(summary="第一例")
        operation = self.operation()
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=HindsightError("unknown", write_outcome="unknown")):
            self.r.sync_once(self.p)
        self.publish(summary="第二例")
        current = self.operation()
        self.assertNotEqual(current, operation)
        self.assertEqual(self.r.sync_read(self.p, operation)["state"], "blocked")
        with self.success() as mocked:
            self.assertEqual(self.r.sync_once(self.p, retry=operation)["status"], "blocked")
        mocked.assert_not_called()
        with self.success():
            self.assertEqual(self.r.sync_once(self.p)["operation_id"], current)

    def test_dirty_source_version_creates_new_current_envelope(self):
        item = self.publish()
        old = self.operation()
        self.r.db.execute("UPDATE knowledge_source_versions SET version=version+1 WHERE project=? AND knowledge_id=?",
                          (self.p, item["knowledge_id"]))
        with self.success():
            self.assertEqual(self.r.sync_once(self.p)["status"], "synced")
        self.assertEqual(self.r.sync_read(self.p, old)["state"], "superseded")
        row = self.r.db.execute("SELECT payload FROM outbox WHERE state='synced'").fetchone()
        self.assertEqual(json.loads(row[0])["envelope"], self.r.knowledge_state_snapshot(self.p, item["knowledge_id"], 1))

    def test_inactive_state_is_distributed_as_tombstone_not_deleted_remotely(self):
        item = self.publish()
        old = self.operation()
        # 数据库active变更与真实撤销共用dirty触发器；避免要求此测试授权生命周期动作。
        self.r.db.execute("DELETE FROM active WHERE project=? AND id=?", (self.p, item["knowledge_id"]))
        with self.success() as mocked:
            result = self.r.sync_once(self.p)
        self.assertEqual(result["status"], "synced")
        self.assertFalse(json.loads(mocked.call_args.args[1])["active"])
        self.assertEqual(self.r.sync_read(self.p, old)["state"], "superseded")

    def test_disabled_preserves_dirty_and_backfill_after_enable_has_current_state(self):
        self.r.config["hindsight"]["enabled"] = False
        item = self.publish()
        self.r.db.execute("DELETE FROM active WHERE project=? AND id=?", (self.p, item["knowledge_id"]))
        self.assertEqual(self.r.sync_once(self.p)["status"], "disabled")
        self.assertEqual(self.r.sync_list(self.p)["items"], [])
        self.r.config["hindsight"]["enabled"] = True
        self.assertEqual(self.r.enqueue_sync(self.p)["enqueued"], 1)
        self.assertFalse(self.r.sync_list(self.p)["items"][0]["active"])

    def test_target_change_does_not_silently_migrate_old_operation(self):
        self.publish()
        old = self.operation()
        self.r.config["hindsight"]["bank_id"] = "different"
        with self.success() as mocked:
            self.assertEqual(self.r.sync_once(self.p)["status"], "blocked")
        mocked.assert_not_called()
        self.assertEqual(self.r.sync_read(self.p, old)["error"]["kind"], "target_changed")
        self.assertEqual(self.r.enqueue_sync(self.p)["enqueued"], 1)
        self.assertEqual(self.r.sync_read(self.p, old)["state"], "blocked")
        self.assertNotEqual(self.operation(), old)

    def test_timeout_changes_reuse_queue_identity(self):
        self.publish()
        operation = self.operation()
        self.r.config["hindsight"]["timeout_seconds"] = 15
        self.assertEqual(self.r.enqueue_sync(self.p)["enqueued"], 0)
        self.assertEqual(self.operation(), operation)

    def test_body_mismatch_cannot_be_queued(self):
        item = self.publish()
        with self.assertRaises(ContractError), self.r.transaction():
            self.r._queue_sync(self.p, item["knowledge_id"], 1, {"content": "wrong"})

    def test_safe_summaries_and_receipts_never_return_arbitrary_remote_fields(self):
        self.publish()
        with patch("agent_learning.hindsight.HindsightClient.retain", return_value={
            "success": True, "async": False, "bank_id": "published", "items_count": 1,
            "authorization": "private-token", "detail": "x" * 900000}):
            result = self.r.sync_once(self.p)
        output = encode([result, self.r.sync_list(self.p), self.r.sync_read(self.p, result["operation_id"])])
        self.assertLess(len(output), 4000)
        self.assertNotIn("private-token", output)
        self.assertNotIn("900000", output)

    def test_adapter_confirmation_whitelists_only_verified_fields(self):
        client = HindsightClient({"url": "http://127.0.0.1:1", "bank_id": "published"})
        client._canonical_bank_id = "published"
        result = client._confirm_retain({"success": True, "async": False, "bank_id": "published", "items_count": 1,
                                         "detail": "x" * 900000, "token": "synthetic-secret"})
        self.assertEqual(result, {"success": True, "async": False, "bank_id": "published", "items_count": 1})

    def test_project_isolation_for_list_read_and_retry(self):
        self.publish()
        operation = self.operation()
        other = self.r.project(Path(self.temp.name) / "other")
        self.assertEqual(self.r.sync_list(other)["items"], [])
        with self.assertRaises(ContractError):
            self.r.sync_read(other, operation)
        with self.assertRaises(ContractError):
            self.r.sync_once(other, retry=operation)

    def test_list_pagination_and_bounds(self):
        for i in range(3):
            self.publish(summary=f"案例{i}", content=f"不同步骤{i}")
        first = self.r.sync_list(self.p, limit=2)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)
        second = self.r.sync_list(self.p, limit=2, offset=2)
        self.assertFalse(second["has_more"])
        self.assertEqual(len(second["items"]), 1)
        for args in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"state": "unknown"}):
            with self.assertRaises(ContractError):
                self.r.sync_list(self.p, **args)

    def test_batch_does_not_retry_blocked_and_limits_new_operations(self):
        for i in range(3):
            self.publish(summary=f"案例{i}", content=f"不同步骤{i}")
        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=HindsightError("safe", write_outcome="not_sent")) as mocked:
            self.assertEqual(self.r.sync_batch(self.p, max_items=2)["processed"], 2)
            self.assertEqual(self.r.sync_batch(self.p, max_items=2)["processed"], 1)
            self.assertEqual(self.r.sync_batch(self.p, max_items=2)["processed"], 0)
        self.assertEqual(mocked.call_count, 3)

    def test_batch_restricts_http_timeout_by_remaining_budget(self):
        self.publish()
        with patch("agent_learning.hindsight.HindsightClient") as mocked:
            mocked.return_value.retain.return_value = {"success": True}
            result = self.r.sync_batch(self.p, max_seconds=3, max_items=1)
        self.assertEqual(result["processed"], 1)
        self.assertLessEqual(mocked.call_args.args[0]["timeout_seconds"], 1)
        self.assertEqual(self.r.config["hindsight"]["timeout_seconds"], 10)

    def test_pending_legacy_payload_keeps_legacy_document_identity(self):
        item = self.publish()
        self.r.db.execute("DELETE FROM outbox")
        body = self.r.knowledge_state_snapshot(self.p, item["knowledge_id"], 1)["body"]
        payload = {"knowledge_id": item["knowledge_id"], "revision": 1, "body": body, "target": self.r.config["hindsight"]}
        self.r.db.execute("INSERT INTO outbox(id,project,kind,payload) VALUES(?,?,?,?)", ("legacy", self.p, "published_sync", encode(payload)))
        self.r.db.execute("DELETE FROM knowledge_sync_dirty")
        with self.success() as mocked:
            self.assertEqual(self.r.sync_once(self.p)["operation_id"], "legacy")
        self.assertEqual(mocked.call_args.args[0], f"{self.p}:{item['knowledge_id']}@1")
        self.assertEqual(json.loads(mocked.call_args.args[1]), body)


class Module7SyncMigrationTests(unittest.TestCase):
    def test_legacy_outcome_preserved_without_rewriting_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "legacy.sqlite")
            try:
                db.row_factory = sqlite3.Row
                db.executescript(SCHEMA)
                payload = encode({"body": {"content": "原始正文"}})
                db.execute("INSERT INTO outbox(id,project,kind,payload,state,attempts,error) VALUES(?,?,?,?,?,?,?)",
                           ("not-sent", "p", "published_sync", payload, "blocked", 3, encode({"write_outcome": "not_sent"})))
                db.execute("INSERT INTO outbox(id,project,kind,payload,state,attempts) VALUES(?,?,?,?,?,?)",
                           ("unknown", "p", "published_sync", payload, "running", 2))
                stub = type("RuntimeStub", (), {"db": db})()
                install_sync(stub)
                rows = {r["id"]: dict(r) for r in db.execute("SELECT * FROM outbox")}
                self.assertEqual((rows["not-sent"]["attempts"], rows["not-sent"]["prepare_attempts"]), (0, 3))
                self.assertEqual(rows["unknown"]["write_outcome"], "unknown")
                self.assertEqual(rows["unknown"]["lease_counted"], 1)
                self.assertTrue(all(r["payload"] == payload for r in rows.values()))
                db.execute("UPDATE outbox SET prepare_attempts=4 WHERE id='not-sent'")
                install_sync(stub)
                self.assertEqual(db.execute("SELECT prepare_attempts FROM outbox WHERE id='not-sent'").fetchone()[0], 4)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
