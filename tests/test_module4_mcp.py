"""真实隔离 SQLite 的知识响应预算、会话展示登记与正文分页。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.knowledge_output import read_page, search_preview
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode


class Module4MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module4-mcp-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.workspace = base / "project"
        self.workspace.mkdir()
        self.runtime = Runtime(base / "home", config={"admin": {"enabled": False},
                                                     "hindsight": {"enabled": False}})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.sequence = 0

    def published(self, content="compiler recovery", title=None):
        self.sequence += 1
        conversation = f"source-{self.sequence}"
        self.runtime.task_update(self.project, conversation, "compiler recovery")
        problem = self.runtime.feedback(self.project, conversation, "compiler recovery", category="operation")
        packet = self.runtime.review_packet(self.project, problem["id"])
        body = dict(problem_id=problem["id"], problem_revision=packet["problem"]["revision"],
                    title=title or f"compiler recovery {self.sequence}", content=content,
                    category="operation", applicability="Configured compiler environment.",
                    limitations="Reported guidance; verify current environment.",
                    metadata={"tool": "compiler", "verification": "reported"},
                    evidence_refs=[packet["events"][0]["id"]], validation_level="reported")
        candidate = self.runtime.submit_candidate(self.project, **body)
        return self.runtime.publish(self.project, candidate["candidate_id"])

    def bind(self, conversation="reader", agent_id=None):
        return self.runtime.register_context(self.project, conversation, agent_id)["context_handle"]

    def shown(self, conversation="reader", agent_id=None):
        if agent_id:
            row = self.runtime.db.execute("SELECT shown FROM subagent_contexts WHERE project=? AND conversation=? AND agent=?",
                                          (self.project, conversation, agent_id)).fetchone()
        else:
            row = self.runtime.db.execute("SELECT shown FROM contexts WHERE project=? AND conversation=?",
                                          (self.project, conversation)).fetchone()
        return json.loads(row[0]) if row else []

    def test_large_search_is_bounded_without_evidence_or_full_body(self):
        for index in range(5):
            self.published("compiler recovery " + f"procedure{index} " * 1800)
        original = self.runtime.search(self.project, "compiler recovery", limit=5)
        self.assertGreater(len(encode(original)), 100000)
        found = Server(self.runtime, self.project).call("knowledge_search", {"query": "compiler recovery"})
        self.assertLessEqual(len(encode(found)), 6000)
        self.assertTrue(found["items"])
        self.assertEqual(json.loads(encode(found)), found)
        self.assertTrue(found["truncated"])
        self.assertEqual(len(found["items"]) + found["omitted_count"], 5)
        for item in found["items"]:
            self.assertTrue(item["summary_only"])
            for key in ("content", "evidence", "sources"):
                self.assertNotIn(key, item)
            self.assertIn("source_version", item)
            self.assertIn("problem_changed", item)

    def test_body_page_reassembles_escaped_and_unicode_content(self):
        published = self.published("compiler recovery " + ('中文\\\"\n修复步骤' * 700))
        original = self.runtime.read(self.project, published["knowledge_id"])
        server = Server(self.runtime, self.project)
        offset, chunks = 0, []
        while True:
            args = {"knowledge_id": published["knowledge_id"], "max_chars": 1024, "offset": offset}
            if offset:
                args["expected_revision"] = published["revision"]
            page = server.call("knowledge_read", args)
            self.assertLessEqual(len(encode(page)), 1024)
            self.assertEqual(json.loads(encode(page)), page)
            self.assertNotIn("evidence", page)
            self.assertLessEqual(len(page["sources"]), 3)
            self.assertFalse(any("evidence" in source for source in page["sources"]))
            self.assertEqual(page["offset"], offset)
            chunks.append(page["content"])
            if page["next_offset"] is None:
                self.assertFalse(page["truncated"])
                break
            self.assertGreater(page["next_offset"], offset)
            self.assertTrue(page["truncated"])
            offset = page["next_offset"]
        self.assertEqual("".join(chunks), original["content"])
        self.assertEqual(self.runtime.read(self.project, published["knowledge_id"]), original)

    def test_query_relevant_excerpt_keeps_match_near_end(self):
        published = self.published("prefix " * 2000 + " compiler recovery final diagnosis")
        item = self.runtime.read(self.project, published["knowledge_id"])
        item["match"] = {"matched_terms": ["diagnosis"], "score": 1, "coverage": 1, "auto_eligible": True}
        preview = search_preview({"status": "matched", "generation": 1, "revocation_epoch": 0, "items": [item]})
        self.assertIn("final diagnosis", preview["items"][0]["content_summary"])

    def test_manual_read_registers_exposure_and_revocation_notification(self):
        published = self.published()
        handle = self.bind()
        server = Server(self.runtime, self.project)
        server.call("knowledge_read", {"knowledge_id": published["knowledge_id"], "context_handle": handle})
        self.assertEqual(server.context_handle, handle)
        self.assertIn(f"{published['knowledge_id']}@{published['revision']}", self.shown())
        self.runtime.revoke(self.project, published["knowledge_id"])
        output = self.runtime.handle_hook({"hook_event_name": "PreToolUse", "session_id": "reader",
            "cwd": str(self.workspace), "tool_use_id": "after-revoke", "tool_name": "Bash"})
        self.assertIn(published["knowledge_id"], output["hookSpecificOutput"]["additionalContext"])

    def test_search_registers_only_cards_actually_returned(self):
        for index in range(3):
            self.published("compiler recovery " + f"variant{index} " * 100)
        server = Server(self.runtime, self.project, context_handle=self.bind())
        found = server.call("knowledge_search", {"query": "compiler recovery", "max_chars": 1024})
        self.assertGreater(found["omitted_count"], 0)
        expected = {f"{item['knowledge_id']}@{item['revision']}" for item in found["items"]}
        self.assertEqual(set(self.shown()), expected)
        self.assertLessEqual(len(encode(found)), 1024)

    def test_unbound_query_does_not_guess_existing_conversation(self):
        published = self.published()
        self.bind()
        self.bind("second-reader")
        server = Server(self.runtime, self.project)
        server.call("knowledge_search", {"query": "compiler recovery"})
        server.call("knowledge_read", {"knowledge_id": published["knowledge_id"]})
        self.assertIsNone(server.context_handle)
        self.assertEqual(self.shown(), [])
        self.assertEqual(self.shown("second-reader"), [])

    def test_child_exposure_is_owned_by_child_context(self):
        published = self.published()
        self.runtime.task_update(self.project, "reader", "compiler recovery")
        self.bind()
        child_handle = self.bind(agent_id="child-a")
        server = Server(self.runtime, self.project, context_handle=child_handle)
        server.call("knowledge_read", {"knowledge_id": published["knowledge_id"]})
        self.assertIn(f"{published['knowledge_id']}@{published['revision']}", self.shown(agent_id="child-a"))
        self.assertEqual(self.shown(), [])

    def test_invalid_handle_does_not_read_or_bind(self):
        published = self.published()
        server = Server(self.runtime, self.project)
        with patch.object(self.runtime, "read", wraps=self.runtime.read) as read:
            with self.assertRaises(ContractError):
                server.call("knowledge_read", {"knowledge_id": published["knowledge_id"], "context_handle": "unknown"})
            read.assert_not_called()
        self.assertIsNone(server.context_handle)

    def test_account_change_during_read_does_not_return_or_record_content(self):
        published = self.published()
        server = Server(self.runtime, self.project, context_handle=self.bind())
        original_read = self.runtime.read

        def changed_read(*args, **kwargs):
            result = original_read(*args, **kwargs)
            self.runtime.account_changed(notification_id="during-manual-read")
            return result

        with patch.object(self.runtime, "read", side_effect=changed_read):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                server.call("knowledge_read", {"knowledge_id": published["knowledge_id"]})
        self.assertEqual(self.shown(), [])

    def test_new_schema_and_paging_errors_do_not_write_exposure(self):
        published = self.published("compiler recovery " * 300)
        server = Server(self.runtime, self.project, context_handle=self.bind())
        for args in ({"query": "compiler", "max_chars": 511}, {"query": "compiler", "max_chars": True},
                     {"query": "compiler", "role": "admin"}):
            with self.assertRaises(ContractError):
                server.call("knowledge_search", args)
        for args in ({"offset": -1}, {"offset": True}, {"offset": 1},
                     {"offset": 100000, "expected_revision": 1}, {"expected_revision": 2}):
            with self.assertRaises(ContractError):
                server.call("knowledge_read", {"knowledge_id": published["knowledge_id"], **args})
        self.assertEqual(self.shown(), [])

    def test_source_baseline_is_preserved_in_preview_and_page(self):
        published = self.published()
        item = self.runtime.read(self.project, published["knowledge_id"])
        item.update(source_version=79, problem_changed=True, source_health="conflicted")
        preview = search_preview({"status": "matched", "items": [item]})["items"][0]
        page = read_page(item)
        for result in (preview, page):
            self.assertEqual(result["source_version"], 79)
            self.assertTrue(result["problem_changed"])
            self.assertEqual(result["source_health"], "conflicted")


if __name__ == "__main__":
    unittest.main()
