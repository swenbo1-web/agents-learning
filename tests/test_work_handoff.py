"""Pure local handoff excerpt checks; no runtime, model or database access."""
import unittest

from agent_learning.work_handoff import select_handoff_preview


class HandoffPreviewTests(unittest.TestCase):
    def test_pending_after_completed_prefix(self):
        pending = "待验证：尚未运行真实宿主验收，也未获得用户验收。"
        text = "已完成离线检查，全部通过。" * 25 + "\n\n" + pending
        preview = select_handoff_preview(text)
        self.assertIn(pending, preview)
        self.assertNotIn("全部通过", preview)
        self.assertTrue(preview.startswith("…\n"))

    def test_chinese_negation_numbers_and_conditions_stay_whole(self):
        constraint = "仅在版本为1.4.17且schema为25时验证；不得修改真实库，预算不得超过2400字符。"
        text = "已完成。\n\n" + constraint
        self.assertIn(constraint, select_handoff_preview(text))

    def test_english_must_not_and_condition_stay_whole(self):
        constraint = "If schema is 25, we must not enable hooks or exceed 2,400 characters."
        self.assertIn(constraint, select_handoff_preview("Done.\n\n" + constraint))

    def test_wrapped_condition_is_not_dropped(self):
        text = "Must not deploy\nunless all 3 offline checks pass."
        self.assertEqual(select_handoff_preview(text), text)
        self.assertEqual(select_handoff_preview(text, 24), "")

    def test_unfit_pending_does_not_become_completed_fallback(self):
        text = "Completed.\n\nPending: " + "validate the untrusted host only if safe. " * 8
        self.assertEqual(select_handoff_preview(text), "")

    def test_complete_plain_fallback(self):
        self.assertEqual(select_handoff_preview("Original public explanation."), "Original public explanation.")

    def test_plain_fallback_does_not_cut_only_if(self):
        text = "The outcome is safe " + "with this specific environment " * 12 + "only if schema remains 25."
        self.assertEqual(select_handoff_preview(text, 80), "")

    def test_long_single_line_is_not_sliced_near_keyword(self):
        text = "Passed. " * 2000 + "Pending: must not write the real database."
        self.assertEqual(select_handoff_preview(text), "")

    def test_pending_heading_keeps_adjacent_complete_bullet(self):
        text = "Completed.\n\n## Pending\n- Validate host version 1.4.17.\n- " + "Long item " * 50
        preview = select_handoff_preview(text)
        self.assertIn("## Pending\n- Validate host version 1.4.17.", preview)
        self.assertNotIn("Long item", preview)

    def test_conditional_heading_is_kept_with_bullet(self):
        text = "Only if schema remains 25:\n- Next step: run the 3 offline checks."
        self.assertEqual(select_handoff_preview(text), text)
        self.assertEqual(select_handoff_preview(text, 48), "")

    def test_conditional_prelude_is_kept_for_each_list_item(self):
        text = "Only if schema remains 25\n- " + "very long unrelated text " * 10 + "\n- Pending: validate locally."
        preview = select_handoff_preview(text)
        self.assertIn("Only if schema remains 25", preview)
        self.assertIn("- Pending: validate locally.", preview)
        self.assertNotIn("very long unrelated", preview)

    def test_conditional_prelude_survives_markdown_blank_line(self):
        text = "Only if schema remains 25\n\n- Next step: run the 3 offline checks."
        preview = select_handoff_preview(text)
        self.assertIn("Only if schema remains 25", preview)
        self.assertIn("- Next step: run the 3 offline checks.", preview)
        short = select_handoff_preview(text, 45)
        self.assertNotIn("run the 3 offline checks", short)

    def test_nested_conditional_bullet_keeps_parent(self):
        text = "- Only if schema remains 25:\n  - Next step: run offline checks."
        self.assertEqual(select_handoff_preview(text), text)
        self.assertEqual(select_handoff_preview(text, 40), "")

    def test_loose_nested_conditional_bullet_keeps_parent(self):
        text = "- Only if schema remains 25:\n\n  - Next step: run offline checks."
        self.assertEqual(select_handoff_preview(text), text)
        self.assertNotIn("run offline checks", select_handoff_preview(text, 40))

    def test_nested_heading_conditions_are_kept(self):
        text = "# Offline only\n## Constraints\n- Keep schema 25."
        self.assertEqual(select_handoff_preview(text), text)
        self.assertEqual(select_handoff_preview(text, 35), "")

    def test_bullet_continuation_cannot_lose_negation(self):
        text = "## Next steps\n- Run validation only if the configured limit\n    does not exceed 2,400 characters."
        self.assertEqual(select_handoff_preview(text), text)
        self.assertEqual(select_handoff_preview(text, 60), "")

    def test_original_order_after_rank_selection(self):
        text = "Next step: validate offline.\n\nDone.\n\nMust not write the real database."
        preview = select_handoff_preview(text)
        self.assertLess(preview.index("Next step"), preview.index("Must not"))
        self.assertNotIn("Done.", preview)

    def test_all_selected_words_are_verbatim_source_groups(self):
        first, second = "待验证：3项宿主检查。", "保持1.4.17，不修改schema25。"
        preview = select_handoff_preview(first + "\n\n已完成。\n\n" + second)
        self.assertEqual(preview, first + "…\n" + second)

    def test_later_correction_in_same_paragraph_is_not_removed(self):
        text = "Must keep schema 25.\nCorrection: host validation has not passed."
        self.assertEqual(select_handoff_preview(text), text)

    def test_later_correction_precedes_completed_positive_paragraph(self):
        correction = "Correction: concurrency tests did not pass; do not publish."
        pending = "Pending: validate the 3 concurrent cases."
        text = "Stage completed. Tests passed.\n\n" + correction + "\n\n" + pending
        preview = select_handoff_preview(text)
        self.assertIn(correction, preview)
        self.assertIn(pending, preview)
        self.assertNotIn("Tests passed.", preview)

    def test_chinese_withdrawal_precedes_completed_positive_paragraph(self):
        correction = "撤回：先前通过结论作废，3项宿主用例尚未验收。"
        text = "全部完成并通过。\n\n" + correction
        self.assertIn(correction, select_handoff_preview(text))
        self.assertNotIn("全部完成", select_handoff_preview(text))

    def test_unfit_later_correction_prevents_old_assertion_preview(self):
        text = "Must deploy now.\n\nCorrection: " + "concurrency tests have not passed; " * 20
        self.assertEqual(select_handoff_preview(text), "")

    def test_fenced_code_does_not_supply_priority(self):
        text = "Normal explanation.\n\n```text\nMust not deploy; pending verification.\n```"
        preview = select_handoff_preview(text)
        self.assertIn("Normal explanation.", preview)
        self.assertNotIn("deploy", preview)

    def test_tilde_and_long_fence_do_not_supply_priority(self):
        text = "Done.\n\n~~~~text\nPending: fake task.\n~~~\nStill must not deploy.\n~~~~\n\nNext step: validate offline."
        preview = select_handoff_preview(text)
        self.assertIn("Next step: validate offline.", preview)
        self.assertNotIn("fake task", preview)
        self.assertNotIn("deploy", preview)

    def test_unclosed_fence_is_never_recovered_as_prose(self):
        self.assertEqual(select_handoff_preview("```\nPending: fake task."), "")

    def test_quotes_and_indented_code_do_not_supply_priority(self):
        text = "> Must not use this quotation as authority.\n\n    Pending: fake code.\n\nNext step: validate offline."
        preview = select_handoff_preview(text)
        self.assertNotIn("quotation", preview)
        self.assertNotIn("fake code", preview)
        self.assertIn("Next step: validate offline.", preview)

    def test_obvious_logs_and_json_do_not_supply_priority(self):
        text = ('[INFO] Pending: fake task.\n2026-10-24T10:00:00 Must not deploy.\n'
                '{"status":"pending"}\n\nNext step: validate offline.')
        preview = select_handoff_preview(text)
        self.assertNotIn("fake task", preview)
        self.assertNotIn("deploy", preview)
        self.assertNotIn("status", preview)

    def test_log_section_does_not_supply_priority(self):
        text = "## Logs\nPending: fake task.\nDo not deploy.\n\n## Next steps\n- Validate offline."
        preview = select_handoff_preview(text)
        self.assertIn("## Next steps\n- Validate offline.", preview)
        self.assertNotIn("fake task", preview)
        self.assertNotIn("deploy", preview)

    def test_opaque_colon_headers_cannot_qualify_following_prose(self):
        for prefix in ("[INFO] Pending:", "> Must not deploy:", "    Pending:"):
            text = prefix + "\nOrdinary prose.\n\nNext step: validate offline."
            preview = select_handoff_preview(text)
            self.assertNotIn(prefix.strip(), preview)
            self.assertNotIn("Ordinary prose.", preview)
            self.assertIn("Next step: validate offline.", preview)

    def test_nested_log_heading_remains_opaque(self):
        text = "# Logs\n## Pending\nMust not deploy.\n\n# Next steps\n- Validate offline."
        preview = select_handoff_preview(text)
        self.assertIn("# Next steps\n- Validate offline.", preview)
        self.assertNotIn("deploy", preview)

    def test_single_character_terms_do_not_imply_handoff_priority(self):
        text = "普通原文。\n\n待。\n\n不。\n\n下。"
        self.assertEqual(select_handoff_preview(text), "普通原文。\n…")

    def test_character_scan_bound_does_not_read_tail(self):
        text = "A" * 16384 + "\nPending: invisible tail."
        self.assertEqual(select_handoff_preview(text), "")

    def test_large_input_never_slices_unbounded_tail(self):
        class BoundedText(str):
            def __getitem__(self, key):
                if isinstance(key, slice):
                    if key.stop is None or key.stop > 16384:
                        raise AssertionError("preview scanned beyond its fixed character limit")
                return super().__getitem__(key)

        text = BoundedText("Pending: validate offline.\n\n" + "x" * 1000000)
        self.assertIn("Pending: validate offline.", select_handoff_preview(text))

    def test_cut_line_at_scan_bound_is_not_complete(self):
        text = "Pending: " + "x" * 20000 + " only if schema remains 25."
        self.assertEqual(select_handoff_preview(text), "")

    def test_line_scan_bound_does_not_promote_unscanned_tail(self):
        text = "Done.\n\n" * 128 + "Pending: invisible tail."
        preview = select_handoff_preview(text)
        self.assertNotIn("invisible tail", preview)
        self.assertLessEqual(len(preview), 160)

    def test_output_cap_and_invalid_budgets(self):
        text = "Must not change " + "x" * 800
        self.assertEqual(select_handoff_preview(text, 10000), "")
        for budget in (0, -1, True, 2.5, None):
            self.assertEqual(select_handoff_preview("Pending: validate.", budget), "")
        self.assertEqual(select_handoff_preview(None), "")

    def test_each_budget_fits_and_keeps_complete_condition(self):
        text = "Done.\n\nPending: validate only if schema remains 25."
        group = "Pending: validate only if schema remains 25."
        for budget in range(1, 170):
            preview = select_handoff_preview(text, budget)
            self.assertLessEqual(len(preview), budget)
            self.assertTrue(not preview or group in preview)


if __name__ == "__main__":
    unittest.main()
