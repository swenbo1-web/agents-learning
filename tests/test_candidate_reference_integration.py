"""Candidate references through native callbacks and verified MCP connections.

All homes, configuration and temporary data are isolated below build/local.
No host process, paid model or user configuration is accessed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as references
from agent_learning.antigravity import AntigravityAdapter
from agent_learning.dsh import DshAdapter
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode


class CandidateReferenceIntegrationTests(unittest.TestCase):
    QUERY = "candidatecompilationfence"

    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / "build" / "local" / "candidate-reference-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="integration-", dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        for name in ("tmp", "home", "codex", "runtime-releases"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex"), "ALP_DSH_REVIEW_PROFILE": ""}
        environment.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, environment).start()
        self.sequence = 0

    def runtime(self, host="codex", *, work=False, suffix=""):
        runtime = Runtime(self.root / (host + suffix + "-home"), host=host,
            config={"admin": {"enabled": False}, "scheduler": {"enabled": False},
                    "hindsight": {"enabled": False}, "work_memory": {"enabled": work},
                    "injection_max_chars": 2400})
        self.addCleanup(runtime.close)
        patch.object(runtime, "_launch_admin", side_effect=AssertionError("unexpected administrator call")).start()
        project = runtime.project(self.workspace)
        return runtime, project

    def candidate(self, runtime, project, *, content=None, title=None):
        self.sequence += 1
        conversation = runtime.host_conversation("source-" + str(self.sequence))
        task = runtime.task_update(project, conversation, self.QUERY + " source", {"mode": "analysis"})
        body = {"last_assistant_message": content or self.QUERY + " recorded guidance; verify before use."}
        with runtime.transaction():
            event, _ = runtime._event(project, conversation, "Stop", body,
                                      task_id=task["id"], task_revision=task["revision"])
            if runtime.config["work_memory"]["enabled"]:
                runtime._work_capture_event(project, event["id"], self.workspace, raw=body)
        problem = runtime.feedback(project, conversation, self.QUERY + " source summary",
            category="operation", event_refs=[event["id"]])
        candidate = runtime.submit_candidate(project, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title or self.QUERY + " recorded finding",
            content=body["last_assistant_message"], category="operation",
            applicability="Recorded local compiler environment only",
            limitations="Unverified reference; independently check current evidence.",
            evidence_refs=[event["id"]], validation_level="reported", metadata={"tool": "compiler"})
        return candidate, event, problem

    def reader(self, runtime, project, native="reader"):
        conversation = runtime.host_conversation(native)
        runtime.task_update(project, conversation, self.QUERY, {"mode": "analysis"})
        if runtime.host == "antigravity":
            adapter = AntigravityAdapter(runtime, self.workspace)
            def prompt(turn, query=None):
                return adapter.handle("PreInvocation", {"conversationId": native,
                    "workspacePaths": [str(self.workspace)], "modelName": "offline-test-model",
                    "invocationNum": turn, "initialNumSteps": 0,
                    "lastUserInput": query or self.QUERY})
        elif runtime.host == "dsh":
            adapter = DshAdapter(runtime, self.workspace)
            adapter.handle("SessionStart", {"sessionId": native, "source": "startup",
                "requestContext": {"provider": "offline", "model": "test-model"}})
            def prompt(turn, query=None):
                return adapter.handle("UserPromptSubmit", {"sessionId": native,
                    "turn": turn, "prompt": query or self.QUERY})
        else:
            runtime.handle_hook({"hook_event_name": "SessionStart", "cwd": str(self.workspace),
                                 "session_id": native, "source": "startup"})
            def prompt(turn, query=None):
                return runtime.handle_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.workspace),
                    "session_id": native, "turn_id": str(turn), "prompt": query or self.QUERY})
        handle = runtime.register_context(project, conversation)["context_handle"]
        return conversation, handle, prompt

    @staticmethod
    def text(result):
        normal = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        ephemeral = "\n".join(item.get("ephemeralMessage", "") for item in result.get("injectSteps", []))
        return normal or ephemeral

    def enable(self, runtime, project):
        policy = references.policy(runtime, project)
        return references.configure(runtime, project, True, policy["version"])

    def assert_no_model_calls(self, runtime):
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_all_hosts_default_closed_and_explicit_enable(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                candidate, _, _ = self.candidate(runtime, project)
                _, _, prompt = self.reader(runtime, project)
                self.assertFalse(references.policy(runtime, project)["enabled"])
                self.assertNotIn(candidate["candidate_id"], self.text(prompt(1)))
                self.enable(runtime, project)
                text = self.text(prompt(2))
                self.assertIn(candidate["candidate_id"], text)
                self.assertIn("reported", text)
                self.assertLessEqual(len(text), 2400)
                self.assert_no_model_calls(runtime)

    def test_native_delivery_repeats_only_ephemeral_candidate_references(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                self.enable(runtime, project)
                _, _, prompt = self.reader(runtime, project)
                # Publish the candidate after native startup so startup did not already expose it.
                candidate, _, _ = self.candidate(runtime, project)
                identity = candidate["candidate_id"]
                self.assertIn(identity, self.text(prompt(1)))
                following = self.text(prompt(2))
                if host == "antigravity":
                    self.assertIn(identity, following)
                else:
                    self.assertNotIn(identity, following)
                self.assert_no_model_calls(runtime)

    def test_tiny_budget_does_not_acknowledge_undelivered_reference(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        runtime.config["injection_max_chars"] = 128
        small = runtime._context(project, "reader", query=self.QUERY, context_handle=handle, workspace=self.workspace)
        self.assertLessEqual(len(small), 128)
        self.assertNotIn(candidate["candidate_id"], small)
        runtime.config["injection_max_chars"] = 2400
        full = runtime._context(project, "reader", query=self.QUERY, context_handle=handle, workspace=self.workspace)
        self.assertIn(candidate["candidate_id"], full)

    def test_compaction_revalidates_rejected_candidate_and_keeps_notification(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        marker = "PENDING_UNVERIFIED_INSTRUCTION"
        candidate, _, _ = self.candidate(runtime, project, content=self.QUERY + " " + marker)
        identity = candidate["candidate_id"]
        self.assertIn(identity, runtime._context(project, "reader", query=self.QUERY,
            context_handle=handle, workspace=self.workspace))
        with runtime.transaction():
            runtime.db.execute("UPDATE candidates SET status='rejected' WHERE project=? AND id=?", (project, identity))
        text = runtime._context(project, "reader", query=self.QUERY, compact=True,
            context_handle=handle, workspace=self.workspace)
        self.assertNotIn(marker, text)
        self.assertIn(identity, text)  # A complete invalidation notice must replace the card.
        self.assert_no_model_calls(runtime)

    def test_status_change_after_recall_prevents_stale_card(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        original = references.search
        def changed(*args, **kwargs):
            items = original(*args, **kwargs)
            with runtime.transaction():
                runtime.db.execute("UPDATE candidates SET status='rejected' WHERE project=? AND id=?",
                                   (project, candidate["candidate_id"]))
            return items
        with patch.object(references, "search", side_effect=changed):
            text = runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                    workspace=self.workspace)
        self.assertNotIn(candidate["candidate_id"], text)
        self.assert_no_model_calls(runtime)

    def test_derived_fts_failure_preserves_formal_knowledge_and_task_context(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        formal_candidate, _, _ = self.candidate(runtime, project)
        formal = runtime.publish(project, formal_candidate["candidate_id"])
        pending, _, _ = self.candidate(runtime, project, title=self.QUERY + " pending alternative")
        with patch.object(references, "search", side_effect=sqlite3.OperationalError("candidate FTS unavailable")):
            text = runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                    workspace=self.workspace)
        self.assertIn(formal["knowledge_id"], text)
        self.assertIn("task=", text)
        self.assertNotIn(pending["candidate_id"], text)
        self.assert_no_model_calls(runtime)

    def test_invalid_handle_is_not_swallowed_as_optional_reference_failure(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        self.reader(runtime, project)
        self.candidate(runtime, project)
        with self.assertRaises(ContractError):
            runtime._context(project, "reader", query=self.QUERY, context_handle="untrusted-handle",
                             workspace=self.workspace)


    def test_source_revision_change_after_recall_prevents_stale_reference(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, problem = self.candidate(runtime, project)
        original = references.search
        def changed(*args, **kwargs):
            items = original(*args, **kwargs)
            with runtime.transaction():
                runtime.db.execute("UPDATE problems SET revision=revision+1 WHERE project=? AND id=?",
                                   (project, problem["id"]))
            return items
        with patch.object(references, "search", side_effect=changed):
            text = runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                    workspace=self.workspace)
        self.assertNotIn(candidate["candidate_id"], text)

    def test_reference_and_work_history_share_optional_pool(self):
        runtime, project = self.runtime(work=True)
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        body = {"last_assistant_message": self.QUERY + " unrelated source provides separate reported history."}
        with runtime.transaction():
            event, _ = runtime._event(project, "independent-work", "Stop", body)
            runtime._work_capture_event(project, event["id"], self.workspace, raw=body)
        text = runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                workspace=self.workspace)
        self.assertIn(candidate["candidate_id"], text)
        self.assertLessEqual(len(text), 2400)
        boundary = next(index for index, line in enumerate(text.splitlines())
                        if candidate["candidate_id"] in line)
        optional = text.splitlines()[boundary:]
        if boundary and "task=" not in text.splitlines()[boundary - 1]:
            optional.insert(0, text.splitlines()[boundary - 1])
        self.assertLessEqual(len("\n".join(optional)) + 1, 600)
        self.assertEqual(sum(candidate["candidate_id"] in line for line in optional), 1)
        self.assert_no_model_calls(runtime)

    def test_candidate_source_does_not_also_inject_same_work_memory(self):
        runtime, project = self.runtime(work=True)
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, event, _ = self.candidate(runtime, project)
        memory = runtime.db.execute("SELECT id FROM work_memories WHERE project=? AND event_id=?",
                                    (project, event["id"])).fetchone()
        self.assertIsNotNone(memory)
        text = runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                workspace=self.workspace)
        self.assertIn(candidate["candidate_id"], text)
        self.assertNotIn(memory["id"], text)

    def test_published_source_is_not_duplicated_as_candidate_or_work_history(self):
        runtime, project = self.runtime(work=True)
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, event, _ = self.candidate(runtime, project)
        memory = runtime.db.execute("SELECT id FROM work_memories WHERE project=? AND event_id=?",
                                    (project, event["id"])).fetchone()
        knowledge = runtime.publish(project, candidate["candidate_id"])
        text = runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                workspace=self.workspace)
        self.assertIn(knowledge["knowledge_id"], text)
        self.assertNotIn(candidate["candidate_id"], text)
        self.assertNotIn(memory["id"], text)

    def test_mcp_candidate_read_requires_binding_and_preserves_project_and_hash(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        identity = candidate["candidate_id"]
        version = references.assess(runtime, project, identity)["reference_version"]
        arguments = {"candidate_id": identity, "expected_version": version}
        with self.assertRaises(ContractError):
            Server(runtime, project).call("candidate_reference_read", arguments)
        with self.assertRaises(ContractError):
            Server(runtime, project).call("candidate_reference_read", {**arguments, "context_handle": "untrusted"})
        server = Server(runtime, project, context_handle=handle)
        before = runtime.task(project, "reader")
        page = server.call("candidate_reference_read", {**arguments, "max_chars": 2000})
        self.assertEqual(page["candidate_id"], identity)
        self.assertEqual(page["reference_version"], version)
        self.assertLessEqual(len(encode(page)), 2000)
        self.assertEqual(runtime.task(project, "reader"), before)
        self.assert_no_model_calls(runtime)
        for extra in ({"role": "admin"}, {"workspace": "outside"}, {"conversation_id": "source-1"}):
            with self.assertRaises(ContractError):
                server.call("candidate_reference_read", {**arguments, **extra})
        with self.assertRaises(ContractError):
            server.call("candidate_reference_read", {**arguments, "expected_version": "0" * 64})
        with runtime.transaction():
            runtime.db.execute("UPDATE candidates SET status='rejected' WHERE project=? AND id=?", (project, identity))
        with self.assertRaises(ContractError):
            server.call("candidate_reference_read", arguments)

    def test_mcp_candidate_counterexample_blocks_reference_without_admin_call(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        identity = candidate["candidate_id"]
        version = references.assess(runtime, project, identity)["reference_version"]
        server = Server(runtime, project, context_handle=handle)
        before = runtime.task(project, "reader")
        server.call("candidate_reference_read", {"candidate_id": identity, "expected_version": version})
        arguments = {"candidate_id": identity, "expected_version": version,
                     "summary": "Actual current environment contradicts the recorded guidance.",
                     "idempotency_key": "candidate-reference-negative-feedback",
                     "expected_task_id": before["id"], "expected_task_revision": before["revision"]}
        first = server.call("candidate_reference_feedback", arguments)
        self.assertEqual(first, server.call("candidate_reference_feedback", arguments))
        self.assertFalse(references.assess(runtime, project, identity)["eligible"])
        self.assertEqual(references.search(runtime, project, self.QUERY), [])
        self.assertEqual(runtime.task(project, "reader"), before)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assert_no_model_calls(runtime)

    def test_mcp_pagination_preserves_escaped_body_inside_json_budget(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        content = self.QUERY + ' 中文\\"\n待验证。' * 400
        candidate, _, _ = self.candidate(runtime, project, content=content)
        identity = candidate["candidate_id"]
        version = references.assess(runtime, project, identity)["reference_version"]
        server = Server(runtime, project, context_handle=handle)
        offset, pieces = 0, []
        while True:
            page = server.call("candidate_reference_read", {"candidate_id": identity,
                "expected_version": version, "section": "content", "offset": offset, "max_chars": 1024})
            self.assertLessEqual(len(encode(page)), 1024)
            self.assertEqual(json.loads(encode(page)), page)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["reference_version"], version)
            pieces.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(pieces), content)
        self.assert_no_model_calls(runtime)

    def test_small_budget_retains_invalidation_until_complete_tool_notification(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        identity = candidate["candidate_id"]
        self.assertIn(identity, runtime._context(project, "reader", query=self.QUERY,
            context_handle=handle, workspace=self.workspace))
        with runtime.transaction():
            runtime.db.execute("UPDATE candidates SET status='rejected' WHERE project=? AND id=?", (project, identity))
        runtime.config["injection_max_chars"] = 128
        small = runtime._context(project, "reader", query=self.QUERY,
            context_handle=handle, workspace=self.workspace)
        self.assertLessEqual(len(small), 128)
        self.assertNotIn(identity, small)
        self.assertIn(identity, [item["candidate_id"] for item in references.notifications(runtime, project, handle)])
        runtime.config["injection_max_chars"] = 2400
        complete = runtime._context(project, "reader", query=self.QUERY, tool_only=True,
            context_handle=handle, workspace=self.workspace)
        self.assertIn(identity, complete)
        self.assertNotIn(identity, [item["candidate_id"] for item in references.notifications(runtime, project, handle)])

    def test_optional_channel_never_swallows_identity_contract_error(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        self.candidate(runtime, project)
        with patch.object(references, "search", side_effect=ContractError("candidate binding changed")):
            with self.assertRaisesRegex(ContractError, "binding changed"):
                runtime._context(project, "reader", query=self.QUERY, context_handle=handle,
                                 workspace=self.workspace)

    def test_mcp_account_switch_after_page_creation_rejects_return(self):
        runtime, project = self.runtime()
        self.enable(runtime, project)
        _, handle, _ = self.reader(runtime, project)
        candidate, _, _ = self.candidate(runtime, project)
        identity = candidate["candidate_id"]
        version = references.assess(runtime, project, identity)["reference_version"]
        original = references.read
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            runtime.account_changed("candidate-read-account-switch")
            return result
        server = Server(runtime, project, context_handle=handle)
        with patch.object(references, "read", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                server.call("candidate_reference_read", {"candidate_id": identity, "expected_version": version})


if __name__ == "__main__":
    unittest.main()
