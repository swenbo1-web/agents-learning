"""Conclusion-first excerpts preserve original text, boundaries and coverage."""
import json
import unittest

from agent_learning.context_dedup import _formal_ranges
from agent_learning.knowledge_excerpt import conclusion_projection, conclusion_range
from agent_learning.knowledge_output import _summary, _shrink_summary, search_preview
from agent_learning.runtime import Runtime
from tests import test_context_dedup as fixtures


class ConclusionProjectionTests(unittest.TestCase):
    def test_complete_conclusion_beats_repeated_keywords_in_background(self):
        background = "背景：SQLite 曾经报错，仍需分析。\n" * 60
        conclusion = "结论：SQLite 使用独立事务。\n限制：仅适用于本机；失败时不得提交。\n版本或时间：v2，2026-10-28。"
        content = background + conclusion
        item = {"content": content, "match": {"matched_terms": ["SQLite"]}}
        text, start, end, suffix = conclusion_projection(content, 500)
        self.assertEqual(text, "…" + conclusion)
        self.assertEqual(content[start:end], conclusion)
        self.assertFalse(suffix)
        self.assertEqual(_summary(item, 500), text)

    def test_conditions_before_and_corrections_after_stay_attached(self):
        content = ("背景：" + "history " * 140 + "\n适用条件：只处理只读请求。\n"
                   "结论：沿用缓存。\n证据：结果由来源陈述，未经独立验证。\n"
                   "更正：写入不得复用缓存。\n细节：" + "trace " * 100)
        text, start, end, _ = conclusion_projection(content, 500)
        self.assertIn("适用条件：只处理只读请求", text)
        self.assertIn("更正：写入不得复用缓存", text)
        self.assertNotIn("trace", text)
        self.assertEqual(text, "…" + content[start:end] + "…")

    def test_over_budget_block_is_not_split_or_rephrased(self):
        content = "结论：" + "仍需核对。" * 100 + "\n限制：绝不能覆盖原文件。"
        self.assertEqual(conclusion_projection(content, 100), ("", 0, 0, True))
        self.assertEqual(_summary({"content": content}, 100), "")
        self.assertEqual(_shrink_summary({"content": content}, content, 100), "")

    def test_unlabelled_qualifier_in_other_section_cannot_be_dropped(self):
        for part in ("背景：only use this for the offline fixture.\n", "细节：未经验证，不得用于生产。\n"):
            content = (part if part.startswith("背景") else "背景：" + "trace " * 150 + "\n")
            content += "结论：使用缓存。\n"
            content += part if part.startswith("细节") else "细节：" + "trace " * 150
            with self.subTest(part=part):
                self.assertEqual(_summary({"content": content}, 100), "")

    def test_verification_limit_is_part_of_the_atomic_block(self):
        content = "背景：" + "trace " * 150 + "\n结论：使用缓存。\n验证：来源陈述，尚未独立验证。\n细节：原始流水。"
        self.assertIn("尚未独立验证", _summary({"content": content}, 200))

    def test_unrelated_conclusion_does_not_replace_a_match_in_background(self):
        content = "背景：SQLite 曾报错。" + "old investigation " * 100 + "\n结论：渲染时使用四宫格。\n限制：仅用于图像。"
        item = {"content": content, "match": {"matched_terms": ["SQLite"]}}
        self.assertEqual(_summary(item, 500), "")
        from agent_learning.work_memory import _work_preview
        self.assertEqual(_work_preview(content, "SQLite"), "")

    def test_code_quoted_list_nested_or_ambiguous_labels_do_not_promote(self):
        samples = ["```text\n结论：示例\n```", "> 结论：引用", "- 结论：列表",
                   "# 旧方案\n## 结论\n不要将旧方案作为当前结论。",
                   "结论：A\n结论：B", "```\n结论：未闭合代码",
                   "尝试结论：这并不是独立结论标题。"]
        for content in samples:
            with self.subTest(content=content):
                self.assertIsNone(conclusion_range(content))

    def test_english_markdown_and_bold_labels_keep_original_offsets(self):
        samples = ["## Background\n" + "old " * 220 + "\n## Conclusion\nUse a transaction.\n"
                   "## Limitations\nNever publish on failure.\n## Evidence\nraw trace",
                   "**背景：**" + "旧记录" * 220 + "\n**结论：**保留原数据库。\n**限制：**不得清库。"]
        for content in samples:
            with self.subTest(content=content):
                text, start, end, suffix = conclusion_projection(content, 300)
                self.assertEqual(text, ("…" if start else "") + content[start:end] + ("…" if suffix else ""))
                self.assertTrue("Never publish" in text or "不得清库" in text)

    def test_legacy_short_and_unstructured_content_remain_original_excerpts(self):
        short = {"content": "未验证的项目说明，不提升证据等级。"}
        self.assertEqual(_summary(short, 500), short["content"])
        long = "early " * 200 + "specificerror constraint " + "late " * 200
        item = {"content": long, "match": {"matched_terms": ["specificerror"]}}
        self.assertIn("specificerror", _summary(item, 500))
        self.assertIsNone(conclusion_range("结论：" + "x" * 20001))

    def test_coverage_tracks_selected_characters_not_omitted_background(self):
        content = "背景：" + "a" * 900 + "\n结论：use SQLite。\n限制：不得覆盖原文件，失败必须回滚。"
        item = {"content": content}
        rendered = _summary(item, 500)
        start, end = conclusion_range(content)
        self.assertEqual(_formal_ranges(item, {"content_summary": rendered}), [[start, end]])
        rendered = _shrink_summary(item, rendered, 24)
        self.assertEqual(rendered, "")
        self.assertEqual(_formal_ranges(item, {"content_summary": rendered}), [])

    def test_manual_small_preview_drops_whole_conclusion_and_preserves_read_entry(self):
        body = "结论：" + "保留事务边界，失败必须回滚。" * 20 + "\n限制：不得误报成功。"
        item = {"knowledge_id": "sample", "revision": 1, "title": "SQLite knowledge", "content": body,
                "applicability": "local only", "limitations": "reported, not verified", "validation_level": "reported"}
        result = search_preview({"items": [item]}, 650)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))), 650)
        self.assertTrue(result["items"])
        card = result["items"][0]
        self.assertEqual(card["content_summary"], "")
        self.assertTrue(card["read_required"])
        self.assertEqual(card["validation_level"], "reported")


class ConclusionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.r.config["work_memory"]["automatic_context"] = False

    def test_context_keeps_conditions_evidence_level_dedup_and_revoke(self):
        f = self.f
        body = "背景：dedupfocus " + "raw history " * 100 + "\n结论：dedupfocus 使用事务。\n限制：失败不得发布。"
        published, _ = f.published(content=body)
        cards = f.formal_cards(f.context())
        self.assertTrue(cards)
        card = cards[0]
        self.assertIn("结论：dedupfocus 使用事务", card["content_summary"])
        self.assertIn("失败不得发布", card["content_summary"])
        self.assertNotIn("raw history", card["content_summary"])
        self.assertEqual(card["validation_level"], "reported")
        self.assertEqual(card["limitations"], "Reported; verify current evidence")
        self.assertEqual(f.formal_cards(f.context()), [])
        self.assertTrue(f.formal_cards(f.context(compact=True)))
        f.r.revoke(f.p, published["knowledge_id"], expected_revision=1)
        self.assertIn("已撤销", f.context())
        self.assertEqual(f.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_budget_never_emits_half_a_conclusion_or_loses_identity(self):
        f = self.f
        body = "结论：dedupfocus " + "完整方案。" * 150 + "\n限制：不要截掉这一项。"
        published, _ = f.published(content=body)
        cards = f.formal_cards(f.context())
        self.assertTrue(cards)
        self.assertEqual(cards[0]["knowledge_id"], published["knowledge_id"])
        self.assertNotIn("完整方案", cards[0].get("content_summary", ""))
        self.assertTrue(cards[0]["read_required"])

    def test_three_host_core_uses_same_excerpt_with_its_delivery_scope(self):
        f = self.f
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                r = Runtime(f.root / (host + "-core"), host=host, config={
                    "admin": {"enabled": False}, "scheduler": {"enabled": False},
                    "work_memory": {"automatic_context": False}, "hindsight": {"enabled": False}})
                self.addCleanup(r.close)
                f.r = r
                f.p = r.project(f.workspace)
                f.conversation = r.host_conversation("reader")
                r.task_update(f.p, f.conversation, "dedupfocus result")
                f.handle = r.register_context(f.p, f.conversation)["context_handle"]
                body = "背景：" + "dedupfocus trace " * 100 + "\n结论：dedupfocus 保留旧接口。\n限制：仅本地测试。"
                f.published(content=body)
                delivery = "ephemeral" if host == "antigravity" else "persistent"
                card = f.formal_cards(f.context(delivery=delivery))[0]
                self.assertIn("结论：dedupfocus 保留旧接口", card["content_summary"])
                self.assertIn("仅本地测试", card["content_summary"])
                repeated = f.formal_cards(f.context(delivery=delivery))
                self.assertEqual(bool(repeated), delivery == "ephemeral")
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_narrow_index_keeps_reported_grade_and_can_expand_without_compaction(self):
        f = self.f
        conclusion = ("结论：dedupfocus 使用独立事务。" + "保留原记录及稳定身份。" * 18
                      + "\n适用条件：仅处理本机只读快照。\n限制：失败不得发布；不能覆盖原数据库。")
        body = "背景：dedupfocus " + "trace " * 180 + "\n" + conclusion
        published, _ = f.published(content=body)
        f.r.config["injection_max_chars"] = 650
        narrow = f.context()
        card = f.formal_cards(narrow)[0]
        self.assertLessEqual(len(narrow), 650)
        self.assertEqual(card["knowledge_id"], published["knowledge_id"])
        self.assertTrue(card["read_required"])
        self.assertEqual(card.get("content_summary", ""), "")
        self.assertEqual(card["validation_level"], "reported")
        self.assertEqual(f.formal_cards(f.context()), [])
        f.r.config["injection_max_chars"] = 6000
        card = f.formal_cards(f.context())[0]
        self.assertIn(conclusion, card["content_summary"])
        self.assertEqual(card["validation_level"], "reported")
        self.assertEqual(f.formal_cards(f.context()), [])


if __name__ == "__main__":
    unittest.main()
