"""Automatic topic evidence must survive exclusions and descriptive queries.

All records are synthetic. Negative advice can be useful knowledge; a topic
mentioned only as an excluded use of another topic cannot qualify that record.
"""
import json
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.knowledge_index import _match_info, normalized_query_plan
from tests import test_context_dedup as fixtures


class InjectionTopicMatchTests(unittest.TestCase):
    INSTALL = "结论：Python 安装使用固定解释器路径。"
    EXCLUSION = "限制：Python 安装路径不能用于诊断内存泄漏。"
    POLICY = ("结论：PowerShell ExecutionPolicy 失败时先检查脚本签名。\n"
              "限制：不能直接全局关闭策略。")
    POLICY_QUERY = "PowerShell ExecutionPolicy 拒绝脚本执行，先检查签名，不要直接关闭全局安全策略。"

    @staticmethod
    def match(query, content, *, title="", applicability="", limitations=""):
        plan = normalized_query_plan(query)
        weights = {word: 2 if any('\u3400' <= char <= '\u9fff' for char in word) and len(word) > 1
                   else 1 for word in plan["terms"]}
        return _match_info(json.dumps({"title": title, "content": content,
            "applicability": applicability, "limitations": limitations}),
            weights, sum(weights.values()), plan["meaningful"], _anchors=plan["exact_anchors"])

    def test_inline_excluded_topic_cannot_complete_automatic_coverage(self):
        for query in ("Python memory leak", "内存泄漏"):
            with self.subTest(query=query):
                self.assertFalse(self.match(query, self.INSTALL + "\n" + self.EXCLUSION,
                    title="Python installation 安装")["auto_eligible"])

    def test_structured_limitations_cannot_complete_automatic_coverage(self):
        for limitation in ("Not applicable to memory leak diagnosis.", self.EXCLUSION):
            with self.subTest(limitation=limitation):
                self.assertFalse(self.match("Python memory leak", self.INSTALL,
                    title="Python installation 安装", limitations=limitation)["auto_eligible"])

    def test_english_inline_exclusion_cannot_supply_the_missing_topic(self):
        content = ("Conclusion: Python installation uses a fixed interpreter path.\n"
                   "Limitations: not applicable to memory leak diagnosis.")
        self.assertFalse(self.match("Python memory leak", content,
            title="Python installation")["auto_eligible"])

    def test_real_diagnosis_with_caveat_remains_eligible(self):
        content = ("结论：Python memory leak 内存泄漏需比较分配快照与残留引用。\n"
                   "限制：进程RSS增长本身不证明泄漏。")
        self.assertTrue(self.match("Python memory leak", content,
            title="Python 内存泄漏定位")["auto_eligible"])

    def test_negative_advice_is_not_discarded_as_an_excluded_topic(self):
        cases = (("SQLite SQLITE_BUSY", "结论：SQLite 遇到 SQLITE_BUSY 不应无限重试，应回滚失败事务。"),
                 ("内存泄漏", "结论：内存泄漏不能仅凭RSS增长判断，应比较残留引用。"),
                 ("PowerShell ExecutionPolicy", "结论：PowerShell ExecutionPolicy 不应全局关闭，先检查脚本签名。"))
        for query, content in cases:
            with self.subTest(query=query):
                self.assertTrue(self.match(query, content)["auto_eligible"])

    def test_positive_error_and_path_identities_survive_following_limitations(self):
        for identity in ("E_IMPORT_1042", "src/ledger.py"):
            with self.subTest(identity=identity):
                self.assertTrue(self.match(identity + " inspect recovery boundaries",
                    "结论：" + identity + " uses bounded recovery.\n限制：cannot overwrite original data.")["auto_eligible"])

    def test_specific_setting_survives_the_long_same_issue_question(self):
        self.assertTrue(self.match(self.POLICY_QUERY, self.POLICY,
            title="PowerShell ExecutionPolicy 脚本签名")["auto_eligible"])

    def test_setting_name_does_not_borrow_a_different_setting_or_tool(self):
        for content in ("PowerShell ExecutionContext selects a runspace.",
                        "PowerShell renders images and plots."):
            with self.subTest(content=content):
                self.assertFalse(self.match(self.POLICY_QUERY, content)["auto_eligible"])

    def test_setting_shortcut_cannot_drop_a_concrete_error_or_file(self):
        for identity in ("E_POLICY_1042", "src/policy.ps1"):
            query = self.POLICY_QUERY + " " + identity
            with self.subTest(identity=identity):
                self.assertFalse(self.match(query, self.POLICY,
                    title="PowerShell ExecutionPolicy 脚本签名")["auto_eligible"])
                self.assertTrue(self.match(query, self.POLICY + "\n证据：" + identity,
                    title="PowerShell ExecutionPolicy 脚本签名")["auto_eligible"])

    def test_generic_tool_mention_cannot_stand_in_for_the_topic(self):
        query = "请认真检查 PowerShell 当前项目内存泄漏的资源释放和引用关系，给出判断依据。"
        self.assertFalse(self.match(query, self.POLICY,
            title="PowerShell ExecutionPolicy 脚本签名")["auto_eligible"])

    def test_restricted_transport_does_not_veto_a_positive_business_topic(self):
        info = self.match("exec_command SQLite rollback", "SQLite rollback preserves transaction boundaries.",
                          title="SQLite rollback", limitations="exec_command output is not business success.")
        self.assertTrue(info["auto_eligible"])
        self.assertNotIn("auto_exclusion_reason", info)

    def test_long_chinese_positive_problem_phrase_remains_eligible(self):
        query = "请分析 Python 应用的内存泄漏，结合生命周期和残留引用给出判断依据，不要只看内存增长。"
        self.assertTrue(self.match(query,
            "结论：Python 内存泄漏应比较分配快照和残留引用。\n限制：不能只凭RSS增长判断。")["auto_eligible"])

    def test_markdown_restriction_blocks_cannot_supply_the_topic(self):
        for restriction in ("## 限制\n不能用于诊断内存泄漏。\n",
                            "## Limitations\nNot applicable to memory leak diagnosis.\n",
                            "**限制：** Python 安装路径不能用于诊断内存泄漏。\n"):
            with self.subTest(restriction=restriction):
                self.assertFalse(self.match("Python memory leak",
                    "## 结论\nPython 安装使用固定解释器路径。\n" + restriction,
                    title="Python installation")["auto_eligible"])

    def test_peer_conclusion_after_restriction_keeps_useful_negative_advice(self):
        content = ("## 限制\n不能用于渲染。\n## 结论\n"
                   "SQLite 遇到 SQLITE_BUSY 不应无限重试，应回滚失败事务。\n"
                   "## 验证\n来源陈述，尚未独立验证。")
        self.assertTrue(self.match("SQLite SQLITE_BUSY", content)["auto_eligible"])

    def test_setting_focus_cannot_supply_an_additional_english_business_topic(self):
        for additional in ("memory leak", "authentication refresh"):
            with self.subTest(additional=additional):
                self.assertFalse(self.match(self.POLICY_QUERY + " " + additional,
                    self.POLICY, title="PowerShell ExecutionPolicy 脚本签名")["auto_eligible"])

    def test_nested_conclusion_does_not_escape_its_restriction_section(self):
        content = "# 限制\n## 结论\nE_ARCHIVE_1042 仅作历史反例，不代表当前方案。\n"
        self.assertFalse(self.match("E_ARCHIVE_1042", content,
            title="历史归档参考")["auto_eligible"])

    def test_same_sentence_contrast_keeps_the_positive_alternative(self):
        # Ordinary incidental exclusions cannot supply a topic. An explicit
        # prohibitive conclusion is separately tested as a useful answer.
        content = "cacheguard 不适用于 Windows，但 Linux 应使用只读缓存。"
        self.assertFalse(self.match("cacheguard Windows", content,
            title="cacheguard 缓存")["auto_eligible"])
        self.assertTrue(self.match("cacheguard Linux", content,
            title="cacheguard 缓存")["auto_eligible"])

    def test_explicit_negative_conclusions_still_answer_the_named_topic(self):
        cases = (("SQLite 跨机器共享事务", "结论：SQLite 不适用于跨机器共享事务，应采用 PostgreSQL。"),
                 ("SQLite cross-machine transactions",
                  "Conclusion: SQLite cannot be used for cross-machine transactions."))
        for query, content in cases:
            with self.subTest(query=query):
                self.assertTrue(self.match(query, content, title="SQLite transaction limits")["auto_eligible"])

    def test_plain_label_cannot_escape_a_markdown_parent_restriction(self):
        content = "# 限制\nConclusion: E_ARCHIVE_1042 is only a historical excluded example.\n"
        self.assertFalse(self.match("E_ARCHIVE_1042", content, title="archive notes")["auto_eligible"])


class InjectionTopicRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @staticmethod
    def cards(text, identity):
        output = []
        for line in text.splitlines():
            if "{" not in line:
                continue
            try:
                item = json.loads(line[line.index("{"):])
            except ValueError:
                continue
            if isinstance(item, dict) and identity in item:
                output.append(item)
        return output

    def material(self, lane, title, content, *, limitations="Reported; verify current evidence"):
        f = self.f
        source = f.source(content, capture=lane == "work")
        if lane == "work":
            return source["memory"]["memory_id"]
        candidate, _ = f.candidate(source, content=content, title=title,
            applicability="Synthetic test project", limitations=limitations)
        if lane == "candidate":
            f.enable_candidates()
            return candidate["candidate_id"]
        return f.r.publish(f.p, candidate["candidate_id"])["knowledge_id"]

    def check_exclusion_lane(self, lane):
        f = self.f
        wrong_body = InjectionTopicMatchTests.INSTALL + "\n" + InjectionTopicMatchTests.EXCLUSION
        right_body = "结论：Python memory leak 内存泄漏需比较分配快照和残留引用。\n限制：RSS增长不能单独证明泄漏。"
        wrong = self.material(lane, "Python installation 安装", wrong_body)
        right = self.material(lane, "Python memory leak 内存泄漏定位", right_body)
        identity = {"formal": "knowledge_id", "candidate": "candidate_id", "work": "memory_id"}[lane]
        output = f.context("Python memory leak")
        selected = [card[identity] for card in self.cards(output, identity)]
        self.assertIn(right, selected)
        self.assertNotIn(wrong, selected)
        self.assertLessEqual(len(output), f.r.config["injection_max_chars"])
        if lane == "formal":
            # Narrow automatic qualification must not rewrite or hide the
            # original authoritative record from an explicit read.
            self.assertEqual(f.r.read(f.p, wrong)["content"], wrong_body)
            self.assertIn(wrong, [item[identity] for item in f.r.search(f.p, "Python memory leak")["items"]])
        elif lane == "candidate":
            self.assertTrue(refs.assess(f.r, f.p, wrong)["eligible"])
        else:
            self.assertEqual(f.r.work_read(f.p, wrong, workspace=f.workspace)["content"], wrong_body)

    def test_formal_automatic_context_rejects_excluded_topic_but_keeps_manual_access(self):
        self.check_exclusion_lane("formal")

    def test_opted_in_candidate_context_rejects_excluded_topic(self):
        self.check_exclusion_lane("candidate")

    def test_work_history_context_rejects_excluded_topic(self):
        self.check_exclusion_lane("work")

    def check_setting_lane(self, lane):
        f = self.f
        body = InjectionTopicMatchTests.POLICY
        target = self.material(lane, "PowerShell ExecutionPolicy 脚本签名", body)
        identity = {"formal": "knowledge_id", "candidate": "candidate_id", "work": "memory_id"}[lane]
        text = f.context(InjectionTopicMatchTests.POLICY_QUERY)
        delivered = self.cards(text, identity)
        card = next((item for item in delivered if item[identity] == target), None)
        self.assertIsNotNone(card)
        self.assertEqual(card["validation_level"], "reported")
        self.assertLessEqual(len(text), f.r.config["injection_max_chars"])
        repeated = self.cards(f.context(InjectionTopicMatchTests.POLICY_QUERY), identity)
        self.assertEqual(repeated, [])
        self.assertEqual(f.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_formal_setting_context_keeps_trust_grade_and_dedup(self):
        self.check_setting_lane("formal")

    def test_candidate_setting_context_keeps_trust_grade_and_dedup(self):
        self.check_setting_lane("candidate")

    def test_work_setting_context_keeps_trust_grade_and_dedup(self):
        self.check_setting_lane("work")

    def test_delivered_conclusion_keeps_limitations_and_reported_source_grade(self):
        f = self.f
        f.r.config["work_memory"]["automatic_context"] = False
        body = "结论：Python memory leak 内存泄漏需比较分配快照。\n限制：RSS增长不能单独证明泄漏。"
        target = self.material("formal", "Python memory leak", body)
        card = next(card for card in self.cards(f.context("Python memory leak"), "knowledge_id")
                    if card["knowledge_id"] == target)
        self.assertEqual(card["content_summary"], body)
        self.assertEqual(card["validation_level"], "reported")
        self.assertEqual(card["knowledge_tier"], "source_reference")
        self.assertFalse(card["current_tool_success"])

    def test_high_semantic_similarity_cannot_revive_a_restriction_only_topic(self):
        f = self.f
        f.r.config["work_memory"]["automatic_context"] = False
        wrong_body = InjectionTopicMatchTests.INSTALL + "\n" + InjectionTopicMatchTests.EXCLUSION
        wrong = self.material("formal", "Python installation 安装", wrong_body)
        row = f.r.db.execute("SELECT * FROM knowledge WHERE id=?", (wrong,)).fetchone()
        prepared = {"config": {"enabled": False, "automatic_min_similarity": 0},
            "diagnostics": {}, "vector": [1, 0], "signature": f.r.semantic_signature(f.p)}
        matches = {wrong: {"row": row, "score": 1, "rank": 1, "input_truncated": False}}
        query = "Python memory leak resource cleanup"
        with patch.object(f.r, "_semantic_prepare", return_value=prepared), \
                patch.object(f.r, "_semantic_candidates", return_value=matches):
            for cached in (False, True):
                with self.subTest(cached=cached):
                    actual = [item["knowledge_id"] for item in f.r.search(f.p,
                        query, _automatic=True)["items"]]
                    self.assertNotIn(wrong, actual)
            manual = f.r.search(f.p, query)["items"]
            rejected = next(item for item in manual if item["knowledge_id"] == wrong)
            self.assertLess(rejected["match"]["coverage"], 0.65)
            self.assertFalse(rejected["match"]["auto_eligible"])

    def test_multiple_camelcase_subjects_do_not_create_a_single_setting_shortcut(self):
        f = self.f
        title = "PowerShell ExecutionPolicy SessionState"
        self.material("formal", title, "结论：PowerShell ExecutionPolicy SessionState 记录可用设置。")
        query = title + " 拒绝脚本执行，先检查签名，不要直接关闭全局安全策略。"
        plan = normalized_query_plan(query)
        self.assertEqual(plan["focused_settings"], [])
        self.assertEqual(self.cards(f.context(query), "knowledge_id"), [])

    def test_original_truncation_cannot_reinfer_focus_from_its_retained_tail(self):
        f = self.f
        self.material("formal", "PowerShell ExecutionPolicy 脚本签名", InjectionTopicMatchTests.POLICY)
        query = "背景材料" * 2100 + " " + InjectionTopicMatchTests.POLICY_QUERY
        plan = normalized_query_plan(query)
        self.assertTrue(plan["query_diagnostics"]["input_truncated"])
        self.assertEqual(plan["focused_settings"], [])
        weights = {word: 2 if any('\u3400' <= char <= '\u9fff' for char in word) and len(word) > 1
                   else 1 for word in plan["terms"]}
        body = json.dumps({"title": "PowerShell ExecutionPolicy 脚本签名",
            "content": InjectionTopicMatchTests.POLICY})
        explicit = _match_info(body, weights, sum(weights.values()), plan["meaningful"],
            _anchors=plan["exact_anchors"], _focus=plan["focused_settings"])
        self.assertFalse(explicit["auto_eligible"])
        self.assertEqual(self.cards(f.context(query), "knowledge_id"), [])


if __name__ == "__main__":
    unittest.main()
