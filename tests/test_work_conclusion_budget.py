"""One full working conclusion may use the existing optional pool, never more."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references
from agent_learning.runtime import Runtime


class WorkConclusionBudgetTests(unittest.TestCase):
    HEADER = "工作历史索引（非正式解法/执行授权；仅相关且索引不足时用 work_memory_read）："

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="work-conclusion-budget-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "budgetfocus read-only analysis", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]
        self.sequence = 0

    def record(self, block=None, *, file=False):
        self.sequence += 1
        block = block or self.block()
        text = "背景：\n" + "budgetfocus 早期排查记录。" * 12 + "\n" + block + "\n细节：\n" + "具体过程仍可展开。" * 12
        payload = {"last_assistant_message": text}
        if file:
            (self.workspace / "memory.py").write_text("old = True\n", encoding="utf-8")
            payload.update(tool_name="read_file", tool_input={"path": "memory.py"})
        with self.r.transaction():
            event, _ = self.r._event(self.p, "past-%d" % self.sequence, "Stop", payload, outcome="reported")
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return memory["memory_id"], text

    @staticmethod
    def block():
        return ("结论：\nbudgetfocus 已修复回执中缺失的模型字段，并完成本地复核。\n"
                "限制：\n仅适用于 schema 25 的隔离项目；远端部署仍需重新验收，不能从本地结果推定远端已经验证。")

    def context(self, **kwargs):
        return self.r._context(self.p, "reader", query="budgetfocus", workspace=self.workspace,
            context_handle=self.handle, **kwargs)

    @staticmethod
    def cards(text):
        return [json.loads(line[len("[工作历史] "):]) for line in text.splitlines() if line.startswith("[工作历史] ")]

    def test_complete_relevant_block_over_80_survives_selection_validation_and_render(self):
        block = self.block()
        self.assertGreater(len(block), 80)
        self.assertLessEqual(len(block) + 2, 160)
        ident, original = self.record(block)
        selected = self.r.work_context_items(self.p, "reader", "budgetfocus", workspace=self.workspace)
        self.assertEqual(next(item for item in selected if item["memory_id"] == ident)["summary"], "")
        output = self.context()
        card = next(card for card in self.cards(output) if card["memory_id"] == ident)
        self.assertEqual(card["summary"], "…" + block + "…")
        self.assertEqual(card["validation_level"], "reported")
        self.assertTrue(card["read_required"])
        self.assertLessEqual(len(output), 2400)
        optional = output[output.index(self.HEADER):]
        self.assertLessEqual(len(optional), 600)
        self.assertEqual(self.r.work_read(self.p, ident, workspace=self.workspace)["content"], original)

    def test_only_one_new_conclusion_expands_in_the_existing_pool(self):
        one, _ = self.record()
        two, _ = self.record(self.block().replace("模型字段", "来源字段"))
        self.r.config["work_memory"]["max_context_chars"] = 1000
        output = self.context()
        cards = self.cards(output)
        self.assertTrue({one, two} <= {card["memory_id"] for card in cards})
        self.assertEqual(sum(len(card.get("summary", "")) > 80 for card in cards), 1)
        self.assertLessEqual(len(output[output.index(self.HEADER):]), 1000)

    def test_insufficient_pool_keeps_an_index_without_half_a_conclusion(self):
        ident, _ = self.record()
        self.r.config["work_memory"]["max_context_chars"] = 300
        output = self.context()
        card = next(card for card in self.cards(output) if card["memory_id"] == ident)
        self.assertNotIn("远端部署", card.get("summary", ""))
        self.assertLessEqual(len(card.get("summary", "")), 0)
        self.assertTrue(card["read_required"])
        self.assertLessEqual(len(output[output.index(self.HEADER):]), 300)

    def test_over_240_complete_conditions_stay_index_only(self):
        block = "结论：\nbudgetfocus 已修复。\n限制：\n" + "只对逐项验收完成的环境生效；" * 20
        ident, _ = self.record(block)
        card = next(card for card in self.cards(self.context()) if card["memory_id"] == ident)
        self.assertFalse(card.get("summary"))
        self.assertTrue(card["read_required"])

    def test_unstructured_work_excerpt_and_manual_search_do_not_expand(self):
        ident, _ = self.record()
        manual = next(item for item in self.r.work_search(self.p, "budgetfocus", max_chars=10000)["items"]
                      if item["memory_id"] == ident)
        self.assertLessEqual(len(manual["summary"]), 120)
        self.assertFalse(any(key.startswith("_conclusion") for key in manual))
        text = "budgetfocus plain evidence " * 12
        with self.r.transaction():
            event, _ = self.r._event(self.p, "plain", "Stop", {"last_assistant_message": text}, outcome="reported")
            self.r._work_capture_event(self.p, event["id"], self.workspace)
        cards = self.cards(self.context())
        self.assertTrue(all(len(card.get("summary", "")) <= 80 for card in cards if card["memory_id"] != ident))

    def test_persistent_repeat_remains_suppressed_and_compaction_does_not_cut_block(self):
        ident, _ = self.record()
        first = self.cards(self.context())
        self.assertGreater(len(first[0].get("summary", "")), 80)
        self.assertNotIn(ident, self.context())
        compact = next(card for card in self.cards(self.context(compact=True)) if card["memory_id"] == ident)
        self.assertIn("不能从本地结果推定远端已经验证", compact["summary"])

    def test_source_change_notification_keeps_priority_without_old_body(self):
        ident, _ = self.record(file=True)
        self.context()
        (self.workspace / "memory.py").write_text("different = True\n", encoding="utf-8")
        output = self.context()
        card = next(card for card in self.cards(output) if card["memory_id"] == ident)
        self.assertEqual(card["reference_role"], "source_update")
        self.assertTrue(card["file_state_changed"])
        self.assertNotIn("summary", card)
        self.assertLessEqual(len(output[output.index(self.HEADER):]), 600)

    def test_deduped_first_item_does_not_spend_the_only_expansion_slot(self):
        old, _ = self.record()
        old_item = self.r.work_context_items(self.p, "reader", "budgetfocus", workspace=self.workspace)[0]
        self.assertIn(old, self.context())
        new, _ = self.record(self.block().replace("模型字段", "工具字段"))
        new_item = next(item for item in self.r.work_context_items(self.p, "reader", "budgetfocus",
            workspace=self.workspace) if item["memory_id"] == new)
        with patch.object(self.r, "work_context_items", return_value=[old_item, new_item]):
            cards = self.cards(self.context())
        self.assertEqual([card["memory_id"] for card in cards], [new])
        self.assertGreater(len(cards[0]["summary"]), 80)

    def test_candidate_keeps_its_prior_share_before_work_conclusion(self):
        ident, _ = self.record()
        candidate_content = "budgetfocus candidate reference remains unverified; read its source before using it."
        source, _ = self.record("结论：\n" + candidate_content)
        event = self.r.db.execute("SELECT event_id,conversation FROM work_memories WHERE id=?", (source,)).fetchone()
        problem = self.r.feedback(self.p, event["conversation"], "budgetfocus candidate source", category="project",
            event_refs=[event["event_id"]])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="budgetfocus candidate reference", content=candidate_content, category="project", knowledge_kind="fact",
            applicability="local only", limitations="unverified", validation_level="reported", evidence_refs=[event["event_id"]])
        policy = candidate_references.policy(self.r, self.p)
        candidate_references.configure(self.r, self.p, True, policy["version"])
        output = self.context()
        self.assertIn(candidate["candidate_id"], output)
        self.assertFalse(any(len(card.get("summary", "")) > 80 for card in self.cards(output)))
        self.assertLessEqual(len(output), 2400)
        self.assertNotIn(ident, output)  # Candidate + full work card cannot share the 600-char pool.

    def test_existing_task_handoff_still_uses_its_own_160_char_rule(self):
        text = "Phase complete.\n\nPending: preserve budgetfocus revision 37 until validation passes."
        task = self.r.task(self.p, "reader")
        with self.r.transaction():
            event, _ = self.r._event(self.p, "reader", "Stop", {"last_assistant_message": text, "completion": "complete"},
                task_id=task["id"], task_revision=task["revision"], task_write_version=task["write_version"], outcome="reported")
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace)
        output = self.r._context(self.p, "reader", query="unmatchedtopic", compact=True, workspace=self.workspace,
            context_handle=self.handle)
        card = next(card for card in self.cards(output) if card["memory_id"] == memory["memory_id"])
        self.assertEqual(card["reference_role"], "task_handoff")
        self.assertIn("until validation passes", card["summary"])
        self.assertLessEqual(len(card["summary"]), 160)

    def test_old_index_exposure_can_upgrade_without_compaction_or_source_change(self):
        ident, _ = self.record()
        original = self.r.work_validate_items
        def legacy(project, items, workspace=None, **kwargs):
            return original(project, items, workspace=workspace)  # Simulate the previous 80-char preview.
        with patch.object(self.r, "work_validate_items", side_effect=legacy):
            old = next(card for card in self.cards(self.context()) if card["memory_id"] == ident)
        self.assertFalse(old.get("summary"))
        refreshed = self.cards(self.context())
        self.assertIn(ident, [card["memory_id"] for card in refreshed])
        expanded = next(card for card in refreshed if card["memory_id"] == ident)
        self.assertGreater(len(expanded["summary"]), 80)
        self.assertNotIn(ident, self.context())

    def test_narrow_then_wide_pool_upgrades_once_and_never_downgrades(self):
        ident, _ = self.record()
        self.r.config["work_memory"]["max_context_chars"] = 300
        initial = next(card for card in self.cards(self.context()) if card["memory_id"] == ident)
        self.assertFalse(initial.get("summary"))
        self.r.config["work_memory"]["max_context_chars"] = 600
        refreshed = self.cards(self.context())
        self.assertIn(ident, [card["memory_id"] for card in refreshed])
        expanded = next(card for card in refreshed if card["memory_id"] == ident)
        self.assertGreater(len(expanded["summary"]), 80)
        for budget in (300, 600, 300, 600):
            self.r.config["work_memory"]["max_context_chars"] = budget
            self.assertNotIn(ident, self.context())

    def test_task_progress_write_does_not_repeat_the_expanded_card(self):
        ident, _ = self.record()
        self.assertIn(ident, self.context())
        task = self.r.task(self.p, "reader")
        updated = self.r.task_update(self.p, "reader", task["goal"], task["scope"], status="delivered")
        self.assertEqual(updated["revision"], task["revision"])
        self.assertNotIn(ident, self.context())

    def test_more_room_for_file_decoration_does_not_repeat_a_whole_conclusion(self):
        ident, _ = self.record(file=True)
        self.r.config["work_memory"]["max_context_chars"] = 360
        first = next(card for card in self.cards(self.context()) if card["memory_id"] == ident)
        self.assertGreater(len(first.get("summary", "")), 80)
        self.assertNotIn("files", first)
        self.r.config["work_memory"]["max_context_chars"] = 600
        self.assertNotIn(ident, self.context())
        (self.workspace / "memory.py").write_text("changed = True\n", encoding="utf-8")
        changed = next(card for card in self.cards(self.context()) if card["memory_id"] == ident)
        self.assertEqual(changed["reference_role"], "source_update")
        self.assertTrue(changed["file_state_changed"])
        self.assertNotIn("summary", changed)

    def test_unhealthy_or_blocked_exposure_is_not_a_conclusion_upgrade(self):
        ident, _ = self.record()
        original = self.r._work_item
        for number, risk in enumerate(({"state": "failed"}, {"state": "conflicted"},
                                        {"source_changed": True}, {"automatic_eligible": False})):
            with self.subTest(risk=risk):
                def unhealthy(*args, **kwargs):
                    return dict(original(*args, **kwargs), **risk)
                conversation = "risk-%d" % number
                with patch.object(self.r, "_work_item", side_effect=unhealthy):
                    items = self.r.work_context_items(self.p, conversation, "budgetfocus",
                        workspace=self.workspace, conclusion_max_chars=240)
                    self.assertIn(ident, [item["memory_id"] for item in items])
                    with self.r.transaction():
                        self.r.work_record_exposure(self.p, conversation, items)
                    self.assertEqual(self.r.work_context_items(self.p, conversation, "budgetfocus",
                        workspace=self.workspace, conclusion_max_chars=240), [])


if __name__ == "__main__":
    unittest.main()
