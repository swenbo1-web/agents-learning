"""Final envelope growth must not leave invisible paid review members behind."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_usage import estimate_reservation
from agent_learning.phase_review import configure, policy, prepare
from tests import test_phase_review as fixtures


class PhaseFinalAdmissionTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def final_capacity(self, maximum_items, *, omitted_limit=None):
        """Inject a final-only envelope cost after the parent gains its source.

        The candidate selection parent has revision zero; the final parent has
        its PhaseReviewRequested source and revision one. This deterministically
        exercises the same final admission branch as newly appended diagnostics.
        """
        def estimate(runtime, project, text, *, phase=False):
            result = estimate_reservation(runtime, project, text, phase=phase)
            if phase:
                from agent_learning.review_transport import restore_packet
                packet = restore_packet(json.loads(text.split("<untrusted_evidence>", 1)[1]
                                                    .rsplit("</untrusted_evidence>", 1)[0]))
                final_oversize = len(packet["items"]) > maximum_items
                final_oversize |= bool(omitted_limit is not None and packet["items"]
                                       and packet.get("omitted_problem_count", 0) > omitted_limit)
                if packet["problem"]["revision"] and final_oversize:
                    result = dict(result, admitted=False, reason="phase_input_token_budget_exceeded")
            return result
        return patch("agent_learning.admin_usage.estimate_reservation", side_effect=estimate)

    def pending(self):
        row = self.r.db.execute("SELECT problem_id,packet FROM phase_review_batches WHERE state='pending'").fetchone()
        return (row["problem_id"], json.loads(row["packet"])) if row else None

    def test_final_two_to_one_tracks_only_visible_member_and_revisits_removed_case(self):
        self.phase("first", "First independently sourced project interface")
        with self.final_capacity(1), patch("agent_learning.admin.subprocess.Popen") as model:
            self.phase("second", "Second independent deployment convention")
        model.assert_not_called()
        identity, body = self.pending()
        visible = {item["problem"]["id"] for item in body["items"]}
        self.assertEqual(len(visible), 1)
        members = {row[0] for row in self.r.db.execute(
            "SELECT problem_id FROM phase_review_members WHERE batch_id=? AND state='pending'", (identity,))}
        self.assertEqual(members, visible)
        all_cases = {row[0] for row in self.r.db.execute(
            "SELECT id FROM problems WHERE conversation IN ('first','second')")}
        removed = all_cases - visible
        self.assertEqual(len(removed), 1)
        self.assertFalse(self.r.db.execute(
            "SELECT 1 FROM phase_review_members WHERE batch_id=? AND problem_id=?",
            (identity, next(iter(removed)))).fetchone())
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as model:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(model.call_count, 1)
        next_batch = self.pending()
        self.assertIsNotNone(next_batch)
        self.assertEqual({item["problem"]["id"] for item in next_batch[1]["items"]}, removed)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as model:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2)

    def test_unsupported_duplicate_diagnostics_are_inside_final_admission(self):
        configure(self.r, self.p, {"every_phases": 3}, policy(self.r, self.p)["version"])
        # Defer selection so all stages start with the same unchecked cursor;
        # their actual creation order then determines the supporting members.
        with patch("agent_learning.phase_review.prepare"):
            self.phase("first", "First independent project contract")
            self.phase("second", "Second repeated convention")
            self.phase("duplicate", "Second repeated convention")
        # Removing the second visible item also removes the support for its
        # duplicate. Its new omission diagnostic must be counted before the
        # final size check, rather than after an apparently admissible request.
        with self.final_capacity(1, omitted_limit=1), patch("agent_learning.admin.subprocess.Popen") as model:
            with self.r.transaction():
                prepare(self.r, self.p)
        model.assert_not_called()
        self.assertIsNone(self.pending())
        self.assertEqual(self.r.db.execute(
            "SELECT COUNT(*) FROM phase_review_members WHERE state IN ('pending','duplicate_pending')").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertGreater(self.r.db.execute(
            "SELECT COUNT(*) FROM phase_review_batches WHERE state='no_ready_material'").fetchone()[0], 0)

    def test_removed_duplicate_support_requires_matching_comparison_dependency(self):
        configure(self.r, self.p, {"every_phases": 3}, policy(self.r, self.p)["version"])
        fact = "The same reported fact requires its own comparison context"
        with patch("agent_learning.phase_review.prepare"):
            self.phase("first", fact)
            self.phase("second", fact)
            self.phase("duplicate", fact)

        def dependency(packet, evidence_signature=None):
            # Identical business observations can have different comparison
            # dependencies. Only the removed second item supports the third.
            return "comparison-A" if packet["problem"]["conversation"] == "first" else "comparison-B"

        with patch("agent_learning.admin_decisions.dependency_signature", side_effect=dependency), \
                self.final_capacity(1), patch("agent_learning.admin.subprocess.Popen") as model:
            with self.r.transaction():
                prepare(self.r, self.p)
        model.assert_not_called()
        identity, body = self.pending()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["problem"]["conversation"], "first")
        self.assertEqual(self.r.db.execute(
            "SELECT COUNT(*) FROM phase_review_members WHERE batch_id=? AND state='duplicate_pending'",
            (identity,)).fetchone()[0], 0)
        self.assertEqual(body["omitted_problem_count"], 2)
        self.assertEqual(set(body["omitted_problem_ids"]), {row[0] for row in self.r.db.execute(
            "SELECT id FROM problems WHERE conversation IN ('second','duplicate')")})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_final_one_to_zero_has_no_pending_members_or_model_call_and_can_recover(self):
        configure(self.r, self.p, {"every_phases": 1}, policy(self.r, self.p)["version"])
        with self.final_capacity(0), patch("agent_learning.admin.subprocess.Popen") as model:
            self.phase("only", "A small independently sourced project fact")
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertIsNone(self.pending())
        self.assertEqual(self.r.db.execute(
            "SELECT COUNT(*) FROM phase_review_members WHERE state='pending'").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertGreater(self.r.db.execute(
            "SELECT COUNT(*) FROM phase_review_batches WHERE state='no_ready_material'").fetchone()[0], 0)
        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        ready = self.pending()
        self.assertIsNotNone(ready)
        self.assertEqual(len(ready[1]["items"]), 1)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fixtures.reviewer("skip")) as model:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.assertEqual(model.call_count, 1)


if __name__ == "__main__":
    unittest.main()
