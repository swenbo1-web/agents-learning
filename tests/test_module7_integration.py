"""发布、上下文、批次及分发之间的真实接口回归。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent_learning.cli import main
from agent_learning.hindsight import HindsightError
from agent_learning.knowledge_output import search_preview
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode


class Module7IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module7-integrated-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "project"
        self.work.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.work)

    def source(self, chat, content="Update compiler include path", level="tool_success", target=None):
        self.r.task_update(self.p, chat, "Check compiler guidance")
        operation = uuid.uuid4().hex
        for kind in ("PreToolUse", "PostToolUse"):
            event = {"hook_event_name": kind, "session_id": chat, "cwd": str(self.work),
                     "tool_use_id": operation, "tool_name": "Bash"}
            if kind == "PostToolUse":
                event["tool_response"] = {"exit_code": 0}
            self.r.handle_hook(event)
        ref = self.r.db.execute("SELECT id FROM events WHERE project=? AND conversation=? AND type='PostToolUse' ORDER BY rowid DESC LIMIT 1",
                                (self.p, chat)).fetchone()[0]
        problem = self.r.feedback(self.p, chat, "Source " + operation, category="operation", event_refs=[ref])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler solution", content=content, category="operation", applicability="compiler projects",
            limitations="Verify the current project", validation_level=level, evidence_refs=[ref])
        kwargs = {"knowledge_id": target["knowledge_id"], "expected_revision": target["revision"]} if target else {}
        return self.r.publish(self.p, candidate["candidate_id"], **kwargs)

    def stable(self):
        knowledge = self.source("first")
        self.source("second")
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], knowledge["revision"], request_id="promote")
        return knowledge

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = main(["--home", str(self.home), "--workspace", str(self.work), *args])
        return result, json.loads(output.getvalue()) if output.getvalue() else None

    def test_cli_revoke_requires_revision_and_replays_request(self):
        knowledge = self.source("first")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--home", str(self.home), "--workspace", str(self.work), "revoke", knowledge["knowledge_id"]])
        args = ("revoke", knowledge["knowledge_id"], "--expected-revision", "1", "--request-id", "cli-revoke")
        code, first = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertTrue(first["revoked"])
        self.assertEqual(self.cli(*args), (0, first))
        self.assertEqual(self.cli("knowledge-action", "cli-revoke")[1]["action"], "revoke")

    def test_maturity_visible_in_read_and_bounded_search_preview(self):
        knowledge = self.stable()
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["maturity"], "stable")
        preview = search_preview(self.r.search(self.p, "compiler"), max_chars=2000)
        self.assertEqual(preview["items"][0]["maturity"], "stable")
        self.assertLessEqual(len(encode(preview)), 2000)

    def test_promotion_notifies_previously_exposed_context(self):
        knowledge = self.source("first")
        self.source("second")
        self.r.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "first", "cwd": str(self.work), "prompt": "compiler"})
        self.r.promote_knowledge(self.p, knowledge["knowledge_id"], 1)
        result = self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": "first", "cwd": str(self.work), "tool_name": "Bash"})
        self.assertIn("成熟度=stable", encode(result))

    def test_counterexample_demotes_public_maturity_without_model(self):
        knowledge = self.stable()
        self.r.feedback(self.p, "first", "Observed counterexample", category="operation",
                        knowledge_id=knowledge["knowledge_id"], knowledge_revision=1, feedback_kind="counterexample")
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["maturity"], "candidate")

    def test_daily_cli_is_unchanged_and_manifest_does_not_copy_body(self):
        knowledge = self.source("first", content="large compiler text " * 1000, level="reported")
        code, first = self.cli("knowledge-daily-release")
        self.assertEqual(code, 0)
        self.assertEqual(first["state"], "committed")
        self.assertFalse(self.cli("knowledge-daily-release")[1]["changed"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0], 1)
        manifest = self.r.db.execute("SELECT manifest FROM knowledge_releases").fetchone()[0]
        self.assertLess(len(manifest), 5000)
        self.assertNotIn("large compiler text", manifest)
        self.assertEqual(self.r.status(self.p)["knowledge_release_head"], first["release_id"])
        self.assertEqual(self.cli("knowledge-release", first["release_id"])[0], 0)

    def test_worker_and_admin_mcp_do_not_expose_release_mutation(self):
        for role in ("worker", "admin"):
            server = Server(self.r, self.p, role)
            server.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            server.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {tool["name"] for tool in response["result"]["tools"]}
            self.assertFalse(names & {"publish", "revoke", "restore", "knowledge_promote", "knowledge_release_commit"})

    def test_old_tombstone_does_not_cancel_new_revision_distribution(self):
        self.r.config["hindsight"]["enabled"] = True
        self.r.config["hindsight"]["bank_id"] = "published"
        first = self.source("first")
        updated = self.source("second", content="Corrected compiler include path", target=first)
        self.r.enqueue_sync(self.p)
        rows = self.r.db.execute("SELECT payload FROM outbox WHERE state='pending'").fetchall()
        pending = {(p["revision"], p["envelope"]["active"]) for p in map(lambda row: json.loads(row[0]), rows)}
        self.assertIn((1, False), pending)
        self.assertIn((2, True), pending)
        with patch("agent_learning.hindsight.HindsightClient") as client:
            client.return_value.retain.return_value = {"success": True, "async": False, "bank_id": "published", "items_count": 1}
            self.r.sync_batch(self.p)
            sent = [json.loads(call.args[1]) for call in client.return_value.retain.call_args_list]
        self.assertEqual({(p["revision"], p["active"]) for p in sent}, {(1, False), (2, True)})
        self.assertEqual(self.r.read(self.p, updated["knowledge_id"])["revision"], 2)

    def test_unknown_write_is_not_erased_by_later_not_sent_failure(self):
        self.r.config["hindsight"].update(enabled=True, bank_id="published")
        knowledge = self.source("first")
        with patch("agent_learning.hindsight.HindsightClient") as client:
            client.return_value.retain.side_effect = HindsightError("unknown", write_outcome="unknown")
            initial = self.r.sync_once(self.p)
            client.return_value.retain.side_effect = HindsightError("missing", kind="missing_credentials", write_outcome="not_sent")
            retry = self.r.sync_once(self.p, initial["operation_id"])
        self.assertEqual(retry["result"]["attempt_write_outcome"], "not_sent")
        self.assertEqual(retry["result"]["write_outcome"], "unknown")
        self.source("second")
        self.r.enqueue_sync(self.p)
        old = self.r.sync_read(self.p, initial["operation_id"])
        self.assertEqual(old["state"], "blocked")
        self.assertEqual(old["write_outcome"], "unknown")
        self.assertEqual(old["attempts"], 1)

    def test_evidence_bound_to_other_knowledge_cannot_support_restore(self):
        first = self.source("first", level="reported")
        self.r.feedback(self.p, "first", "Counterexample", category="operation", knowledge_id=first["knowledge_id"],
                        knowledge_revision=1, feedback_kind="counterexample")
        self.r.revoke(self.p, first["knowledge_id"], expected_revision=1)
        other = self.source("other", content="Other knowledge", level="reported")
        problem = self.r.feedback(self.p, "other", "New observation for other knowledge", category="operation",
            knowledge_id=other["knowledge_id"], knowledge_revision=1, feedback_kind="observation")
        refs = [e["id"] for e in self.r.review_packet(self.p, problem["id"])["events"]]
        with self.assertRaises(ContractError):
            self.r.restore(self.p, first["knowledge_id"], 1, reason="Recheck original knowledge", evidence_refs=refs)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM active WHERE id=?", (first["knowledge_id"],)).fetchone())

    def test_account_change_preserves_project_knowledge_and_release(self):
        knowledge = self.stable()
        release = self.r.daily_knowledge_release(self.p)
        self.r.account_changed(notification_id="changed-login")
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["maturity"], "stable")
        self.assertEqual(self.r.status(self.p)["knowledge_release_head"], release["release_id"])


if __name__ == "__main__":
    unittest.main()
