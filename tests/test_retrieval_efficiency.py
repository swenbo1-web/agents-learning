"""No models: version gates, bilingual recall and concurrent read-only startup."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.knowledge_applicability import automatic_applicability, project_environment
from agent_learning.knowledge_index import INDEX_VERSION, normalized_query_plan
from agent_learning.runtime import ContractError, Runtime


class RetrievalEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "example-project"
        self.workspace.mkdir()
        self.home = self.root / "state"
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.serial = 0

    def publish(self, title, content, *, category="project", applicability="Same project",
                limitations="Reported only", metadata=None):
        self.serial += 1
        problem = self.r.feedback(self.p, "seed", title + str(self.serial), category=category)
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title, content=content,
            category=category, applicability=applicability, limitations=limitations,
            validation_level="reported", metadata=metadata or {},
            evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])["knowledge_id"]

    def hook(self, kind="UserPromptSubmit", **fields):
        self.serial += 1
        result = self.r.handle_hook({"hook_event_name":kind, "cwd":str(self.workspace),
                                    "session_id":"chat", "turn_id":str(self.serial), **fields})
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    def manifest(self, version):
        (self.workspace / "package.json").write_text(json.dumps(
            {"name":"example-project", "version":version}), encoding="utf-8")

    def test_bilingual_concepts_recall_existing_index_in_both_directions(self):
        pairs = [("管理员项目预算", "未知调用次数保留预留", "project administrator budget unknown"),
                 ("冻结历史批次", "主审后再进行增量审核", "history frozen batch incremental review"),
                 ("knowledge revision revocation", "Inspect exposure evidence", "知识修订撤销展示证据"),
                 ("内存泄漏", "定位分配与释放", "memory leak")]
        for title, content, query in pairs:
            with self.subTest(query=query):
                expected = self.publish(title, content)
                with patch.object(self.r, "_rebuild_knowledge_index", side_effect=AssertionError("query rebuild")):
                    found = self.r.search(self.p, query, _automatic=True)["items"]
                self.assertIn(expected, [item["knowledge_id"] for item in found])

    def test_aliases_do_not_translate_inside_technical_identifiers(self):
        plan = normalized_query_plan("project_admin unknown_error /history/index.py")
        for identifier in ("project_admin", "unknown_error", "history/index.py"):
            self.assertIn(identifier, plan["terms"])
        self.assertNotIn("administrator", plan["terms"])
        self.assertLessEqual(len(plan["recall_terms"]), 128)

    def test_partial_concept_does_not_pass_automatic_gate(self):
        self.publish("管理员安装", "管理员安装步骤", category="operation")
        self.assertEqual(self.r.search(self.p, "administrator project budget unknown", _automatic=True)["items"], [])

    def test_work_observation_uses_the_same_bilingual_recall(self):
        self.r.handle_hook({"hook_event_name":"Stop", "cwd":str(self.workspace), "session_id":"prior",
            "last_assistant_message":"已整理项目管理员预算，未知调用保留预留。", "turn_id":"old"})
        found = self.r.work_search(self.p, "project administrator budget unknown")
        self.assertTrue(found["items"])

    def test_automatic_history_prefers_results_but_keeps_user_constraints(self):
        for conversation, kind, text in (("question", "UserPromptSubmit", "signaturecompat 如何处理"),
                                          ("constraint", "UserPromptSubmit", "signaturecompat 必须保留原签名"),
                                          ("answer", "Stop", "signaturecompat 已保留原签名并核对配置")):
            field = "prompt" if kind == "UserPromptSubmit" else "last_assistant_message"
            self.r.handle_hook({"hook_event_name":kind, "cwd":str(self.workspace), "session_id":conversation,
                                field:text, "turn_id":conversation})
        selected = self.r._work_search_rows(self.p, "signaturecompat", automatic=True)
        conversations = {row["conversation"] for row in selected}
        self.assertNotIn("question", conversations)
        self.assertIn("constraint", conversations)
        self.assertIn("answer", conversations)
        self.assertIn("question", {row["conversation"] for row in self.r._work_search_rows(self.p, "signaturecompat")})

    def test_version_mismatch_blocks_automatic_but_preserves_manual_access(self):
        self.manifest("2.1.0")
        old = self.publish("example-project 兼容基线", "版本 `1.0.0`、数据库 schema 15。",
                           limitations="来源为文档快照，当时状态。")
        current = self.publish("example-project 兼容基线", "项目版本2.1.0的约定。")
        text = self.hook(prompt="example-project 兼容基线")
        self.assertNotIn(old, text)
        self.assertIn(current, text)
        self.assertEqual(self.r.read(self.p, old)["knowledge_id"], old)
        self.assertIn(old, [item["knowledge_id"] for item in self.r.search(self.p, "example-project 兼容基线")["items"]])

    def test_manifest_change_invalidates_cached_context_and_compact_restore(self):
        self.manifest("1.0.0")
        old = self.publish("example-project compatibility", "baseline", applicability="项目版本1.0.0")
        new = self.publish("example-project compatibility", "baseline", applicability="项目版本2.0.0")
        self.assertIn(old, self.hook(prompt="example-project compatibility"))
        self.manifest("2.0.0")
        text = self.hook("SessionStart", source="compact")
        self.assertNotIn(old, text)
        self.assertIn(new, text)

    def test_version_change_during_retrieval_cannot_commit_old_cards(self):
        self.manifest("1.0.0")
        self.publish("compatibility", "baseline", applicability="项目版本1.0.0")
        search = self.r.search
        def changing(*args, **kwargs):
            result = search(*args, **kwargs)
            self.manifest("2.0.0")
            return result
        with patch.object(self.r, "search", side_effect=changing):
            with self.assertRaisesRegex(ContractError, "project version changed"):
                self.hook(prompt="compatibility")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 0)

    def test_incompatible_top_matches_do_not_crowd_out_current_version(self):
        self.manifest("2.0.0")
        for index in range(8):
            self.publish("compatibility " + str(index), "compatibility baseline", applicability="项目版本1.0.0")
        expected = self.publish("compatibility", "compatibility baseline", applicability="项目版本2.0.0")
        items = self.r.search(self.p, "compatibility", limit=1, _automatic=True,
                              _environment=project_environment(self.workspace))["items"]
        self.assertEqual([item["knowledge_id"] for item in items], [expected])

    def test_unknown_ranges_dependencies_and_other_tools_are_not_guessed(self):
        self.manifest("2.1.0")
        environment = project_environment(self.workspace)
        for condition in ("项目版本1.0.0以上", "项目版本1.0.0+", "project version >=1.0.0", "Python version 3.10"):
            item = {"category":"project", "applicability":condition}
            self.assertNotEqual(automatic_applicability(item, environment)["status"], "incompatible")
        item = {"metadata":{"tool":"Python", "tool_version":"3.10"}}
        self.assertEqual(automatic_applicability(item, environment)["status"], "unknown")
        dependency = {"category":"project", "title":"example-project dependency snapshot",
                      "content":"Python version 3.10", "limitations":"Historical snapshot"}
        self.assertEqual(automatic_applicability(dependency, environment)["status"], "unknown")
        item = {"metadata":{"tool":"example-project", "tool_version":"2.1"}}
        self.assertEqual(automatic_applicability(item, environment)["status"], "compatible")

    def test_conflicting_manifests_do_not_guess_a_project_version(self):
        self.manifest("2.0.0")
        (self.workspace / "pyproject.toml").write_text('[project]\nname = "backend"\nversion = "3.0.0"\n', encoding="utf-8")
        self.assertEqual(project_environment(self.workspace), {})

    def test_reopening_current_storage_does_not_acquire_a_write_transaction(self):
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("agent_learning.runtime.sqlite3.connect", side_effect=traced):
            other = Runtime(self.home)
        other.close()
        self.assertFalse(any(sql.upper().startswith(("BEGIN IMMEDIATE", "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP"))
                             for sql in statements), statements)

    def test_three_hosts_reopen_under_an_existing_writer_without_startup_writes(self):
        for host in ("antigravity", "dsh"):
            other = Runtime(self.home, host=host)
            other.close()
        barrier = threading.Barrier(3)
        failures = []
        def worker(host):
            other = None
            try:
                barrier.wait(timeout=10)
                other = Runtime(self.home, host=host)
                self.assertGreaterEqual(other.session_epoch(), 0)
            except Exception as exc:
                failures.append(repr(exc))
            finally:
                if other:
                    other.close()
        with self.r.transaction():
            threads = [threading.Thread(target=worker, args=(host,)) for host in ("codex", "antigravity", "dsh")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])

    def test_index_drift_and_schema_change_invalidate_the_startup_receipt(self):
        self.r.db.execute("UPDATE knowledge_index_state SET version=0 WHERE id=1")
        other = Runtime(self.home)
        self.assertEqual(other.db.execute("SELECT version FROM knowledge_index_state WHERE id=1").fetchone()[0], INDEX_VERSION)
        other.close()
        self.r.db.execute("DROP INDEX work_memories_scope")
        other = Runtime(self.home)
        try:
            self.assertIsNotNone(other.db.execute("SELECT 1 FROM sqlite_master WHERE name='work_memories_scope'").fetchone())
        finally:
            other.close()
