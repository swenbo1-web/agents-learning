"""新能力与既有CLI、Hook、MCP预算的契约；不调用真实模型或知识库。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.knowledge_output import search_preview
from agent_learning.runtime import ContractError, Runtime, encode


class ProductIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-products-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "project"
        self.workspace.mkdir()
        self.home = self.base / "state"
        self.r = Runtime(self.home, config={})
        self.addCleanup(lambda: self.r.close())
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "diagnostic task")

    def hook(self, event, **fields):
        return self.r.handle_hook(dict(hook_event_name=event, session_id="chat",
                                       cwd=str(self.workspace), **fields))

    def test_default_semantic_is_off_and_partial_config_merges(self):
        self.assertFalse(self.r.config["retrieval"]["semantic"]["enabled"])
        other = Runtime(self.base / "partial", config={"retrieval": {"semantic": {"model": "local-test"}}})
        self.addCleanup(other.close)
        self.assertFalse(other.config["retrieval"]["semantic"]["enabled"])
        self.assertEqual(other.config["retrieval"]["semantic"]["candidate_limit"], 32)
        self.assertEqual(other.config["retrieval"]["semantic"]["model"], "local-test")

    def test_bad_config_rejected_before_database_creation(self):
        for index, config in enumerate(([], {"retrieval": []}, {"retrieval": {"other": 1}},
                                       {"retrieval": {"semantic": {"enabled": True}}},
                                       {"retrieval": {"semantic": {"url": "http://[broken"}}})):
            home = self.base / f"bad-{index}"
            with self.subTest(config=config), self.assertRaises(ContractError):
                Runtime(home, config=config)
            self.assertFalse((home / "runtime.sqlite3").exists())

    def test_semantic_refresh_cli_default_makes_no_provider_call(self):
        with patch("agent_learning.semantic_retrieval.embed_texts", side_effect=AssertionError("model called")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--home", str(self.home), "--workspace", str(self.workspace), "semantic-refresh"])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(result["requests"], 0)

    def test_semantic_failure_cools_across_runtime_reopen_then_retries(self):
        fallback = {"status": "no_match", "items": [], "retrieval": {
            "mode": "hybrid", "semantic_status": "degraded", "retryable": True, "retry_after_seconds": 5}}
        with patch("agent_learning.knowledge_context.time.time", return_value=100), patch.object(
                self.r, "search", return_value=fallback) as searched:
            self.hook("UserPromptSubmit", prompt="memory recovery", turn_id="first")
            self.assertEqual(searched.call_count, 1)
        signature = self.r.db.execute("SELECT retrieval_signature FROM contexts WHERE conversation='chat'").fetchone()[0]
        self.assertEqual(float(signature.split(":", 2)[1]), 105)
        self.r.close()
        self.r = Runtime(self.home, config={})
        with patch("agent_learning.knowledge_context.time.time", return_value=101), patch.object(
                self.r, "search", side_effect=AssertionError("cooldown lost across processes")):
            self.hook("UserPromptSubmit", prompt="继续", turn_id="cooling")
        with patch("agent_learning.knowledge_context.time.time", return_value=106), patch.object(
                self.r, "search", return_value={"status": "no_match", "items": []}) as searched:
            self.hook("UserPromptSubmit", prompt="继续", turn_id="recovered")
            self.assertEqual(searched.call_count, 1)
        self.assertFalse(self.r.db.execute("SELECT retrieval_signature FROM contexts WHERE conversation='chat'").fetchone()[0].startswith("retry:"))

    def test_index_generation_change_invalidates_continuation_plan(self):
        with patch.object(self.r, "semantic_signature", return_value="version-one"), patch.object(
                self.r, "search", return_value={"status": "no_match", "items": []}):
            self.hook("UserPromptSubmit", prompt="memory recovery", turn_id="first")
        with patch.object(self.r, "semantic_signature", return_value="version-two"), patch.object(
                self.r, "search", return_value={"status": "no_match", "items": []}) as searched:
            self.hook("UserPromptSubmit", prompt="继续", turn_id="new-index")
            self.assertEqual(searched.call_count, 1)

    def test_failed_tool_pending_semantic_retry_is_not_consumed(self):
        self.hook("SessionStart")
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        fallback = {"status": "no_match", "items": [], "retrieval": {"retryable": True}}
        with patch("agent_learning.knowledge_context.time.time", return_value=100), patch.object(self.r, "search", return_value=fallback):
            self.hook("PreToolUse", tool_use_id="retry-one", tool_name="Read")
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "pending")
        with patch("agent_learning.knowledge_context.time.time", return_value=101), patch.object(
                self.r, "search", side_effect=AssertionError("failure retry ignored cooldown")):
            self.hook("PreToolUse", tool_use_id="retry-two", tool_name="Read")
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "pending")
        with patch("agent_learning.knowledge_context.time.time", return_value=106), patch.object(
                self.r, "search", return_value={"status": "no_match", "items": []}) as searched:
            self.hook("PreToolUse", tool_use_id="retry-three", tool_name="Read")
            self.assertEqual(searched.call_count, 1)
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "done")

    def test_mcp_preview_preserves_hybrid_explanation_and_budget(self):
        item = {"knowledge_id": "knowledge", "revision": 1, "title": "memory recovery",
                "content": "procedure " * 3000, "applicability": "local", "limitations": "verify",
                "source_health": "valid", "match": {"score": 0, "coverage": 0, "matched_terms": [],
                "auto_eligible": True, "method": "hybrid", "semantic_score": 0.96, "semantic_rank": 1,
                "fusion_method": "reciprocal_rank_k60", "semantic_input_truncated": False}}
        result = {"status": "matched", "items": [item], "retrieval": {"mode": "hybrid", "semantic_status": "ready", "retryable": False}}
        preview = search_preview(result)
        self.assertEqual(preview["items"][0]["match"]["semantic_score"], 0.96)
        self.assertEqual(preview["items"][0]["match"]["fusion_method"], "reciprocal_rank_k60")
        self.assertEqual(preview["retrieval"]["semantic_status"], "ready")
        for budget in (512, 6000):
            output = search_preview(result, max_chars=budget)
            self.assertLessEqual(len(encode(output)), budget)
            self.assertEqual(json.loads(encode(output)), output)

    def test_waiting_failure_card_rechecks_changed_semantic_generation(self):
        problem = self.r.feedback(self.p, "chat", "Bash E_IMPORT observed", category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Bash E_IMPORT", content="import recovery", category="operation", applicability="local",
            limitations="verify", validation_level="reported", evidence_refs=[packet["events"][0]["id"]])
        published = self.r.publish(self.p, candidate["candidate_id"])
        item = self.r.read(self.p, published["knowledge_id"])
        item["match"] = {"auto_eligible": True, "matched_terms": [], "method": "hybrid", "semantic_score": 0.99}
        self.hook("SessionStart")
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        self.r.config["injection_max_chars"] = 128
        with patch.object(self.r, "semantic_signature", return_value="old-model"), patch.object(
                self.r, "search", return_value={"status": "matched", "items": [item]}):
            self.hook("PreToolUse", tool_use_id="small", tool_name="Read")
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "ready")
        self.r.config["injection_max_chars"] = 2400
        with patch.object(self.r, "semantic_signature", return_value="new-model"), patch.object(
                self.r, "search", return_value={"status": "no_match", "items": []}) as searched:
            result = self.hook("PreToolUse", tool_use_id="new-model", tool_name="Read")
            self.assertEqual(searched.call_count, 1)
        self.assertNotIn(published["knowledge_id"], encode(result))

    def test_browser_missing_database_does_not_initialize_runtime(self):
        home = self.base / "absent"
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("browser initialized Runtime")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--home", str(home), "browser"]), 1)
        self.assertFalse(home.exists())

    def test_waiting_failure_card_rechecks_new_knowledge_publication(self):
        def publish(title):
            problem = self.r.feedback(self.p, "chat", title + " observed", category="operation")
            packet = self.r.review_packet(self.p, problem["id"])
            candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
                title=title, content="recovery " + title, category="operation", applicability="local",
                limitations="verify", validation_level="reported", evidence_refs=[packet["events"][0]["id"]])
            published = self.r.publish(self.p, candidate["candidate_id"])
            item = self.r.read(self.p, published["knowledge_id"])
            item["match"] = {"auto_eligible": True, "matched_terms": []}
            return item
        old = publish("Bash E_IMPORT old")
        self.hook("SessionStart")
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        self.r.config["injection_max_chars"] = 128
        with patch.object(self.r, "search", return_value={"status": "matched", "items": [old]}):
            self.hook("PreToolUse", tool_use_id="small", tool_name="Read")
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "ready")
        new = publish("Bash E_IMPORT new")
        self.r.config["injection_max_chars"] = 2400
        with patch.object(self.r, "search", return_value={"status": "matched", "items": [new]}) as searched:
            result = self.hook("PreToolUse", tool_use_id="fresh", tool_name="Read")
            self.assertEqual(searched.call_count, 1)
        self.assertIn(new["knowledge_id"], encode(result))
        self.assertNotIn(old["knowledge_id"], encode(result))

    def test_browser_cli_closes_cleanly_without_writable_runtime(self):
        class FakeServer:
            url = "http://127.0.0.1:1234/"
            access_token = "x" * 48
            browser_url = url + "#token=" + access_token
            closed = False
            def serve_forever(self, **kwargs):
                raise KeyboardInterrupt
            def server_close(self):
                self.closed = True
        server = FakeServer()
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("browser initialized Runtime")), patch(
                "agent_learning.knowledge_browser.make_browser_server", return_value=server), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "--workspace", str(self.workspace), "browser"]), 0)
        self.assertTrue(server.closed)

    def test_evaluation_cli_uses_files_without_creating_database(self):
        spec = {"id": "synthetic", "data_class": "synthetic_template", "model": {
                "provider": "synthetic", "name": "fake", "version": "1", "parameters": {}},
                "environment_sha256": "a" * 64, "knowledge_sha256": "b" * 64, "repeats": 2, "seed": 0,
                "tasks": [{"id": "task", "prompt": "synthetic", "dataset_sha256": "c" * 64, "acceptance_criteria": "fixture"}]}
        spec_path, output = self.base / "spec.json", self.base / "protocol.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        home = self.base / "absent"
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("evaluation initialized Runtime")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--home", str(home), "evaluation-protocol", "--spec", str(spec_path), "--output", str(output)]), 0)
        self.assertTrue(output.is_file())
        self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
