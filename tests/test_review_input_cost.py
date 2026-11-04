"""Local character snapshots never become evidence, tokens, or new model work."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint
from agent_learning.admin_decisions import dependency_signature, review_signature
from agent_learning.phase_metrics import batch_metrics
from agent_learning.phase_review import PHASE_PREFIX, instruction, response_schema
from agent_learning.review_input_cost import cost_from_packet, input_cost_snapshot, validate_input_cost
from agent_learning.review_packet import review_instruction
from agent_learning.runtime import encode
from tests.test_review_transport import batch, case
from tests import test_phase_metrics as fixtures


class InputCostProjectionTests(unittest.TestCase):
    def assert_matches(self, packet, *, prefix="", schema=None):
        frozen = deepcopy(packet)
        result = input_cost_snapshot(packet, outer_prefix=prefix, schema=schema)
        visible = prefix + review_instruction(packet)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["instruction_chars"], len(visible))
        self.assertEqual(result["instruction_bytes"], len(visible.encode("utf-8")))
        self.assertEqual(sum(result["components"].values()), len(visible))
        self.assertEqual(packet, frozen)
        self.assertNotIn("input_tokens", result)
        self.assertFalse(result["provider_overhead_included"])
        self.assertEqual(result["schema_chars"], len(encode(schema)) if schema is not None else None)
        self.assertEqual(validate_input_cost(result), result)
        return result

    def test_plain_chinese_payload_preserves_exact_unicode_and_json_character_counts(self):
        packet = case()
        packet["events"][0]["payload"] = {"text": "中文😀\n\"\\value", "related_knowledge": "business-only"}
        result = self.assert_matches(packet)
        self.assertGreater(result["instruction_bytes"], result["instruction_chars"])
        from agent_learning.review_evidence import project_packet
        self.assertEqual(result["components"]["source_events"], len(encode(project_packet(packet)["events"])))
        self.assertEqual(result["components"]["related_knowledge"], 2)  # Only structural [] is a comparison.
        self.assertFalse(result["transport_applied"])
        self.assertEqual(result["transport_saved_chars"], 0)

    def test_phase_compaction_and_short_references_are_counted_after_actual_projection(self):
        result = self.assert_matches(batch(), prefix=PHASE_PREFIX, schema=response_schema())
        self.assertTrue(result["transport_applied"])
        self.assertGreater(result["transport_saved_chars"], 128)
        self.assertGreater(result["components"]["shared_context"], 0)
        self.assertGreater(result["schema_chars"], 0)
        self.assertEqual(result["instruction_chars"], len(instruction(batch())))

    def test_raw_reference_mode_and_reserved_collision_do_not_claim_transport_saving(self):
        packet = batch()
        packet["items"][0]["events"][0]["payload"]["review_transport"] = {"a": "literal business data"}
        from agent_learning.review_evidence import raw_packet
        result = self.assert_matches(raw_packet(packet), prefix=PHASE_PREFIX)
        self.assertFalse(result["transport_applied"])
        self.assertEqual(result["transport_saved_chars"], 0)

    def test_history_and_coverage_guidance_use_the_actual_instruction(self):
        packet = case()
        packet.update(purpose="history_review", related_history=[{"events": [{"id": "h", "payload": "历史"}]}],
                      public_delivery_coverage={"complete": False, "read_required": True})
        result = self.assert_matches(packet)
        self.assertGreater(result["components"]["related_history"], 0)
        self.assertEqual(result["components"]["shared_context"], 0)

    def test_local_snapshot_does_not_change_instruction_fingerprint_or_review_signature(self):
        packet = batch()
        original = deepcopy(packet)
        cfg = {"enabled": True, "max_input_chars": 40000}
        before = (instruction(packet), _fingerprint(packet), dependency_signature(packet),
                  review_signature(packet, cfg, _fingerprint(packet)))
        cost = input_cost_snapshot(packet, outer_prefix=PHASE_PREFIX, schema=response_schema())
        packet["_local_admission"] = {"version": 1, "input_cost": cost}
        self.assertEqual((instruction(packet), _fingerprint(packet), dependency_signature(packet),
                          review_signature(packet, cfg, _fingerprint(packet))), before)
        self.assertNotIn("input_cost", instruction(packet))
        self.assertEqual(original["items"], packet["items"])
        self.assertEqual(cost_from_packet(packet), cost)

    def test_flat_deep_recursive_and_oversized_inputs_fail_closed_without_throwing(self):
        values = [None, {"payload": "x" * 1_000_001}, {"items": [0] * 100001}]
        nested = value = {}
        for _ in range(70):
            value["nested"] = {}
            value = value["nested"]
        values.append(nested)
        cycle = {}
        cycle["cycle"] = cycle
        values.append(cycle)
        values.append({"events": [{"payload": "\ud800"}]})
        for packet in values:
            with self.subTest(kind=type(packet).__name__):
                self.assertEqual(input_cost_snapshot(packet)["status"], "unknown")

    def test_wide_container_is_consumed_only_until_the_node_guard(self):
        from agent_learning.review_input_cost import _bounded

        class CountedList(list):
            visited = 0

            def __iter__(self):
                for value in super().__iter__():
                    self.visited += 1
                    if self.visited > 16:
                        raise AssertionError("wide list was prefetched before node limit")
                    yield value

        values = CountedList([0] * 1000)
        with patch("agent_learning.review_input_cost._MAX_NODES", 8):
            self.assertFalse(_bounded({"items": values}))
        self.assertLessEqual(values.visited, 8)

    def test_oversized_dictionary_key_is_rejected_before_serialization(self):
        packet = {"k" * 1_000_001: 0}
        with patch("agent_learning.runtime.encode", side_effect=AssertionError("oversized key encoded")) as encode_value:
            self.assertEqual(input_cost_snapshot(packet),
                             {"status": "unknown", "unit": "characters", "reason": "projection_limit"})
            encode_value.assert_not_called()

    def test_old_packets_and_corrupt_snapshots_are_unknown_not_zero_or_reconstructed(self):
        self.assertEqual(cost_from_packet({})["reason"], "not_recorded")
        self.assertEqual(cost_from_packet(None)["reason"], "packet_unavailable")
        base = input_cost_snapshot(case())
        for key, value in (("version", True), ("instruction_chars", True), ("instruction_bytes", -1),
                           ("instruction_bytes", 10 ** 100), ("scope", "reconstructed"),
                           ("transport_saved_chars", 8), ("transport_applied", "true"),
                           ("schema_chars", 4), ("provider_overhead_included", True)):
            with self.subTest(key=key):
                self.assertEqual(validate_input_cost({**base, key: value})["status"], "unknown")
        changed = deepcopy(base)
        changed["components"]["source_events"] += 1
        self.assertEqual(validate_input_cost(changed)["reason"], "invalid_snapshot")
        changed = deepcopy(base)
        changed["components"]["secret_field"] = "not returned"
        self.assertEqual(validate_input_cost(changed)["status"], "unknown")
        base["raw_prompt"] = "do not expose arbitrary stored fields"
        self.assertNotIn("raw_prompt", validate_input_cost(base))


class InputCostMetricsTests(unittest.TestCase):
    setUp = fixtures.PhaseMetricsTests.setUp
    phase = fixtures.PhaseMetricsTests.phase
    review = fixtures.PhaseMetricsTests.review
    usage = fixtures.PhaseMetricsTests.usage

    def test_new_frozen_batches_have_exact_snapshot_and_settings_never_reconstruct(self):
        identity, packet = self.review()
        expected = packet["_local_admission"]["input_cost"]
        self.assertEqual(expected["status"], "recorded")
        self.assertEqual(expected["instruction_chars"], len(instruction(packet)))
        before = self.r.db.total_changes
        with patch("agent_learning.review_packet._instruction_parts", side_effect=AssertionError("hot-path rendering")), \
             patch("agent_learning.admin.subprocess.Popen") as model:
            self.assertEqual(batch_metrics(self.r, self.p, identity)["input_cost"], expected)
            model.assert_not_called()
        self.assertEqual(self.r.db.total_changes, before)

    def test_legacy_missing_snapshot_stays_unknown_when_current_renderer_changes(self):
        identity, packet = self.review()
        packet["_local_admission"].pop("input_cost")
        self.r.db.execute("UPDATE phase_review_batches SET packet=? WHERE problem_id=?", (json.dumps(packet), identity))
        before = self.r.db.total_changes
        with patch("agent_learning.review_packet._instruction_parts", side_effect=AssertionError("reconstruction")):
            self.assertEqual(batch_metrics(self.r, self.p, identity)["input_cost"],
                             {"status": "unknown", "unit": "characters", "reason": "not_recorded"})
        self.assertEqual(self.r.db.total_changes, before)


if __name__ == "__main__":
    unittest.main()
