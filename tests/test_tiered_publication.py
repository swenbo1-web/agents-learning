"""Tiered references retain provenance; disputed knowledge cannot re-enter automatically."""
import copy
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_control import apply_control, control_status
from agent_learning.admin_governance import execute_governance, propose_governance
from agent_learning.admin_review_flow import publication_policy
from agent_learning.history_review_control import request_history_review, run_history_review
from agent_learning.runtime import ContractError, Runtime
from tests import test_admin_review_flow as flow_fixtures
from tests.test_module6_queue import fake_reviewer


class TieredPublicationTests(unittest.TestCase):
    setUp = flow_fixtures.ReviewFlowTests.setUp
    control = flow_fixtures.ReviewFlowTests.control
    history = flow_fixtures.ReviewFlowTests.history
    event = flow_fixtures.ReviewFlowTests.event

    def enable(self, **values):
        return apply_control(self.r, self.p, self.control(auto_publish=True, publication_mode="tiered", **values))

    def fact(self, summary="SQLite Atlas project configuration"):
        return self.r.feedback(self.p, "root", summary, category="project")

    def reviewer(self, *, category="project", kind="fact", title="SQLite Atlas", content="Project store uses SQLite",
                 callback=None, batch=False):
        base = fake_reviewer("candidate", callback=callback)

        class Reviewer(base):
            def communicate(inner, data=None, timeout=None):
                super().communicate(data, timeout)
                response = json.loads(inner.output.read_text(encoding="utf-8"))
                response["candidate"].update(title=title, content=content, category=category, knowledge_kind=kind,
                    applicability="This project and the cited source version only",
                    limitations="Sourced statement; not verified against current execution")
                if batch:
                    first = response["candidate"]
                    response.update(candidate=None, candidates=[first, dict(first, title="Archived source scope",
                        content="This applies only to the archived source version")])
                inner.output.write_text(json.dumps(response), encoding="utf-8")

        return Reviewer

    def review(self, problem, **values):
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(**values)):
            return admin_once(self.r, self.p, problem["id"])

    def test_first_explicit_enable_uses_tiered_default_across_hosts(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r = self.r if host == "codex" else Runtime(self.r.home, config=copy.deepcopy(self.config), host=host)
                if r is not self.r:
                    self.addCleanup(r.close)
                state = control_status(r, self.p, details=False)
                self.assertTrue(state["publication_policy"]["first_enable_recommended"])
                body = self.control(request_id=host)
                body.update(expected_policy_version=state["policy"]["policy_version"],
                    expected_settings_version=state["settings_version"], expected_session_epoch=state["session_epoch"])
                with patch("subprocess.Popen") as model:
                    apply_control(r, self.p, body, details=False)
                model.assert_not_called()
                self.assertEqual((publication_policy(r, self.p)["enabled"], publication_policy(r, self.p)["mode"]),
                                 (True, "tiered"))

    def test_explicit_off_and_legacy_on_choices_are_preserved(self):
        apply_control(self.r, self.p, self.control(auto_publish=False))
        apply_control(self.r, self.p, self.control(request_id="enable-again"))
        self.assertFalse(publication_policy(self.r, self.p)["enabled"])
        apply_control(self.r, self.p, self.control(request_id="legacy-on", auto_publish=True))
        self.assertEqual(publication_policy(self.r, self.p)["mode"], "verified_operation_solutions")
        apply_control(self.r, self.p, self.control(request_id="preserve-legacy"))
        self.assertEqual(publication_policy(self.r, self.p)["mode"], "verified_operation_solutions")

    def test_nullable_mode_migration_preserves_existing_publication_choice(self):
        apply_control(self.r, self.p, self.control(auto_publish=False))
        self.r.db.execute("ALTER TABLE admin_control_state DROP COLUMN publication_mode")
        other = Runtime(self.r.home, config=copy.deepcopy(self.config))
        self.addCleanup(other.close)
        self.assertFalse(publication_policy(other, self.p)["enabled"])
        self.assertEqual(publication_policy(other, self.p)["mode"], "verified_operation_solutions")

    def test_reported_project_fact_publishes_as_reference_without_an_extra_model_call(self):
        self.enable()
        result = self.review(self.fact())
        self.assertEqual(result["publication"]["status"], "published", result)
        item = self.r.read(self.p, result["publication"]["knowledge_id"])
        self.assertEqual(item["validation_level"], "reported")
        self.assertEqual(item["knowledge_tier"], "source_reference")
        self.assertTrue(item["sources"])
        self.assertFalse(item["current_tool_success"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_unverified_daily_solutions_and_professional_claims_do_not_auto_publish(self):
        self.enable()
        for index, (category, kind) in enumerate((("professional", "fact"), ("operation", "solution"),
                                                 ("operation", "fact"), ("project", "pattern"))):
            with self.subTest(category=category, kind=kind):
                result = self.review(self.fact(f"Project observation {index}"), category=category, kind=kind,
                                     title=f"Separate observation {index}")
                self.assertEqual(result["publication"]["reason"], "requires sourced project fact", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_failed_tool_cannot_be_relabelled_into_an_automatic_project_fact(self):
        self.enable()
        problem = self.r._problem(self.p, "root", "failed tool record", "operation", [self.event()["id"]])
        result = self.review(problem)
        self.assertEqual(result["publication"]["reason"], "reference source is unhealthy")

    def test_policy_changed_during_paid_review_retains_candidate_without_publication(self):
        self.enable()
        def change(_packet, _reviewer):
            apply_control(self.r, self.p, self.control(request_id="narrow", auto_publish=True,
                                                       publication_mode="verified_operation_solutions"))
        result = self.review(self.fact(), callback=change)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["publication"]["reason"], "publication_mode_changed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)

    def test_exact_reference_duplicate_attaches_provenance_without_creating_a_revision(self):
        self.enable()
        first = self.review(self.fact())
        second = self.review(self.fact("Second SQLite Atlas source"))
        self.assertEqual(second["publication"].get("deduplicated"), True, second)
        self.assertEqual(first["publication"]["knowledge_id"], second["publication"]["knowledge_id"])
        self.assertEqual(len(self.r.read(self.p, first["publication"]["knowledge_id"])["sources"]), 2)

    def test_counterexample_blocks_automatic_injection_and_equivalent_new_id(self):
        self.enable()
        first = self.review(self.fact())
        kid = first["publication"]["knowledge_id"]
        self.r.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "reader", "cwd": str(self.workspace),
                            "prompt": "SQLite Atlas project configuration"})
        self.r.feedback(self.p, "counterexample", "SQLite Atlas statement is wrong", category="project",
                        knowledge_id=kid, knowledge_revision=1, feedback_kind="counterexample")
        notice = self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": "reader", "cwd": str(self.workspace),
            "tool_name": "Bash", "tool_use_id": "read-notice", "tool_input": {"command": "echo status"}})
        self.assertIn("未解决反例", str(notice))
        self.assertEqual(self.r.search(self.p, "SQLite Atlas", _automatic=True)["items"], [])
        self.assertGreater(self.r.read(self.p, kid)["pending_counterexample_count"], 0)
        self.assertEqual(len(self.r.search(self.p, "SQLite Atlas")["items"]), 1)
        for event in ("UserPromptSubmit", "SessionStart"):
            result = self.r.handle_hook({"hook_event_name": event, "session_id": "fresh-"+event,
                "cwd": str(self.workspace), "source": "compact", "prompt": "SQLite Atlas project configuration"})
            self.assertNotIn(kid, str(result))
        second = self.review(self.fact("New SQLite Atlas source after disputed statement"))
        self.assertEqual(second["publication"]["reason"], "unresolved knowledge counterexample")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT disposition FROM knowledge_feedback").fetchone()[0], "unresolved")

    def test_revoked_reference_cannot_be_republished_by_exact_deduplication(self):
        self.enable()
        kid = self.review(self.fact())["publication"]["knowledge_id"]
        self.r.revoke(self.p, kid, expected_revision=1)
        result = self.review(self.fact("Another SQLite Atlas source"))
        self.assertEqual(result["publication"]["reason"], "equivalent knowledge was revoked")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_related_dispute_is_visible_to_review_and_blocks_a_renamed_candidate(self):
        self.enable()
        kid = self.review(self.fact())["publication"]["knowledge_id"]
        self.r.feedback(self.p, "counterexample", "SQLite Atlas is incorrect", knowledge_id=kid,
                        knowledge_revision=1, category="project", feedback_kind="counterexample")
        self.task = self.r.task_update(self.p, "root", "SQLite Atlas")
        problem = self.fact("SQLite Atlas")
        packet = self.r.review_packet(self.p, problem["id"])
        related = next(item for item in packet["related_knowledge"] if item["knowledge_id"] == kid)
        self.assertEqual(related["pending_counterexample_count"], 1)
        self.assertEqual(related["knowledge_tier"], "source_reference")
        result = self.review(problem, title="Renamed Atlas configuration", content="New alleged correction")
        self.assertEqual(result["publication"]["reason"], "unresolved knowledge counterexample")

    def test_same_title_different_content_is_held_for_comparison(self):
        self.enable()
        kid = self.review(self.fact())["publication"]["knowledge_id"]
        result = self.review(self.fact("SQLite Atlas now uses another database"), content="Project store uses a different database")
        self.assertEqual(result["publication"]["reason"], "conflicting knowledge requires review", result)
        self.assertEqual(self.r.read(self.p, kid)["revision"], 1)

    def test_history_remains_manual_without_explicit_batch_permission(self):
        self.enable()
        request = self.history()
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer()):
            result = self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1, max_seconds=60)
        self.assertFalse(result["job"]["publication_automatic"])
        self.assertEqual(result["job"]["counts"]["published_candidates"], 0)
        self.assertEqual(result["job"]["counts"]["candidates"], 1)

    def test_explicit_history_permission_publishes_each_sourced_reference(self):
        self.enable()
        request = self.history()
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(batch=True)), \
                patch("agent_learning.history_sources.scan_sources", side_effect=AssertionError("expanded sources")):
            result = self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1,
                                        max_seconds=60, auto_reference=True)
        counts = result["job"]["counts"]
        self.assertEqual((counts["candidates"], counts["published_candidates"], counts["calls"]), (2, 2, 1), result)
        for row in self.r.db.execute("SELECT id FROM active"):
            self.assertEqual(self.r.read(self.p, row[0])["knowledge_tier"], "source_reference")
        self.assertTrue(result["job"]["publication_automatic"])
        self.assertEqual(len(result["job"]["last_result"]["reviews"][0]["publications"]), 2)

    def test_history_permission_requires_enabled_tiered_policy_and_review(self):
        request = self.history()
        with patch("subprocess.Popen") as model:
            with self.assertRaises(ContractError):
                self.r.history_run(self.p, request["job_id"], allow_review=True, auto_reference=True)
            with self.assertRaises(ContractError):
                self.r.history_run(self.p, request["job_id"], auto_reference=True)
        model.assert_not_called()

    def test_history_request_freezes_permission_and_replays_receipt_without_launch(self):
        self.enable()
        body = dict(self.history(), auto_reference=True)
        with patch.object(self.r, "_launch_background") as launch:
            first = request_history_review(self.r, self.p, body, details=False)
            self.assertEqual(first, request_history_review(self.r, self.p, body, details=False))
        launch.assert_called_once()
        self.assertTrue(first["history_review"]["publication_automatic"])
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer()):
            result = run_history_review(self.r, self.p, body["request_id"])
        self.assertEqual((result["status"], result["counts"]["published_candidates"]), ("completed", 1), result)
        with patch("subprocess.Popen") as model:
            self.assertEqual(run_history_review(self.r, self.p, body["request_id"])["status"], "not_started")
        model.assert_not_called()

    def test_user_pause_during_history_review_rolls_back_automatic_publication(self):
        self.enable()
        request = self.history()
        def pause(_packet, _reviewer):
            self.r.history_pause(self.p, request["job_id"])
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(callback=pause)):
            self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1, max_seconds=60, auto_reference=True)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_invalid_publication_mode_does_not_mutate_settings(self):
        with self.assertRaises(ContractError):
            apply_control(self.r, self.p, self.control(publication_mode={}))
        self.assertEqual(control_status(self.r, self.p, details=False)["settings_version"], 0)

    def test_explicit_history_case_cannot_bypass_per_batch_permission(self):
        self.enable()
        request = self.history()
        problem = self.r.db.execute("SELECT problem_id FROM history_case_links WHERE job_id=?", (request["job_id"],)).fetchone()[0]
        with patch("subprocess.Popen") as model:
            result = admin_once(self.r, self.p, problem)
        self.assertEqual(result["status"], "idle")
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_history_reference_permission_does_not_carry_into_the_next_run(self):
        self.enable()
        (self.workspace / "NOTES.md").write_text("# Atlas notes\nSQLite is the stored source.\n", encoding="utf-8")
        request = self.history()
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer()):
            self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1, max_seconds=60, auto_reference=True)
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(title="Other source fact")):
            result = self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1, max_seconds=60)
        self.assertFalse(result["job"]["publication_automatic"])
        self.assertEqual(result["job"]["counts"]["candidates"], 2)
        self.assertEqual(result["job"]["counts"]["published_candidates"], 1)

    def test_history_batch_keeps_each_candidate_publication_result(self):
        self.enable()
        self.review(self.fact())
        request = self.history()
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(batch=True, content="Changed source statement")):
            result = self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1,
                                        max_seconds=60, auto_reference=True)
        self.assertEqual(result["job"]["counts"]["published_candidates"], 1, result)
        receipt = result["job"]["last_result"]["reviews"][0]
        self.assertEqual(receipt["publication"]["status"], "partially_published")
        self.assertEqual([item["status"] for item in receipt["publications"]], ["not_published", "published"])

    def test_disabling_during_history_review_keeps_reference_pending(self):
        self.enable()
        request = self.history()
        def disable(_packet, _reviewer):
            apply_control(self.r, self.p, self.control(request_id="off", auto_publish=False))
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(callback=disable)):
            result = self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1,
                                        max_seconds=60, auto_reference=True)
        self.assertEqual(result["job"]["counts"]["candidates"], 1)
        receipt = result["job"]["last_result"]["reviews"][0]
        self.assertEqual(receipt["publication"]["reason"], "automatic_publication_disabled")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_changed_history_run_cannot_reuse_the_old_publication_grant(self):
        self.enable()
        request = self.history()
        def change_owner(_packet, _reviewer):
            self.r.db.execute("UPDATE history_jobs SET owner='different-run',last_result='{}' WHERE id=?", (request["job_id"],))
        with patch("agent_learning.admin.subprocess.Popen", self.reviewer(callback=change_owner)):
            self.r.history_run(self.p, request["job_id"], allow_review=True, max_calls=1,
                               max_seconds=60, auto_reference=True)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_correcting_disputed_knowledge_requires_explicit_governance_and_preserves_history(self):
        self.enable()
        kid = self.review(self.fact())["publication"]["knowledge_id"]
        problem = self.r.feedback(self.p, "root", "SQLite Atlas needs correction", category="project",
            knowledge_id=kid, knowledge_revision=1, feedback_kind="counterexample")
        self.assertEqual(self.r.search(self.p, "SQLite Atlas", _automatic=True)["items"], [])
        # This is an explicit trusted governance action, not an automatic review.
        evidence = self.r.record_event(self.p, "root", "Feedback", {"summary": "Source checked; the corrected store is SQLite2",
            "feedback_kind": "observation"}, outcome="reported", task_id=self.task["id"], task_revision=self.task["revision"])
        problem = self.r._problem(self.p, "root", "SQLite Atlas needs correction", "project", [evidence["id"]], kid,
                                   knowledge_revision=1)
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="SQLite Atlas", content="Corrected store is SQLite2", category="project", knowledge_kind="fact",
            applicability="This project and corrected source", limitations="Sourced and still unverified",
            evidence_refs=[evidence["id"]], validation_level="reported")
        proposal = propose_governance(self.r, self.p, problem["id"], problem["revision"], "update",
            candidate_id=candidate["candidate_id"], knowledge_id=kid, expected_revision=1,
            evidence_refs=[evidence["id"]], reason="Explicit correction of the wrong sourced statement")
        result = execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(result["publication"]["revision"], 2)
        self.assertEqual(self.r.db.execute("SELECT disposition FROM knowledge_feedback").fetchone()[0], "revised")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge WHERE id=?", (kid,)).fetchone()[0], 2)
        self.assertEqual(self.r.read(self.p, kid)["knowledge_tier"], "source_reference")
        self.assertEqual(len(self.r.search(self.p, "SQLite Atlas", _automatic=True)["items"]), 1)


if __name__ == "__main__":
    unittest.main()
