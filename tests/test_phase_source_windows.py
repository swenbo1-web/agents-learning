"""Task-stage source windows through native host callbacks, without model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter
from agent_learning.dsh import DshAdapter
from agent_learning.mcp import Server
from agent_learning.phase_review import configure
from agent_learning.runtime import Runtime


class PhaseSourceWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-source-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()

    def runtime(self, host, suffix=""):
        runtime = Runtime(self.root / (host + suffix + "-home"), host=host,
            config={"admin": {"enabled": True, "command": [sys.executable],
                "antigravity_command": [sys.executable], "dsh_command": [sys.executable],
                "timeout_seconds": 5, "max_input_chars": 64000}})
        self.addCleanup(runtime.close)
        patch.object(runtime, "_launch_admin").start()
        project = runtime.project(self.workspace)
        return runtime, project

    def callbacks(self, runtime, native="native-window-chat"):
        if runtime.host == "antigravity":
            adapter = AntigravityAdapter(runtime, self.workspace)
            def send(kind, turn, text):
                common = {"conversationId": native, "workspacePaths": [str(self.workspace)],
                          "modelName": "offline-host-model"}
                if kind == "prompt":
                    return adapter.handle("PreInvocation", {**common, "invocationNum": turn,
                        "initialNumSteps": 0, "lastUserInput": text})
                return adapter.handle("Stop", {**common, "executionNum": turn,
                    "fullyIdle": True, "terminationReason": "model_stop", "error": "",
                    "finalModelOutput": text})
        elif runtime.host == "dsh":
            adapter = DshAdapter(runtime, self.workspace)
            # A native session start is required for model selection, never a fabricated task event.
            adapter.handle("SessionStart", {"sessionId": native, "source": "startup",
                "requestContext": {"provider": "offline", "model": "host-model"}})
            def send(kind, turn, text):
                payload = {"sessionId": native, "turn": turn}
                payload["prompt" if kind == "prompt" else "lastAssistantMessage"] = text
                return adapter.handle("UserPromptSubmit" if kind == "prompt" else "Stop", payload)
        else:
            runtime.handle_hook({"cwd": str(self.workspace), "session_id": native,
                "hook_event_name": "SessionStart", "source": "startup", "model": "offline-host-model"})
            def send(kind, turn, text):
                payload = {"cwd": str(self.workspace), "session_id": native, "turn_id": str(turn),
                           "hook_event_name": "UserPromptSubmit" if kind == "prompt" else "Stop"}
                payload["prompt" if kind == "prompt" else "last_assistant_message"] = text
                return runtime.handle_hook(payload)
        return send, runtime.host_conversation(native)

    def task_update(self, runtime, project, conversation, *, goal=None, **changes):
        previous = runtime.task(project, conversation)
        handle = runtime.register_context(project, conversation)["context_handle"]
        arguments = {"context_handle": handle, "goal": goal or previous["goal"], **changes}
        if previous:
            arguments.update(expected_task_id=previous["id"], expected_write_version=previous["write_version"])
        return Server(runtime, project, workspace=self.workspace).call("task_update", arguments)

    def phase_sources(self, runtime, project, phase_id):
        return runtime.db.execute("SELECT DISTINCT e.* FROM problems p JOIN problem_events pe ON pe.problem=p.id "
            "JOIN events e ON e.id=pe.event WHERE p.project=? AND p.summary LIKE ? ORDER BY e.created,e.rowid",
            (project, "%[" + phase_id + "]%")).fetchall()

    def refs_for_marker(self, runtime, project, marker, kind):
        rows = runtime.db.execute("SELECT id,payload FROM events WHERE project=? AND type=?", (project, kind)).fetchall()
        return {row["id"] for row in rows if marker in json.dumps(json.loads(row["payload"]), ensure_ascii=False)}

    def assert_marker_sources(self, runtime, project, phase, marker, kind):
        expected = self.refs_for_marker(runtime, project, marker, kind)
        if not expected and kind == "PublicMessageSegment":
            # Short public replies stay in the Stop; only long messages have
            # separately captured segments that replace their preview.
            expected = self.refs_for_marker(runtime, project, marker, "Stop")
        self.assertTrue(expected, "the native callback must really have captured " + marker)
        actual = {row["id"] for row in self.phase_sources(runtime, project, phase["id"])}
        self.assertTrue(expected <= actual, f"stage lost {kind} marker {marker}: {expected - actual}")

    def test_all_hosts_preserve_multiturn_active_stage_and_final_prompt(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
                send, conversation = self.callbacks(runtime)
                self.task_update(runtime, project, conversation, goal="Retain the complete current task convention", new_task=True)
                send("prompt", 1, "FIRST_REQUIREMENT_" + host)
                reply = "FIRST_REPLY_" + host + ". " + "Current task evidence remains applicable. " * 65 + "FIRST_REPLY_TAIL_" + host
                send("stop", 1, reply)
                self.assertEqual(runtime.task(project, conversation)["status"], "active")
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 0)
                send("prompt", 2, "FINAL_REQUIREMENT_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 2, "FINAL_DELIVERY_" + host)
                phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()
                self.assertIsNotNone(phase)
                for marker, kind in (("FIRST_REQUIREMENT_", "UserPromptSubmit"),
                                     ("FINAL_REQUIREMENT_", "UserPromptSubmit"),
                                     ("FIRST_REPLY_", "PublicMessageSegment"),
                                     ("FIRST_REPLY_TAIL_", "PublicMessageSegment"),
                                     ("FINAL_DELIVERY_", "PublicMessageSegment")):
                    self.assert_marker_sources(runtime, project, phase, marker + host, kind)
                packets = [json.loads(row[0]) for row in runtime.db.execute(
                    "SELECT packet FROM phase_review_batches WHERE project=?", (project,))]
                self.assertEqual(len(packets), 1)
                packet_text = json.dumps(packets[0], ensure_ascii=False)
                for marker in ("FIRST_REQUIREMENT_", "FINAL_REQUIREMENT_", "FIRST_REPLY_TAIL_", "FINAL_DELIVERY_"):
                    self.assertIn(marker + host, packet_text)
                send("stop", 2, "FINAL_DELIVERY_" + host)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_phases WHERE project=?", (project,)).fetchone()[0], 1)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0], 1)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_all_hosts_resume_begins_new_stage_without_reusing_previous_sources(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                configure(runtime, project, {"mode": "phase", "every_phases": 2}, 0)
                send, conversation = self.callbacks(runtime)
                self.task_update(runtime, project, conversation, goal="Preserve independent delivery stages", new_task=True)
                send("prompt", 1, "PREVIOUS_REQUIREMENT_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 1, "PREVIOUS_DELIVERY_" + host)
                first = runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()
                self.task_update(runtime, project, conversation, resume=True, status="active")
                send("prompt", 2, "RESUMED_REQUIREMENT_" + host)
                send("stop", 2, "RESUMED_PROGRESS_" + host)
                send("prompt", 3, "RESUMED_FINAL_REQUIREMENT_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 3, "RESUMED_DELIVERY_" + host)
                phases = runtime.db.execute("SELECT * FROM review_phases WHERE project=? ORDER BY created", (project,)).fetchall()
                self.assertEqual(len(phases), 2)
                self.assertEqual(phases[0]["id"], first["id"])
                for marker, kind in (("RESUMED_REQUIREMENT_", "UserPromptSubmit"),
                                     ("RESUMED_FINAL_REQUIREMENT_", "UserPromptSubmit"),
                                     ("RESUMED_PROGRESS_", "PublicMessageSegment"),
                                     ("RESUMED_DELIVERY_", "PublicMessageSegment")):
                    self.assert_marker_sources(runtime, project, phases[1], marker + host, kind)
                old_refs = {row["id"] for row in self.phase_sources(runtime, project, phases[0]["id"])}
                new_refs = {row["id"] for row in self.phase_sources(runtime, project, phases[1]["id"])}
                self.assertTrue(old_refs)
                self.assertFalse(old_refs & new_refs, "an explicit resume must not re-review the delivered stage")
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_all_hosts_initial_task_includes_its_unbound_initiating_prompt(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
                send, conversation = self.callbacks(runtime)
                send("prompt", 1, "INITIAL_TASK_REQUIREMENT_" + host)
                self.assertIsNone(runtime.task(project, conversation))
                prompt = runtime.db.execute("SELECT id,task_id,task_revision FROM events WHERE project=? "
                    "AND conversation=? AND type='UserPromptSubmit' ORDER BY created DESC LIMIT 1",
                    (project, conversation)).fetchone()
                self.assertIsNone(prompt["task_id"])
                self.task_update(runtime, project, conversation, goal="Implement the initiating user requirement", new_task=True)
                send("stop", 1, "INITIAL_TASK_PROGRESS_" + host)
                send("prompt", 2, "INITIAL_TASK_FINAL_REQUIREMENT_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 2, "INITIAL_TASK_DELIVERY_" + host)
                phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=?", (project,)).fetchone()
                for marker, kind in (("INITIAL_TASK_REQUIREMENT_", "PhaseRequestContext"),
                                     ("INITIAL_TASK_FINAL_REQUIREMENT_", "UserPromptSubmit"),
                                     ("INITIAL_TASK_PROGRESS_", "PublicMessageSegment"),
                                     ("INITIAL_TASK_DELIVERY_", "PublicMessageSegment")):
                    self.assert_marker_sources(runtime, project, phase, marker + host, kind)
                original = runtime.db.execute("SELECT task_id,task_revision FROM events WHERE id=?", (prompt["id"],)).fetchone()
                self.assertEqual((original["task_id"], original["task_revision"]),
                                 (prompt["task_id"], prompt["task_revision"]),
                                 "source-window association must not rewrite the original event contract")
                derived = next(json.loads(row["payload"]) for row in self.phase_sources(runtime, project, phase["id"])
                               if row["type"] == "PhaseRequestContext")
                self.assertEqual(derived["original_event_id"], prompt["id"])
                self.assertEqual(derived["validation_level"], "reported")
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_all_hosts_phase_opt_in_and_contract_changes_do_not_backfill_old_sources(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                send, conversation = self.callbacks(runtime)
                self.task_update(runtime, project, conversation, goal="Original contract", new_task=True)
                send("prompt", 1, "BEFORE_OPT_IN_" + host)
                send("stop", 1, "OLD_PROGRESS_" + host)
                configure(runtime, project, {"mode":"phase", "every_phases":2}, 0)
                send("prompt", 2, "AFTER_OPT_IN_" + host)
                send("stop", 2, "CURRENT_PROGRESS_" + host)
                self.task_update(runtime, project, conversation, goal="Changed independent contract")
                send("prompt", 3, "NEW_CONTRACT_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 3, "NEW_DELIVERY_" + host)
                phase = runtime.db.execute("SELECT * FROM review_phases").fetchone()
                text = json.dumps([json.loads(row["payload"]) for row in self.phase_sources(runtime, project, phase["id"])])
                self.assertIn("NEW_CONTRACT_" + host, text)
                for old in ("BEFORE_OPT_IN_", "OLD_PROGRESS_", "AFTER_OPT_IN_", "CURRENT_PROGRESS_"):
                    self.assertNotIn(old + host, text)

    def test_all_hosts_opt_in_starts_at_explicit_enable_time(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                send, conversation = self.callbacks(runtime)
                self.task_update(runtime, project, conversation, goal="Existing active task", new_task=True)
                send("prompt", 1, "LEGACY_REQUEST_" + host)
                send("stop", 1, "LEGACY_PROGRESS_" + host)
                configure(runtime, project, {"mode":"phase", "every_phases":2}, 0)
                send("prompt", 2, "OPTED_REQUEST_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 2, "OPTED_DELIVERY_" + host)
                phase = runtime.db.execute("SELECT * FROM review_phases").fetchone()
                text = json.dumps([json.loads(row["payload"]) for row in self.phase_sources(runtime, project, phase["id"])])
                self.assertIn("OPTED_REQUEST_" + host, text)
                self.assertNotIn("LEGACY_REQUEST_" + host, text)
                self.assertNotIn("LEGACY_PROGRESS_" + host, text)

    def test_all_hosts_account_boundary_starts_new_source_window(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                configure(runtime, project, {"mode":"phase", "every_phases":2}, 0)
                send, conversation = self.callbacks(runtime)
                self.task_update(runtime, project, conversation, goal="Boundary-sensitive task", new_task=True)
                send("prompt", 1, "OLD_ACCOUNT_REQUEST_" + host)
                send("stop", 1, "OLD_ACCOUNT_PROGRESS_" + host)
                runtime.account_changed(notification_id="source-window-change")
                send("prompt", 2, "CURRENT_ACCOUNT_REQUEST_" + host)
                self.task_update(runtime, project, conversation, status="delivered")
                send("stop", 2, "CURRENT_ACCOUNT_DELIVERY_" + host)
                phase = runtime.db.execute("SELECT * FROM review_phases").fetchone()
                text = json.dumps([json.loads(row["payload"]) for row in self.phase_sources(runtime, project, phase["id"])])
                self.assertIn("CURRENT_ACCOUNT_REQUEST_" + host, text)
                self.assertNotIn("OLD_ACCOUNT_REQUEST_" + host, text)
                self.assertNotIn("OLD_ACCOUNT_PROGRESS_" + host, text)


if __name__ == "__main__":
    unittest.main()
