"""Bounded explicit version conditions, without Runtime, databases or models."""
import unittest

from agent_learning.knowledge_applicability import automatic_applicability


class ExplicitVersionApplicabilityTests(unittest.TestCase):
    current = {"project_version": "1.4.37", "project_names": ["versionguard"]}
    other = {"project_version": "9.0.0", "project_names": ["other-project"]}

    def item(self, content, **extra):
        return dict(title="versionguard 接口", content=content, category="project", **extra)

    def status(self, item, environment=None, query="versionguard v1.4.37"):
        return automatic_applicability(item, environment or {}, query=query)["status"]

    def test_original_body_conditions_exclude_old_card_without_metadata(self):
        item = self.item("适用条件：仅 v1.4.35。\n结论：versionguard 使用旧接口。\n限制：v1.4.37 不适用。")
        for environment in ({}, self.current, self.other):
            with self.subTest(environment=environment):
                self.assertEqual(self.status(item, environment), "incompatible")

    def test_current_explicit_applicability_remains_compatible(self):
        for text in ("适用条件：仅 v1.4.37。", "Applicability: only v1.4.37.",
                     "适用条件：versionguard v1.4.37。", "Applicability: versionguard version v1.4.37 only."):
            with self.subTest(text=text):
                self.assertEqual(self.status(self.item(text)), "compatible")

    def test_chinese_and_english_explicit_exclusions(self):
        for text in ("限制：v1.4.37 不适用。", "限制：不适用于 versionguard v1.4.37。",
                     "Limitations: versionguard v1.4.37 is not applicable.",
                     "Exclusions: versionguard v1.4.37.", "Limitations: not applicable to versionguard v1.4.37."):
            with self.subTest(text=text):
                self.assertEqual(self.status(self.item(text)), "incompatible")

    def test_heading_and_metadata_field_conditions(self):
        for item in (self.item("## 适用条件\n仅 v1.4.35。\n## 结论\n使用旧接口。"),
                     self.item("结论：使用旧接口。", applicability="仅 versionguard v1.4.35"),
                     self.item("结论：使用旧接口。", limitations="versionguard v1.4.37 不适用")):
            with self.subTest(item=item):
                self.assertEqual(self.status(item), "incompatible")

    def test_project_scoped_statements_use_only_project_version(self):
        item = self.item("适用条件：仅项目版本 v1.4.35。")
        self.assertEqual(self.status(item, self.current), "incompatible")
        self.assertEqual(self.status(item, {}, query="versionguard v1.4.37"), "unknown")
        self.assertEqual(self.status(item, self.other), "unknown")

    def test_manifest_conflicting_with_same_subject_query_does_not_guess(self):
        environment = dict(self.current, project_version="1.4.35")
        self.assertEqual(self.status(self.item("限制：v1.4.37 不适用。"), environment), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅项目版本 v1.4.35。"), environment), "unknown")

    def test_other_component_versions_do_not_become_project_versions(self):
        for text in ("适用条件：仅 Python v3.10。", "限制：Python v1.4.37 不适用。",
                     "适用条件：仅项目版本 v1.4.35。"):
            with self.subTest(text=text):
                self.assertEqual(self.status(self.item(text), self.other), "unknown")

    def test_bare_version_requires_an_identified_item_subject(self):
        item = {"title": "依赖约束", "content": "适用条件：仅 v1.4.35。"}
        self.assertEqual(self.status(item), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.35。"), query="v1.4.37"), "unknown")
        item = {"title": "other-tool 使用指南", "content": "适用条件：仅 v1.4.35。\n集成 versionguard。"}
        self.assertEqual(self.status(item), "unknown")
        item = {"title": "助手交付", "content": "适用条件：仅 v1.4.35。\n结论：versionguard 使用旧接口。"}
        self.assertEqual(self.status(item), "incompatible")
        for conclusion in ("结论：versionguard 与 Python 使用旧接口。", "结论：versionguard and Python use old APIs.",
                           "```text\n结论：versionguard 使用旧接口。\n```", "> 结论：versionguard 使用旧接口。",
                           "结论：versionguard 使用旧接口。\n结论：Python 使用新接口。"):
            item = {"title": "助手交付", "content": "适用条件：仅 v1.4.35。\n" + conclusion}
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.status(item), "unknown")
        item = {"title": "other-project 接口", "category": "project", "content": "适用条件：仅 v1.4.35。"}
        self.assertEqual(self.status(item, self.other), "unknown")

    def test_history_quotes_examples_code_and_fix_descriptions_are_unknown(self):
        examples = (
            "历史讨论：v1.4.35 使用旧接口，v1.4.37 已修复。",
            "History: versionguard v1.4.35 used an old API; fixed in v1.4.37.",
            "> 适用条件：仅 v1.4.35。", "```text\n适用条件：仅 v1.4.35。\n```",
            "历史讨论：\n适用条件：仅 v1.4.35。", "引用：\n适用条件：仅 v1.4.35。",
            "示例：\n适用条件：仅 v1.4.35。", "修复说明：\n适用条件：仅 v1.4.35。",
            "适用条件：修复 v1.4.35 不适用 v1.4.37 的问题。",
            '适用条件："仅 v1.4.35" 是历史引用。',
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.status(self.item(text)), "unknown")
        for title in ("versionguard 历史讨论", "versionguard historical discussion", "versionguard 修复描述"):
            item = self.item("适用条件：仅 v1.4.35。")
            item["title"] = title
            with self.subTest(title=title):
                self.assertEqual(self.status(item), "unknown")

    def test_ambiguous_negation_does_not_reverse_its_scope(self):
        for text in ("适用条件：不只 v1.4.35。", "适用条件：不仅适用于 v1.4.35。",
                     "限制：并非 v1.4.37 不适用。", "限制：不能认为 v1.4.37 不适用。",
                     "Applicability: not only v1.4.35.",
                     "Limitations: v1.4.37 is not inapplicable.",
                     "Limitations: not (v1.4.37 is not applicable).",
                     "限制：项目版本 v1.4.37 并非不适用。"):
            with self.subTest(text=text):
                self.assertEqual(self.status(self.item(text), self.current), "unknown")

    def test_alternatives_ranges_and_multicomponent_conditions_remain_unknown(self):
        for clause in ("v1.4.35 或 v1.4.37", "v1.4.35-v1.4.37", "v1.4.35 以上", ">=v1.4.35",
                       "versionguard v1.4.35 和 Python v3.10", "versionguard v1.4.35 or v1.4.37",
                       "v1.4.35+", "v1.4.*", "v1.4.x", "v1.4.35/v1.4.37"):
            with self.subTest(clause=clause):
                self.assertEqual(self.status(self.item("适用条件：仅 " + clause)), "unknown")
        item = self.item("适用条件：仅 v1.4.35。")
        item["title"] = "versionguard 与 Python 接口"
        self.assertEqual(self.status(item), "unknown")

    def test_multiversion_and_multicomponent_query_does_not_guess(self):
        item = self.item("适用条件：仅 v1.4.35。")
        for query in ("versionguard v1.4.35 升级至 v1.4.37", "versionguard v1.4.37 Python v3.10"):
            with self.subTest(query=query):
                self.assertEqual(self.status(item, query=query), "unknown")
                self.assertEqual(self.status(item, self.current, query=query), "unknown")

    def test_version_matching_has_full_numeric_boundaries(self):
        self.assertEqual(self.status(self.item("限制：v1.4.3 不适用。")), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.3。")), "incompatible")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4。")), "compatible")
        self.assertEqual(self.status(self.item("限制：v1.4.370 不适用。")), "unknown")
        self.assertEqual(self.status(self.item("限制：V1.4.37 不适用。"), query="versionguard V1.4.37"), "incompatible")

    def test_disagreeing_conditions_remain_unknown(self):
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.37。\n限制：v1.4.37 不适用。")), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.35。\n限制：v1.4.37 并非不适用。")), "unknown")

    def test_truncated_or_oversize_input_cannot_establish_incompatibility(self):
        item = self.item("适用条件：仅 v1.4.35。", content_complete=False)
        self.assertEqual(self.status(item), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.35。\n" + "界" * 40000)), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.35。"), query="x" * 5000 + " versionguard v1.4.37"), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.35。"), self.current,
                                     query="x" * 5000 + " versionguard v1.4.37"), "unknown")
        item = self.item("适用条件：仅 v1.4.37。", applicability="\n".join(["仅 v1.4.35"] * 256))
        self.assertEqual(self.status(item), "unknown")
        self.assertEqual(self.status(self.item("适用条件：仅 v1.4.35。", truncated=True)), "unknown")

    def test_existing_metadata_and_two_argument_api_remain_available(self):
        for version, status in (("1.4.35", "incompatible"), ("1.4", "compatible")):
            item = {"metadata": {"tool": "versionguard", "tool_version": version}}
            self.assertEqual(automatic_applicability(item, self.current)["status"], status)
        item = {"category": "project", "applicability": "项目版本1.4.35"}
        self.assertEqual(automatic_applicability(item, self.current)["status"], "incompatible")


if __name__ == "__main__":
    unittest.main()
