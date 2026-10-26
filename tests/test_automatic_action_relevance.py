"""Generic work instructions must not revive unrelated historical delivery claims."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.runtime import Runtime
from agent_learning.semantic_retrieval import SEMANTIC_DEFAULTS
from tests.test_hybrid_retrieval import LocalEmbeddings


class AutomaticActionRelevanceTests(unittest.TestCase):
    QUERY = "推送安装本地深度测试"
    OLD_TITLE = "Widget v1.0.0 旧版交付记录"
    OLD_CONTENT = "旧版修改尚未推送、尚未部署，未安装到本地；测试通过。"
    GUIDE_TITLE = "安装与发布验收"
    GUIDE_CONTENT = "推送安装后，应核对固定副本、MCP 和 Hook，并执行本地测试。"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="automatic-action-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.serial = 0
        self.r, self.p = self.runtime()

    def runtime(self, host="codex", extra=None):
        config = {"admin": {"enabled": False}, "scheduler": {"enabled": False},
                  "hindsight": {"enabled": False}, **(extra or {})}
        runtime = Runtime(self.root / (host + str(self.serial)), config=config, host=host)
        self.addCleanup(runtime.close)
        return runtime, runtime.project(self.workspace)

    def candidate(self, title, content, *, runtime=None, project=None):
        runtime, project = runtime or self.r, project or self.p
        self.serial += 1
        conversation = "source-" + str(self.serial)
        task = runtime.task_update(project, conversation, title, new_task=True)
        with runtime.transaction():
            event, _ = runtime._event(project, conversation, "Stop", {"last_assistant_message": content},
                task_id=task["id"], task_revision=task["revision"], outcome="reported")
            problem = runtime._problem(project, conversation, title, "project", [event["id"]], task_binding=task)
        result = runtime.submit_candidate(project, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title, content=content,
            category="project", applicability="本项目本地安装与交付。", limitations="公开陈述，未独立验证。",
            validation_level="reported", evidence_refs=[event["id"]])
        return result["candidate_id"]

    def publish(self, title=None, content=None):
        candidate = self.candidate(title or self.OLD_TITLE, content or self.OLD_CONTENT)
        return self.r.publish(self.p, candidate)["knowledge_id"]

    def work(self, text, *, kind="Stop", runtime=None, project=None):
        runtime, project = runtime or self.r, project or self.p
        self.serial += 1
        payload = {"prompt" if kind == "UserPromptSubmit" else "last_assistant_message": text}
        with runtime.transaction():
            event, _ = runtime._event(project, runtime.host_conversation("past-" + str(self.serial)),
                                      kind, payload, outcome="reported")
            record = runtime._work_capture_event(project, event["id"], self.workspace)
        return record["memory_id"]

    def automatic_ids(self, query=None):
        return [item["knowledge_id"] for item in self.r.search(
            self.p, self.QUERY if query is None else query, _automatic=True)["items"]]

    def test_generic_chinese_action_rejects_old_status_but_preserves_installation_guide(self):
        old = self.publish()
        guide = self.publish(self.GUIDE_TITLE, self.GUIDE_CONTENT)
        items = self.r.search(self.p, self.QUERY)["items"]
        self.assertIn(old, [item["knowledge_id"] for item in items])
        self.assertFalse(next(item for item in items if item["knowledge_id"] == old)["match"]["auto_eligible"])
        self.assertNotIn(old, self.automatic_ids())
        self.assertIn(guide, self.automatic_ids())

    def test_english_action_rejects_previous_delivery_but_preserves_procedure(self):
        old = self.publish("Previous push install local test delivery record",
            "This release has been pushed and installed. The local test passed.")
        guide = self.publish("Push install local test procedure",
            "Before you push, install dependencies and run the local test. Check the exit code before publishing.")
        identities = self.automatic_ids("push install local test")
        self.assertNotIn(old, identities)
        self.assertIn(guide, identities)

    def test_conditional_installation_instructions_are_not_status_claims(self):
        for title, content, query in (
            ("安装说明", "如果已安装则跳过安装，否则运行安装命令并执行本地测试。", "安装"),
            ("Installation notes", "If already installed, skip installation; otherwise install dependencies.", "install"),
        ):
            with self.subTest(query=query):
                guide = self.publish(title, content)
                self.assertIn(guide, self.automatic_ids(query))

    def test_generic_method_queries_prefer_guidance_over_delivery_status(self):
        old = self.publish()
        guide = self.publish(self.GUIDE_TITLE, self.GUIDE_CONTENT)
        for query in ("安装方法", "怎么安装", "查看安装说明"):
            with self.subTest(query=query):
                self.assertNotIn(old, self.automatic_ids(query))
                self.assertIn(guide, self.automatic_ids(query))

    def test_how_to_install_selects_instructions_rather_than_previous_status(self):
        pairs = [("怎么安装本地测试", self.OLD_TITLE, self.OLD_CONTENT,
                  self.GUIDE_TITLE, self.GUIDE_CONTENT),
                 ("how to install local test", "Previous install local test delivery record",
                  "This release has been installed. The local test passed.",
                  "Install local test procedure", "Follow these steps to install and run the local test.")]
        for query, title, content, guide_title, guide_content in pairs:
            with self.subTest(query=query):
                old = self.publish(title, content)
                guide = self.publish(guide_title, guide_content)
                identities = self.automatic_ids(query)
                self.assertNotIn(old, identities)
                self.assertIn(guide, identities)

    def test_imperative_installation_notes_are_not_a_delivery_snapshot(self):
        for title in ("安装说明", "知识安装说明"):
            with self.subTest(title=title):
                guide = self.publish(title, "运行安装命令，接着执行本地测试。")
                self.assertIn(guide, self.automatic_ids("安装"))

    def test_manual_search_and_read_preserve_old_authoritative_record(self):
        old = self.publish()
        before = tuple(self.r.db.execute("SELECT body FROM knowledge WHERE project=? AND id=?",
                                         (self.p, old)).fetchone())
        self.automatic_ids()
        manual = self.r.search(self.p, self.QUERY)["items"]
        self.assertIn(old, [item["knowledge_id"] for item in manual])
        self.assertEqual(self.r.read(self.p, old)["content"], self.OLD_CONTENT)
        self.assertEqual(tuple(self.r.db.execute("SELECT body FROM knowledge WHERE project=? AND id=?",
                                                (self.p, old)).fetchone()), before)

    def test_explicit_historical_version_still_selects_the_original_delivery(self):
        old = self.publish()
        self.assertIn(old, self.automatic_ids("Widget v1.0.0 旧版交付记录"))
        self.assertIn(old, self.automatic_ids("上次交付记录是否推送安装"))

    def test_error_file_and_business_topic_remain_eligible(self):
        cases = [("E_LAUNCH_FAILED 安装故障", "安装遇到 E_LAUNCH_FAILED 应核对启动路径。", "E_LAUNCH_FAILED 安装"),
                 ("src/launch.py 安装检查", "安装需要先检查 src/launch.py 入口。", "src/launch.py 安装"),
                 ("Android 消息推送", "Android 消息推送失败时核对通知权限。", "Android 消息推送失败"),
                 ("SQLite 优化", "继续优化 SQLite 查询时，应核对索引和执行计划。", "继续优化 SQLite 查询")]
        for title, content, query in cases:
            with self.subTest(query=query):
                identity = self.publish(title, content)
                self.assertIn(identity, self.automatic_ids(query))

    def test_concrete_identity_can_recall_status_without_changing_source_health(self):
        cases = [("E_LAUNCH_FAILED 安装状态", "E_LAUNCH_FAILED 的安装测试已经完成。", "E_LAUNCH_FAILED 安装"),
                 ("src/launch.py 安装状态", "src/launch.py 已经安装，测试通过。", "src/launch.py 安装")]
        for title, content, query in cases:
            with self.subTest(query=query):
                identity = self.publish(title, content)
                self.assertIn(identity, self.automatic_ids(query))
                self.assertEqual(self.r.read(self.p, identity)["source_health"], "valid")

    def test_filtering_status_does_not_mark_it_expired_or_change_validation(self):
        old = self.publish()
        self.assertNotIn(old, self.automatic_ids())
        manual = next(item for item in self.r.search(self.p, self.QUERY)["items"]
                      if item["knowledge_id"] == old)
        self.assertEqual(manual["source_health"], "valid")
        self.assertEqual(manual["validation_level"], "reported")
        self.assertEqual(self.r.read(self.p, old)["revision"], 1)

    def test_work_history_uses_the_same_generic_gate_on_all_three_hosts(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                runtime, project = (self.r, self.p) if host == "codex" else self.runtime(host)
                old = self.work(self.OLD_TITLE + "：" + self.OLD_CONTENT, runtime=runtime, project=project)
                guide = self.work(self.GUIDE_CONTENT, runtime=runtime, project=project)
                identities = [item["memory_id"] for item in runtime.work_context_items(
                    project, "reader", self.QUERY, workspace=self.workspace)]
                self.assertNotIn(old, identities)
                self.assertIn(guide, identities)
                self.assertIn(old, [item["memory_id"] for item in runtime.work_search(
                    project, self.QUERY, max_chars=10000)["items"]])

    def test_generic_old_user_request_is_not_a_useful_answer(self):
        old = self.work("继续优化", kind="UserPromptSubmit")
        self.assertIn(old, [item["memory_id"] for item in self.r.work_search(self.p, "继续优化")["items"]])
        identities = [item["memory_id"] for item in self.r.work_context_items(
            self.p, "reader", "继续优化", workspace=self.workspace)]
        self.assertNotIn(old, identities)

    def test_relevant_user_constraint_is_not_discarded_as_an_old_request(self):
        constraint = self.work("不要直接推送安装；执行本地测试后再确认。", kind="UserPromptSubmit")
        identities = [item["memory_id"] for item in self.r.work_context_items(
            self.p, "reader", self.QUERY, workspace=self.workspace)]
        self.assertIn(constraint, identities)

    def test_constraints_mixed_with_past_status_retain_original_relevance(self):
        for content, query in (
            ("保留当前安装版本；推送安装之前核对本地测试，旧版尚未推送。", self.QUERY),
            ("Keep the current version for push and install; local test passed but the previous release was installed incorrectly.",
             "push install local test"),
        ):
            with self.subTest(query=query):
                formal = self.publish("Installation constraint", content)
                self.assertIn(formal, self.automatic_ids(query))
                work = self.work(content, kind="UserPromptSubmit")
                identities = [item["memory_id"] for item in self.r.work_context_items(
                    self.p, "reader", query, workspace=self.workspace)]
                self.assertIn(work, identities)

    def test_user_correction_with_status_keeps_its_original_gate(self):
        correction = self.work("不对，推送安装本地测试未完成，不是已安装。", kind="UserPromptSubmit")
        identities = [item["memory_id"] for item in self.r.work_context_items(
            self.p, "reader", self.QUERY, workspace=self.workspace)]
        self.assertIn(correction, identities)
        self.assertNotIn(correction, [item["memory_id"] for item in self.r.work_context_items(
            self.p, "reader", "SQLite memory leak", workspace=self.workspace)])

    def test_candidate_reference_respects_generic_gate_without_changing_eligibility(self):
        old = self.candidate(self.OLD_TITLE, self.OLD_CONTENT)
        guide = self.candidate(self.GUIDE_TITLE, self.GUIDE_CONTENT)
        refs.configure(self.r, self.p, True, 0)
        self.assertTrue(refs.assess(self.r, self.p, old)["eligible"])
        items = refs.search(self.r, self.p, self.QUERY, limit=3)
        identities = [item["candidate_id"] for item in items]
        self.assertNotIn(old, identities)
        self.assertIn(guide, identities)
        self.assertTrue(refs.assess(self.r, self.p, old)["eligible"])
        self.assertIn(old, [item["candidate_id"] for item in refs.search(self.r, self.p, "Widget v1.0.0")])

    def test_hybrid_similarity_cannot_reenable_an_ineligible_status_snapshot(self):
        provider = LocalEmbeddings()
        self.addCleanup(provider.close)
        self.r.config["retrieval"]["semantic"] = {**SEMANTIC_DEFAULTS, "enabled": True,
            "model": "fixture", "url": provider.url}
        old = self.publish()
        self.r.semantic_refresh(self.p)
        manual = self.r.search(self.p, self.QUERY)["items"]
        item = next(item for item in manual if item["knowledge_id"] == old)
        self.assertEqual(item["match"]["semantic_score"], 1)
        self.assertFalse(item["match"]["auto_eligible"])
        self.assertNotIn(old, self.automatic_ids())

    def test_previous_policy_cache_does_not_reintroduce_a_filtered_delivery(self):
        old = self.publish()
        self.r.db.execute("DELETE FROM cache")
        with patch("agent_learning.knowledge_index._MATCH_POLICY_VERSION", 0):
            self.automatic_ids()
        old_key = self.r.db.execute("SELECT key FROM cache").fetchone()[0]
        legacy = json.dumps([{"knowledge_id": old, "revision": 1}])
        self.r.db.execute("UPDATE cache SET body=? WHERE key=?", (legacy, old_key))
        self.assertNotIn(old, self.automatic_ids())
        self.assertEqual(self.r.db.execute("SELECT body FROM cache WHERE key=?", (old_key,)).fetchone()[0], legacy)


if __name__ == "__main__":
    unittest.main()
