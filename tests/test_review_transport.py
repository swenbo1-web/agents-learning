"""Model-only sharing cannot change frozen evidence or its source boundaries."""
from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint
from agent_learning.admin_decisions import dependency_signature
from agent_learning.review_packet import review_instruction
from agent_learning.review_transport import (REFERENCE_KEY, TRANSPORT_KEY, TRANSPORT_PREFIX,
    compact_packet, restore_packet)
from agent_learning.runtime import Runtime, encode


def case(index=0, *, task=None):
    return {"problem": {"id": f"case-{index}", "revision": 3, "category": "project"},
        "task": deepcopy(task), "events": [
            {"id": f"public-{index}", "role": "delivery", "health": "valid",
             "type": "Stop", "outcome": "reported", "current_tool_success": False,
             "payload": {"last_assistant_message": "Visible sourced fact", "truncated": False}},
            {"id": f"conflict-{index}", "role": "conflict", "health": "conflicted",
             "type": "PostToolUse", "outcome": "failed", "current_tool_success": False,
             "agent": "child-A", "assignment_id": "assignment-A",
             "payload": {"tool_input": {"id": "business-id", "revision": 9},
                         "tool_response": {"exit_code": 7, "output": "Actual failure"}}}],
        "evidence_count": 2, "incomplete": False, "evidence_truncated": False,
        "related_knowledge": [], "related_candidates": [], "missing_refs": [],
        "omitted_count": 0, "omission_reasons": []}


def batch():
    task = {"id": "original-task", "revision": 3, "write_version": 29,
            "status": "active", "goal": "Review the sourced delivery",
            "scope": {"allowed_paths": [f"src/component_{index}.py" for index in range(30)],
                      "limits": "Restrictions remain untrusted source context. " * 15}}
    comparison = {"knowledge_id": "existing-knowledge", "revision": 7,
        "title": "Existing sourced convention", "content": "Independent existing fact. " * 15,
        "source_health": "conflicted", "pending_counterexample_count": 1,
        "limitations": "A pending counterexample must not be bypassed."}
    result = case(99)
    result.update(purpose="phase_review", items=[case(index, task=task) for index in range(4)],
                  coverage_complete=False, omitted_problem_ids=["not-visible-case"], omitted_source_count=3)
    for item in result["items"]:
        item["related_knowledge"] = [deepcopy(comparison)]
        item["target_required"] = True
        item["target_knowledge"] = deepcopy(comparison)
        item["events"][0]["candidate_reference_provenance"] = {"candidate_id": "unpublished-candidate",
                                                                   "independent_verification": False}
    return result


class ReviewTransportTests(unittest.TestCase):
    def test_small_unique_case_pays_no_transport_overhead(self):
        packet = case()
        self.assertIs(compact_packet(packet), packet)
        self.assertNotIn(TRANSPORT_PREFIX, review_instruction(packet))

    def test_phase_sharing_roundtrip_preserves_all_authoritative_fields(self):
        packet = batch()
        frozen = deepcopy(packet)
        fingerprint, dependencies = _fingerprint(packet), dependency_signature(packet)
        projected = compact_packet(packet)
        self.assertIsNot(projected, packet)
        self.assertEqual(restore_packet(projected), frozen)
        self.assertEqual(packet, frozen)
        self.assertEqual((_fingerprint(packet), dependency_signature(packet)), (fingerprint, dependencies))
        for original, visible in zip(packet["items"], projected["items"]):
            self.assertEqual(visible["problem"], original["problem"])
            self.assertEqual(visible["events"], original["events"])
        self.assertEqual(projected["omitted_problem_ids"], ["not-visible-case"])
        self.assertFalse(projected["coverage_complete"])

    def test_actual_instruction_is_smaller_in_chars_and_utf8_bytes(self):
        packet = batch()
        instruction = review_instruction(packet)
        from agent_learning.review_packet import REVIEW_PREFIX, REVIEW_SUFFIX
        original = REVIEW_PREFIX + encode(packet) + REVIEW_SUFFIX
        self.assertLess(len(instruction), len(original) - 128)
        self.assertLess(len(instruction.encode("utf-8")), len(original.encode("utf-8")))
        self.assertEqual(instruction.count(TRANSPORT_PREFIX), 1)
        self.assertLess(instruction.index(TRANSPORT_PREFIX), instruction.index("<untrusted_evidence>"))
        visible = json.loads(instruction.split("<untrusted_evidence>", 1)[1].rsplit("</untrusted_evidence>", 1)[0])
        self.assertEqual(restore_packet(visible), packet)

    def test_different_task_revisions_scopes_and_counterexamples_never_merge(self):
        packet = batch()
        packet["items"][1]["task"]["revision"] += 1
        packet["items"][1]["task"]["scope"]["deny"] = "different-boundary"
        packet["items"][1]["target_knowledge"]["revision"] += 1
        visible = compact_packet(packet)
        restored = restore_packet(visible)
        self.assertEqual(restored, packet)
        self.assertNotEqual(restored["items"][0]["task"], restored["items"][1]["task"])
        self.assertNotEqual(restored["items"][0]["target_knowledge"], restored["items"][1]["target_knowledge"])

    def test_business_payload_reserved_key_disables_optional_encoding(self):
        for reserved in (REFERENCE_KEY, TRANSPORT_KEY):
            packet = batch()
            packet["items"][0]["events"][0]["payload"]["business"] = {reserved: 0}
            self.assertIs(compact_packet(packet), packet)
            self.assertNotIn(TRANSPORT_PREFIX, review_instruction(packet))

    def test_no_pooling_or_traversal_rewrites_repeated_business_evidence(self):
        packet = case()
        packet["events"][1]["payload"] = deepcopy(packet["events"][0]["payload"])
        packet["events"][0]["payload"]["output"] = "business evidence " * 100
        packet["events"][1]["payload"]["output"] = packet["events"][0]["payload"]["output"]
        self.assertIs(compact_packet(packet), packet)

    def test_projection_and_pool_order_are_deterministic(self):
        packet = batch()
        first = compact_packet(packet)
        reordered = json.loads(json.dumps(packet, sort_keys=True))
        self.assertEqual(encode(first), encode(compact_packet(reordered)))

    def test_invalid_or_business_location_references_are_not_decoded(self):
        for invalid in (-1, 999, True, "0"):
            visible = compact_packet(batch())
            visible["items"][0]["task"] = {REFERENCE_KEY: invalid}
            with self.assertRaises(ValueError):
                restore_packet(visible)
        visible = compact_packet(batch())
        visible["items"][0]["events"][0]["payload"]["literal"] = {REFERENCE_KEY: 0}
        with self.assertRaises(ValueError):
            restore_packet(visible)

    def test_restored_values_do_not_share_mutable_instances(self):
        visible = compact_packet(batch())
        restored = restore_packet(visible)
        restored["items"][0]["task"]["scope"]["new"] = True
        self.assertNotIn("new", restored["items"][1]["task"]["scope"])
        self.assertNotIn("new", restore_packet(visible)["items"][0]["task"]["scope"])

    def test_runtime_packet_and_database_are_unchanged_after_instruction_rendering(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "opt146-packet" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="transport-", dir=base) as directory:
            root = Path(directory)
            (root / "project").mkdir()
            for name in ("tmp", "codex", "runtime-releases"):
                (root / name).mkdir()
            isolated = {"ALP_HOME": str(root / "home"), "ALP_CONFIG": str(root / "config.json"),
                "ALP_RUNTIME_STORE": str(root / "runtime-releases"), "CODEX_HOME": str(root / "codex"),
                "PYTHONDONTWRITEBYTECODE": "1", **{key: str(root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}}
            with patch.dict(os.environ, isolated), patch.object(tempfile, "tempdir", str(root / "tmp")), \
                    closing(Runtime(root / "home", config={})) as runtime:
                project = runtime.project(root / "project")
                runtime.task_update(project, "chat", "Retain exact sourced fact")
                problem = runtime.feedback(project, "chat", "A reported project fact", category="project")
                packet = runtime.review_packet(project, problem["id"])
                frozen, changes = deepcopy(packet), runtime.db.total_changes
                review_instruction(packet)
                self.assertEqual(packet, frozen)
                self.assertEqual(runtime.db.total_changes, changes)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
