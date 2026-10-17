"""Automatic publication keeps evidence, feedback targets and source lineage distinct."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint, admin_once
from agent_learning.admin_governance import execute_governance, propose_governance
from agent_learning.knowledge_store import problem_feedback_target
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class AutoPublicationLineageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="alp-auto-lineage-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "project"
        self.workspace.mkdir()
        self.runtime = Runtime(Path(temporary.name) / "state")
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.runtime.config["admin"].update(enabled=True, command=[sys.executable],
                                            max_calls_per_day=100, auto_publish=True)
        self.sequence = 0

    def tool(self, conversation, outcome=0, tool="edit", command=None):
        self.sequence += 1
        target = self.workspace / "service.py"
        target.write_text(f"version = {self.sequence}\n", encoding="utf-8")
        common = {"session_id": conversation, "cwd": str(self.workspace),
                  "tool_use_id": f"tool-{self.sequence}", "tool_name": tool,
                  "tool_input": {"command": command} if command else {"file_path": str(target)}}
        self.runtime.handle_hook(dict(common, hook_event_name="PreToolUse"))
        self.runtime.handle_hook(dict(common, hook_event_name="PostToolUse",
                                      tool_response={"exit_code": outcome, "output": "observed result"}))
        return self.runtime.db.execute(
            "SELECT id FROM events WHERE project=? AND conversation=? AND type='PostToolUse' "
            "ORDER BY rowid DESC LIMIT 1", (self.project, conversation)).fetchone()[0]

    def problem(self, conversation="one", summary="source problem", event=None):
        event = event or self.tool(conversation)
        return self.runtime.feedback(self.project, conversation, summary, category="operation",
                                     resolution="Visible operation result", event_refs=[event])

    def candidate(self, problem, content="Version one", category="operation", kind="solution", refs=None):
        refs = refs or [row[0] for row in self.runtime.db.execute(
            "SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        return self.runtime.submit_candidate(self.project, problem_id=problem["id"],
            problem_revision=problem["revision"], title="service configuration", content=content,
            category=category, knowledge_kind=kind, applicability="Same observation",
            limitations="Observed tool result only", evidence_refs=refs, validation_level="tool_success")

    def review(self, problem, content="Version one", target=None, revision=None, refs=None, action="update"):
        base = fake_reviewer("candidate")

        class Reviewer(base):
            def communicate(inner, data=None, timeout=None):
                super().communicate(data, timeout)
                response = json.loads(inner.output.read_text(encoding="utf-8"))
                response["candidate"].update(title="service configuration", content=content,
                    validation_level="tool_success", knowledge_kind="solution")
                if refs is not None:
                    response["candidate"]["evidence_refs"] = refs
                if target:
                    response["proposal"] = {"action": action, "knowledge_id": target,
                                            "expected_revision": revision}
                inner.output.write_text(json.dumps(response), encoding="utf-8")

        with patch("agent_learning.admin.subprocess.Popen", Reviewer):
            return admin_once(self.runtime, self.project, problem["id"])

    def test_target_professional_and_pattern_require_explicit_governance(self):
        for index, (category, kind) in enumerate((("professional", "solution"),
                                                 ("operation", "pattern"),
                                                 ("professional", "pattern"))):
            with self.subTest(category=category, kind=kind):
                first = self.problem(f"source-{index}", f"original-{index}")
                candidate = self.candidate(first, category=category, kind=kind)
                knowledge = self.runtime.publish(self.project, candidate["candidate_id"])
                changed = self.problem(f"changed-{index}", f"later-{index}")
                result = self.review(changed, content=f"New operation {index}",
                                     target=knowledge["knowledge_id"], revision=1)
                self.assertEqual(result["publication"], {
                    "status": "not_published", "reason": "target knowledge requires trusted governance"})
                current = self.runtime.read(self.project, knowledge["knowledge_id"])
                self.assertEqual((current["revision"], current["category"], current["knowledge_kind"]),
                                 (1, category, kind))

    def test_publication_does_not_mutate_feedback_target_or_source_fingerprint(self):
        problem = self.problem()
        before = self.runtime.review_packet(self.project, problem["id"])
        knowledge = self.review(problem)["publication"]
        after = self.runtime.review_packet(self.project, problem["id"])
        row = self.runtime.db.execute("SELECT * FROM problems WHERE id=?", (problem["id"],)).fetchone()
        self.assertEqual((row["knowledge_id"], row["knowledge_revision"]), (None, None))
        self.assertEqual(_fingerprint(before), _fingerprint(after))
        self.assertIn(knowledge["knowledge_id"], [item["knowledge_id"] for item in after["related_knowledge"]])

    def test_old_source_case_can_update_current_revision_after_another_case(self):
        first = self.problem()
        kid = self.review(first)["publication"]["knowledge_id"]
        second = self.problem("two", "Different description")
        self.assertEqual(self.review(second, "Version two", kid, 1)["publication"]["revision"], 2)
        first = self.problem()
        result = self.review(first, "Version three", kid, 2)
        self.assertEqual(result["publication"]["revision"], 3)
        self.assertEqual(self.runtime.read(self.project, kid)["content"], "Version three")

    def test_legacy_publication_backfill_is_derived_not_an_explicit_target(self):
        first = self.problem()
        before = self.runtime.review_packet(self.project, first["id"])
        kid = self.review(first)["publication"]["knowledge_id"]
        # Simulate exactly the transient 8f54757 persisted source-case backfill.
        self.runtime.db.execute("UPDATE problems SET knowledge_id=?,knowledge_revision=1 WHERE id=?",
                                (kid, first["id"]))
        normalized = self.runtime.review_packet(self.project, first["id"])
        self.assertEqual(normalized["problem"]["knowledge_id"], None)
        self.assertEqual(_fingerprint(before), _fingerprint(normalized))
        second = self.problem("two", "Other source")
        self.assertEqual(self.review(second, "Version two", kid, 1)["publication"]["revision"], 2)
        first = self.problem()
        self.assertEqual(self.review(first, "Version three", kid, 2)["publication"]["revision"], 3)

    def test_counterexample_keeps_its_exact_historical_revision(self):
        first = self.problem()
        kid = self.review(first)["publication"]["knowledge_id"]
        counterexample = self.runtime.feedback(self.project, "counterexample", "Original revision failed",
            knowledge_id=kid, knowledge_revision=1, category="operation")
        event = self.tool("counterexample")
        counterexample = self.runtime.feedback(self.project, "counterexample", "Original revision failed",
            knowledge_id=kid, knowledge_revision=1, category="operation", event_refs=[event])
        body = self.candidate(counterexample, content="Manual revision two")
        self.runtime.publish(self.project, body["candidate_id"], knowledge_id=kid, expected_revision=1)
        self.runtime.db.execute("UPDATE problems SET knowledge_revision=2 WHERE id=?", (counterexample["id"],))
        current_case = self.runtime.db.execute("SELECT * FROM problems WHERE id=?", (counterexample["id"],)).fetchone()
        self.assertEqual(problem_feedback_target(self.runtime, self.project, current_case), (kid, 1))
        with self.assertRaisesRegex(ContractError, "target differs"):
            propose_governance(self.runtime, self.project, counterexample["id"], counterexample["revision"],
                "update", candidate_id=body["candidate_id"], knowledge_id=kid, expected_revision=2,
                reason="Cannot silently move a historical counterexample target")
        # A proposal written by the older implementation must be checked again
        # when executed, even though its original admission saw polluted columns.
        with patch("agent_learning.knowledge_store.problem_feedback_target", return_value=(kid, 2)):
            legacy = propose_governance(self.runtime, self.project, counterexample["id"], counterexample["revision"],
                "update", candidate_id=body["candidate_id"], knowledge_id=kid, expected_revision=2,
                reason="Legacy proposal saw incorrectly advanced feedback columns")
        with self.assertRaisesRegex(ContractError, "target differs"):
            execute_governance(self.runtime, self.project, legacy["proposal_id"])
        packet = self.runtime.review_packet(self.project, counterexample["id"])
        self.assertEqual(packet["problem"]["knowledge_revision"], 1)
        self.assertEqual(packet["target_knowledge"]["revision"], 1)
        self.assertEqual(packet["target_knowledge"]["active_revision"], 2)

    def test_explicit_observation_target_is_not_mistaken_for_backfill(self):
        first = self.problem()
        kid = self.review(first)["publication"]["knowledge_id"]
        event = self.tool("observed")
        observation = self.runtime.feedback(self.project, "observed", "Explicit target", category="operation",
            knowledge_id=kid, knowledge_revision=1, feedback_kind="observation", event_refs=[event])
        candidate = self.candidate(observation)
        self.runtime.publish(self.project, candidate["candidate_id"], knowledge_id=kid, expected_revision=1)
        self.runtime.db.execute("UPDATE problems SET knowledge_revision=2 WHERE id=?", (observation["id"],))
        row = self.runtime.db.execute("SELECT * FROM problems WHERE id=?", (observation["id"],)).fetchone()
        self.assertEqual(problem_feedback_target(self.runtime, self.project, row), (kid, 1))

    def test_update_must_cite_success_after_latest_failure(self):
        first = self.problem()
        old_success = self.runtime.db.execute(
            "SELECT id FROM events WHERE type='PostToolUse' AND outcome='succeeded'").fetchone()[0]
        kid = self.review(first)["publication"]["knowledge_id"]
        failed = self.tool("one", 1, "Bash", "python verify_service.py")
        first = self.problem(event=failed)
        recovered = self.tool("one", 0, "Bash", "python verify_service.py")
        first = self.problem(event=recovered)
        blocked = self.review(first, "Unsupported update", kid, 1, [old_success])
        self.assertEqual(blocked["publication"], {
            "status": "not_published", "reason": "requires current cited tool-success evidence"})
        self.assertEqual(self.runtime.read(self.project, kid)["revision"], 1)
        # A new observation invalidates the previous review; citing its actual
        # successful recovery allows the subsequent bounded update.
        recovered = self.tool("one", 0, "Bash", "python verify_service.py")
        first = self.problem(event=recovered)
        accepted = self.review(first, "Verified update", kid, 1, [recovered])
        self.assertEqual(accepted["publication"]["revision"], 2)

    def test_update_must_cite_success_at_or_after_latest_linked_write(self):
        first = self.problem()
        old_success = self.runtime.db.execute(
            "SELECT id FROM events WHERE type='PostToolUse' AND outcome='succeeded'").fetchone()[0]
        kid = self.review(first)["publication"]["knowledge_id"]
        latest_write = self.tool("one")
        first = self.problem(event=latest_write)
        blocked = self.review(first, "Stale success update", kid, 1, [old_success])
        self.assertEqual(blocked["publication"]["reason"], "requires current cited tool-success evidence")
        self.assertEqual(self.runtime.read(self.project, kid)["revision"], 1)

    def test_auto_publication_failure_rolls_back_all_business_writes(self):
        first = self.problem()
        kid = self.review(first)["publication"]["knowledge_id"]
        second = self.problem("two", "Later source")
        with patch.object(self.runtime, "_queue_sync", side_effect=ValueError("controlled sync failure")):
            result = self.review(second, "Version two", kid, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.runtime.read(self.project, kid)["revision"], 1)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0], 1)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
