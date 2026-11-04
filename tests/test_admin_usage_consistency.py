"""Usage consistency is conservative, idempotent and independent of review success."""
import json
import unittest

from agent_learning.admin import _usage
from agent_learning.admin_lifecycle import finish_call
from agent_learning.admin_usage import normalize_usage
from agent_learning.runtime import ContractError
from tests import test_module8_budget as fixtures


class CacheUsageNormalizationTests(unittest.TestCase):
    def usage(self, **totals):
        return {"token_totals": {"input_tokens": 100, "output_tokens": 20, **totals}, "completeness": "complete"}

    def test_cache_is_an_optional_subset_not_an_extra_token_debit(self):
        for value in (self.usage(), self.usage(cached_input_tokens=0), self.usage(cached_input_tokens=100)):
            with self.subTest(value=value):
                normalized = normalize_usage(value)
                self.assertEqual(normalized["completeness"], "complete")
                self.assertFalse(normalized.get("conflict", False))

    def test_oversized_cache_is_preserved_but_not_complete(self):
        normalized = normalize_usage(self.usage(cached_input_tokens=150))
        self.assertEqual(normalized["token_totals"], {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 150})
        self.assertEqual(normalized["completeness"], "partial")
        self.assertTrue(normalized["conflict"])
        self.assertIn("cached_input_exceeds_input", normalized["consistency_errors"])

    def test_per_turn_conflict_survives_an_otherwise_valid_aggregate_and_renormalization(self):
        value = {"token_usage": [{"input_tokens": 1, "cached_input_tokens": 10, "output_tokens": 1},
                                 {"input_tokens": 99, "cached_input_tokens": 0, "output_tokens": 19}],
                 "token_totals": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20},
                 "completeness": "complete"}
        normalized = normalize_usage(value)
        self.assertEqual(normalized["completeness"], "partial")
        self.assertIn("turn_cached_input_exceeds_input", normalized["consistency_errors"])
        snapshot = {key: val for key, val in normalized.items() if key != "token_usage"}
        self.assertEqual(normalize_usage(snapshot), snapshot)

    def test_full_record_totals_mismatch_is_not_hidden_by_explicit_complete(self):
        value = dict(self.usage(), token_usage=[{"input_tokens": 90, "output_tokens": 20}])
        self.assertIn("token_totals_records_mismatch", normalize_usage(value)["consistency_errors"])

    def test_truncated_preview_is_not_required_to_equal_all_turn_totals(self):
        value = dict(self.usage(cached_input_tokens=60), preview_truncated=True,
                     token_usage=[{"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 10}])
        normalized = normalize_usage(value)
        self.assertEqual(normalized["completeness"], "complete")
        self.assertFalse(normalized.get("conflict", False))

    def test_missing_input_in_a_reported_turn_cannot_validate_its_cache(self):
        normalized = normalize_usage(dict(self.usage(cached_input_tokens=50),
            token_usage=[{"cached_input_tokens": 50, "output_tokens": 20}]))
        self.assertEqual(normalized["completeness"], "partial")
        self.assertEqual(normalized["token_totals"]["input_tokens"], 100)

    def test_record_total_disagreement_is_not_canceled_by_another_record(self):
        value = {"token_usage": [{"input_tokens": 5, "output_tokens": 5, "total_tokens": 12},
                                 {"input_tokens": 5, "output_tokens": 5, "total_tokens": 8}]}
        normalized = normalize_usage(value)
        self.assertEqual(normalized["completeness"], "partial")
        self.assertIn("turn_total_tokens_mismatch", normalized["consistency_errors"])

    def test_consistency_codes_are_bounded_and_cannot_be_arbitrary_text(self):
        for value in ("cached_input_exceeds_input", ["free-form provider text"], [True],
                      ["cached_input_exceeds_input"] * 20):
            with self.subTest(value=value), self.assertRaises(ContractError):
                normalize_usage(dict(self.usage(), consistency_errors=value))


class UsageLedgerConsistencyTests(unittest.TestCase):
    setUp = fixtures.Module8BudgetTests.setUp
    claim = fixtures.Module8BudgetTests.claim
    start = fixtures.Module8BudgetTests.start
    status = fixtures.Module8BudgetTests.status

    def begin(self):
        call = self.claim()
        self.start(call)
        return call

    def finish(self, call, usage, *, state="skip"):
        row = finish_call(self.r, call["call_id"], state, usage)
        return json.loads(row["usage"])

    def bad(self):
        return {"token_totals": {"input_tokens": 100, "cached_input_tokens": 150, "output_tokens": 20},
                "completeness": "complete"}

    def test_finish_records_known_tokens_once_and_keeps_conflict_reservation(self):
        call = self.begin()
        for _ in range(3):
            usage = self.finish(call, self.bad())
        self.assertEqual(usage["completeness"], "partial")
        self.assertTrue(usage["conflict"])
        status = self.status()
        self.assertEqual(status["reported_tokens"], 120)
        self.assertEqual(status["accounted_tokens"], 4000)
        self.assertEqual(status["usage_states"]["conflict"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 1)

    def test_empty_or_later_valid_report_cannot_clear_the_original_conflict(self):
        call = self.begin()
        self.finish(call, self.bad())
        self.finish(call, {"token_usage": []})
        valid = {"token_totals": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20},
                 "completeness": "complete", "consistency_errors": [], "conflict": False}
        usage = self.finish(call, valid)
        self.assertEqual(usage["completeness"], "partial")
        self.assertTrue(usage["conflict"])
        self.assertIn("cached_input_exceeds_input", usage["consistency_errors"])
        self.assertEqual(self.status()["reported_tokens"], 120)
        self.assertEqual(self.status()["usage_states"]["conflict"], 1)

    def test_empty_replay_reclassifies_legacy_invalid_complete_usage_without_clearing_numbers(self):
        call = self.begin()
        # Reproduce an old version's accepted inconsistency in this isolated fixture.
        self.r.db.execute("UPDATE admin_calls SET usage=? WHERE id=?", (json.dumps(self.bad()), call["call_id"]))
        self.r.db.execute("UPDATE admin_call_budgets SET reported_tokens=120,completeness='complete' WHERE call_id=?", (call["call_id"],))
        usage = self.finish(call, {"token_totals": {}})
        self.assertTrue(usage["conflict"])
        self.assertEqual(self.status()["usage_states"]["conflict"], 1)
        self.assertEqual(self.status()["accounted_tokens"], 4000)

    def test_empty_observation_cannot_clear_a_ledger_only_historical_conflict(self):
        call = self.begin()
        self.finish(call, {"token_usage": [{"input_tokens": 100, "output_tokens": 20}]})
        self.r.db.execute("UPDATE admin_call_budgets SET conflict=1,completeness='partial' WHERE call_id=?", (call["call_id"],))
        usage = self.finish(call, {"token_usage": [], "conflict": False, "consistency_errors": []})
        self.assertTrue(usage["conflict"])
        self.assertEqual(usage["completeness"], "partial")
        self.assertEqual(self.status()["reported_tokens"], 120)
        self.assertEqual(self.status()["accounted_tokens"], 4000)

    def test_no_reported_numbers_remain_unknown_despite_consistency_flag(self):
        call = self.begin()
        usage = self.finish(call, {"token_totals": {}, "consistency_errors": ["turn_cached_input_exceeds_input"]})
        self.assertTrue(usage["conflict"])
        self.assertEqual(usage["completeness"], "unknown")
        self.assertEqual(self.status()["usage_states"]["unknown"], 1)
        self.assertEqual(self.status()["reported_tokens"], 0)
        self.assertEqual(self.status()["accounted_tokens"], 4000)

    def test_hidden_bad_turn_is_retained_after_sixteen_record_preview(self):
        log = self.home.parent / "synthetic-stream.jsonl"
        bad = {"input_tokens": 1, "cached_input_tokens": 10, "output_tokens": 1}
        good = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 2}
        events = [{"type": "turn.completed", "turn_id": str(n), "usage": bad if n == 0 else good}
                  for n in range(18)]
        log.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        observed = _usage(log, 0.1)
        self.assertTrue(observed["preview_truncated"])
        self.assertEqual(len(observed["token_usage"]), 16)
        self.assertLess(observed["token_totals"]["cached_input_tokens"], observed["token_totals"]["input_tokens"])
        call = self.begin()
        usage = self.finish(call, observed)
        self.assertEqual(usage["completeness"], "partial")
        self.assertIn("turn_cached_input_exceeds_input", usage["consistency_errors"])
        self.assertEqual(self.status()["reported_tokens"], 1736)

    def test_oversized_database_integer_still_rolls_back_atomically(self):
        call = self.begin()
        with self.assertRaises(OverflowError):
            self.finish(call, {"token_totals": {"input_tokens": 10 ** 400, "output_tokens": 1}, "completeness": "complete"})
        row = self.r.db.execute("SELECT state,usage FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()
        self.assertEqual(row["state"], "started")
        self.assertIsNone(row["usage"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_usage_observations").fetchone()[0], 0)
        self.assertEqual(self.status()["accounted_tokens"], 4000)


if __name__ == "__main__":
    unittest.main()
