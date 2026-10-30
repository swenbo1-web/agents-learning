"""Delivery metadata preserves the contract, evidence window and exact history."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.dsh import DshAdapter
from agent_learning.phase_review import configure
from agent_learning.runtime import ContractError, Runtime
from tests import test_phase_source_windows as phase_fixtures


class TaskResultMetadataTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("ALP_TEST_ROOT", Path(__file__).resolve().parents[1]
                                  / "build" / "local" / "task-result-metadata"))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="metadata-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("project", "home", "tmp", "exports", "runtime-releases", "codex"):
            (self.root / name).mkdir()
        self.workspace = self.root / "project"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(self.root / "config.json"),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1",
            "ALP_DSH_REVIEW_PROFILE": "",
            **{key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}}).start()
        patch.object(tempfile, "tempdir", str(self.root / "tmp")).start()
        self.r = Runtime(self.root / "home", config={"scheduler": {"enabled": False},
            "admin": {"enabled": False}, "event_max_chars": 200})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.scope = {"mode": "execute", "allowed_actions": ["read"],
                      "stop_conditions": ["A new user requirement"]}

    def begin(self, scope=None):
        return self.r.task_update(self.p, "chat", "Inspect the current contract",
                                  self.scope if scope is None else scope)

    def update(self, previous, scope, **changes):
        return self.r.task_update(self.p, "chat", previous["goal"], scope,
            expected_task_id=previous["id"], expected_write_version=previous["write_version"], **changes)

    def snapshot(self, task):
        return self.r.task_snapshot(self.p, task["id"], task["revision"], task["write_version"])

    def approve(self, task):
        return self.r.approve_execution(self.p, "chat", task["id"], task["write_version"],
            {"mode": "execute", "allowed_tools": ["Read"],
             "tool_rules": {"Read": {"action": "read"}}})

    def test_multiple_results_and_removal_preserve_exact_write_history(self):
        first = self.begin()
        second = self.update(first, dict(self.scope, result={"summary": "First result"}))
        third = self.update(second, dict(self.scope, result={"summary": "Corrected result"}))
        null = self.update(third, dict(self.scope, result=None))
        removed = self.update(null, self.scope)
        for index, task in enumerate((first, second, third, null, removed), 1):
            self.assertEqual(task["revision"], 1)
            self.assertEqual(task["write_version"], index)
            self.assertEqual(task["execution_epoch"], first["execution_epoch"])
            self.assertEqual(self.snapshot(task)["scope"], task["scope"])
        immutable = self.r.task_snapshot(self.p, first["id"], first["revision"])
        self.assertEqual(immutable["scope"], first["scope"])
        self.assertIsNone(self.r.task_snapshot(self.p, first["id"], 1, removed["write_version"] + 1))

    def test_result_only_replay_is_noop_and_stale_write_cannot_overwrite(self):
        first = self.begin()
        scope = dict(self.scope, result="Captured result")
        updated = self.update(first, scope)
        before = self.r.db.total_changes
        self.assertEqual(self.update(updated, scope), updated)
        self.assertEqual(self.r.db.total_changes, before)
        with self.assertRaisesRegex(ContractError, "stale task write version"):
            self.update(first, scope)
        self.assertEqual(self.r.task(self.p, "chat"), updated)

    def test_full_cleaned_result_survives_truncated_task_update_and_later_fence(self):
        first = self.begin()
        full = {"summary": "Observed detail " * 2000, "api_key": "secret-value"}
        result = self.update(first, dict(self.scope, result=full))
        payload = self.r.db.execute("SELECT payload FROM events WHERE type='TaskUpdate' "
            "AND task_id=? AND task_write_version=?", (result["id"], result["write_version"])).fetchone()[0]
        self.assertTrue(json.loads(payload)["truncated"])
        self.assertEqual(self.snapshot(result)["scope"]["result"],
                         dict(full, api_key="[REDACTED]"))
        with self.r.transaction():
            fenced = self.r._execution_fence(self.p, "chat", "isolated state fence")
        self.assertEqual(fenced["revision"], first["revision"])
        self.assertEqual(self.snapshot(fenced)["scope"], result["scope"])
        replacement = self.update(fenced, dict(self.scope, result="Later result"))
        self.assertEqual(self.snapshot(fenced)["scope"], result["scope"])
        self.assertEqual(self.snapshot(replacement)["scope"]["result"], "Later result")

    def test_old_database_without_extension_table_is_read_only_and_upgrade_safe(self):
        first = self.begin(dict(self.scope, result="Original legacy metadata"))
        self.r.db.execute("DROP TABLE task_result_states")
        before = self.r.db.total_changes
        self.assertEqual(self.snapshot(first)["scope"], first["scope"])
        self.assertEqual(self.r.db.total_changes, before)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master "
                                           "WHERE name='task_result_states'").fetchone())
        updated = self.update(first, dict(self.scope, result="New metadata"))
        self.assertEqual(updated["revision"], first["revision"])
        self.assertEqual(self.snapshot(first)["scope"], first["scope"])
        self.assertEqual(self.snapshot(updated)["scope"], updated["scope"])

    def test_missing_metadata_rows_never_borrow_future_results(self):
        first = self.begin()
        second = self.update(first, dict(self.scope, result="Second state"))
        third = self.update(second, dict(self.scope, result="Future state"))
        self.r.db.execute("DELETE FROM task_result_states WHERE write_version<?", (third["write_version"],))
        self.assertNotIn("result", self.snapshot(first)["scope"])
        self.assertNotIn("result", self.snapshot(second)["scope"])
        self.assertEqual(self.snapshot(third)["scope"]["result"], "Future state")

    def test_metadata_cannot_grant_actions_or_override_trusted_approval(self):
        first = self.begin()
        self.approve(first)
        injected = {"allowed_actions": ["write", "execute"], "allowed_tools": ["Bash"],
                    "allowed_paths": ["/"], "mode": "execute", "budget": {"remaining": 1000}}
        updated = self.update(first, dict(self.scope, result=injected))
        self.assertEqual(updated["execution_epoch"], first["execution_epoch"])
        self.assertTrue(self.r.execution_status(self.p, "chat")["ready"])
        with self.r.transaction():
            read = self.r.check_execution(self.p, "chat", updated, {"tool_name": "Read", "tool_input": {}})
            shell = self.r.check_execution(self.p, "chat", updated, {"tool_name": "Bash", "tool_input": {}})
        self.assertEqual(read["decision"], "allowed")
        self.assertEqual(shell["reason"], "tool_not_approved")
        self.assertEqual(updated["scope"]["allowed_actions"], ["read"])

    def test_result_delivery_and_explicit_resume_still_revoke_old_permission(self):
        first = self.begin()
        self.approve(first)
        delivered = self.update(first, dict(self.scope, result="Delivered"), status="delivered")
        self.assertEqual(delivered["revision"], first["revision"])
        self.assertEqual(delivered["execution_epoch"], first["execution_epoch"] + 1)
        self.assertEqual(self.r.execution_status(self.p, "chat")["reason"], "task_inactive")
        corrected = self.update(delivered, dict(self.scope, result="Corrected delivery"))
        self.assertEqual(corrected["status"], "delivered")
        self.assertEqual(corrected["execution_epoch"], delivered["execution_epoch"])
        with self.assertRaisesRegex(ContractError, "resume_required"):
            self.update(corrected, corrected["scope"], status="active")
        resumed = self.update(corrected, corrected["scope"], resume=True)
        self.assertEqual(resumed["revision"], first["revision"])
        self.assertEqual(resumed["execution_epoch"], delivered["execution_epoch"] + 1)
        self.assertEqual(self.r.execution_status(self.p, "chat")["reason"], "execution_approval_stale")

    def test_every_non_result_scope_field_and_goal_still_changes_contract(self):
        variants = ({"allowed_actions": ["write"]}, {"allowed_paths": [str(self.workspace)]},
            {"allowed_tools": ["Read"]}, {"stop_conditions": ["Stop now"]}, {"mode": "analysis"},
            {"verification": ["offline check"]}, {"budget": {"remaining": 1}},
            {"environment": {"result": "A nested result remains contractual"}})
        for index, fields in enumerate(variants):
            with self.subTest(fields=fields):
                conversation = "contract-" + str(index)
                first = self.r.task_update(self.p, conversation, "Original", self.scope)
                changed = self.r.task_update(self.p, conversation, "Original",
                    dict(self.scope, result="Metadata", **fields), expected_task_id=first["id"],
                    expected_write_version=first["write_version"])
                self.assertEqual(changed["revision"], first["revision"] + 1)
                self.assertEqual(changed["execution_epoch"], first["execution_epoch"] + 1)
        first = self.begin()
        changed = self.r.task_update(self.p, "chat", "New goal", dict(self.scope, result="Metadata"),
            expected_task_id=first["id"], expected_write_version=first["write_version"])
        self.assertEqual(changed["revision"], 2)

    def test_new_task_and_assignments_keep_their_own_metadata_identity(self):
        first = self.begin()
        child = self.r.register_context(self.p, "chat", "initial-child")["context_handle"]
        updated = self.update(first, dict(self.scope, result="Parent progress"))
        frozen = self.r.session_context(self.p, child)
        self.assertTrue(frozen["assignment"]["current"])
        self.assertEqual(frozen["task"]["scope"], first["scope"])
        later_child = self.r.register_context(self.p, "chat", "later-child")["context_handle"]
        replacement = self.update(updated, dict(self.scope, result="Different task"), new_task=True)
        self.assertNotEqual(replacement["id"], first["id"])
        self.assertEqual(replacement["revision"], 1)
        self.assertEqual(replacement["write_version"], updated["write_version"] + 1)
        self.assertFalse(self.r.session_context(self.p, later_child)["assignment"]["current"])
        self.assertEqual(self.r.session_context(self.p, later_child)["task"]["scope"], updated["scope"])
        self.assertEqual(self.snapshot(updated)["scope"], updated["scope"])
        self.assertEqual(self.snapshot(replacement)["scope"], replacement["scope"])


class PhaseResultMetadataTests(unittest.TestCase):
    # Reuse actual host/MCP fixtures; no model, installation or real database.
    setUp = TaskResultMetadataTests.setUp
    runtime = phase_fixtures.PhaseSourceWindowTests.runtime
    callbacks = phase_fixtures.PhaseSourceWindowTests.callbacks
    task_update = phase_fixtures.PhaseSourceWindowTests.task_update
    phase_sources = phase_fixtures.PhaseSourceWindowTests.phase_sources

    def test_all_hosts_delivery_result_retains_earlier_requests_and_progress(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
                send, conversation = self.callbacks(runtime)
                self.task_update(runtime, project, conversation, goal="Retain the current stage evidence",
                                 scope=self.scope, new_task=True)
                first = runtime.task(project, conversation)
                send("prompt", 1, "EARLY_REQUIREMENT_" + host)
                send("stop", 1, "EARLY_PROGRESS_" + host)
                send("prompt", 2, "FINAL_REQUIREMENT_" + host)
                self.task_update(runtime, project, conversation, scope=dict(self.scope, result="Stage complete"),
                                 status="delivered")
                pending = dict(runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone())
                for amended_scope in (dict(self.scope, result="Corrected result"), self.scope,
                                      dict(self.scope, result="Final result")):
                    self.task_update(runtime, project, conversation, scope=amended_scope)
                delivered = runtime.task(project, conversation)
                self.assertEqual(delivered["revision"], first["revision"])
                self.assertGreater(delivered["execution_epoch"], first["execution_epoch"])
                amended = dict(runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone())
                self.assertEqual(amended, dict(pending, delivery_version=delivered["write_version"]))
                send("stop", 2, "FINAL_DELIVERY_" + host)
                phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()
                self.assertEqual(phase["start_time"], first["updated"])
                sources = json.dumps([json.loads(row["payload"]) for row in
                    self.phase_sources(runtime, project, phase["id"])])
                for marker in ("EARLY_REQUIREMENT_", "EARLY_PROGRESS_", "FINAL_REQUIREMENT_", "FINAL_DELIVERY_"):
                    self.assertIn(marker + host, sources)
                frozen = runtime.db.execute("SELECT packet FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0]
                self.assertIn("EARLY_REQUIREMENT_" + host, frozen)
                self.assertIn("EARLY_PROGRESS_" + host, frozen)
                send("stop", 2, "FINAL_DELIVERY_" + host)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_phases WHERE project=?", (project,)).fetchone()[0], 1)
                self.assertEqual(runtime.db.execute("SELECT packet FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0], frozen)
                self.task_update(runtime, project, conversation, scope=dict(self.scope, result="Post-freeze correction"))
                send("stop", 3, "A later explanation of the same delivered stage")
                self.assertEqual(dict(runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()), dict(phase))
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0], 1)
                self.assertEqual(runtime.db.execute("SELECT packet FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0], frozen)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_amended_delivery_keeps_resumed_suffix_without_legacy_order_markers(self):
        with patch("agent_learning.runtime.time.time", return_value=1800000000.0):
            runtime, project = self.runtime("codex", "-suffix")
            configure(runtime, project, {"mode": "phase", "every_phases": 2}, 0)
            send, conversation = self.callbacks(runtime)
            self.task_update(runtime, project, conversation, goal="Retain independent completed suffixes", new_task=True)
            send("prompt", 1, "OLD_SUFFIX_REQUIREMENT")
            self.task_update(runtime, project, conversation, status="delivered")
            send("stop", 1, "OLD_SUFFIX_DELIVERY")
            self.task_update(runtime, project, conversation, resume=True)
            send("prompt", 2, "CURRENT_SUFFIX_REQUIREMENT")
            self.task_update(runtime, project, conversation, scope={"result": "First result"}, status="delivered")
            self.task_update(runtime, project, conversation, scope={"result": "Corrected result"})
            # Older persisted windows have no ordered markers. Their state
            # history must still exclude the earlier suffix at equal times.
            runtime.db.execute("DELETE FROM events WHERE type='PhaseWindowStarted'")
            send("stop", 2, "CURRENT_SUFFIX_DELIVERY")
            phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=? "
                "ORDER BY delivery_version DESC LIMIT 1", (project,)).fetchone()
            text = json.dumps([json.loads(row["payload"]) for row in self.phase_sources(runtime, project, phase["id"])])
            self.assertIn("CURRENT_SUFFIX_REQUIREMENT", text)
            self.assertIn("CURRENT_SUFFIX_DELIVERY", text)
            self.assertNotIn("OLD_SUFFIX_REQUIREMENT", text)
            self.assertNotIn("OLD_SUFFIX_DELIVERY", text)

    def test_observed_stop_pins_delivery_during_local_seal_recovery(self):
        from agent_learning.phase_seal_recovery import recover
        for guard in ("request", "event"):
            with self.subTest(guard=guard):
                runtime, project = self.runtime("dsh", "-recovery-" + guard)
                configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
                send, conversation = self.callbacks(runtime, native="recovery-" + guard)
                self.task_update(runtime, project, conversation, goal="Keep the original observed delivery", new_task=True)
                send("prompt", 1, "ORIGINAL_SEAL_REQUIREMENT_" + guard)
                self.task_update(runtime, project, conversation, scope={"result": "Original result"}, status="delivered")
                delivered = runtime.task(project, conversation)
                with patch("agent_learning.phase_review.seal_phase", return_value=False):
                    send("stop", 2, "ORIGINAL_SEAL_DELIVERY_" + guard)
                before = dict(runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone())
                self.assertEqual(before["state"], "awaiting_stop")
                request = runtime.db.execute("SELECT * FROM phase_seal_requests WHERE project=?", (project,)).fetchone()
                self.assertIsNotNone(request)
                if guard == "event":
                    runtime.db.execute("DELETE FROM phase_seal_requests WHERE project=?", (project,))
                self.task_update(runtime, project, conversation, scope={"result": "Later metadata"})
                self.assertEqual(dict(runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()), before)
                with runtime.transaction():
                    recover(runtime, project, force=True)
                phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()
                self.assertEqual(phase["state"], "sealed")
                self.assertEqual(phase["event_id"], request["event_id"])
                self.assertEqual(phase["delivery_version"], delivered["write_version"])
                snapshot = runtime.task_snapshot(project, delivered["id"], delivered["revision"], delivered["write_version"])
                self.assertEqual(snapshot["scope"]["result"], "Original result")
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_dsh_delivery_result_keeps_tool_observation_in_frozen_review_packet(self):
        runtime, project = self.runtime("dsh", "-tool")
        configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
        send, conversation = self.callbacks(runtime)
        self.task_update(runtime, project, conversation, goal="Diagnose TERM_RESULT_DIAG", scope=self.scope, new_task=True)
        first = runtime.task(project, conversation)
        send("prompt", 1, "Inspect TERM_RESULT_DIAG and retain the result")
        adapter = DshAdapter(runtime, self.workspace)
        values = {"sessionId": "native-window-chat", "turn": 1, "callId": "result-diagnostic",
                  "name": "pwsh", "arguments": {"command": "inspect TERM_RESULT_DIAG"}}
        adapter.handle("PreToolUse", values)
        adapter.handle("PostToolUse", {**values, "result": {"exit_code": 0, "output": "TERM_RESULT_DIAG observed"}})
        observed = runtime.db.execute("SELECT * FROM events WHERE conversation=? AND type='PostToolUse'",
                                      (conversation,)).fetchone()
        self.assertIsNotNone(observed)
        self.assertEqual(observed["outcome"], "succeeded")
        original = dict(observed)
        self.task_update(runtime, project, conversation, scope=dict(self.scope, result="TERM_RESULT_DIAG observed"),
                         status="delivered")
        self.task_update(runtime, project, conversation, scope=dict(self.scope, result="TERM_RESULT_DIAG confirmed"))
        send("stop", 2, "TERM_RESULT_DIAG describes the successful diagnostic observation")
        phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()
        self.assertEqual(phase["task_revision"], first["revision"])
        packet = json.loads(runtime.db.execute("SELECT packet FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0])
        events = [event for item in packet["items"] for event in item["events"]]
        tool = next(event for event in events if event["id"] == observed["id"])
        self.assertEqual(tool["type"], "PostToolUse")
        self.assertTrue(tool["current_tool_success"])
        self.assertEqual(dict(runtime.db.execute("SELECT * FROM events WHERE id=?", (observed["id"],)).fetchone()), original)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
