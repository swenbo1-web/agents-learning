"""Schema 17 to 18 fences executors, while preserving current account observations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent, mark_started
from agent_learning.admin_queue import eligible_query
from agent_learning.admin_usage import admin_project_budget_status, reserve_usage
from agent_learning.runtime import ContractError, Runtime


class Schema18BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.project = self.r.project(self.workspace)
        self.r.config["admin"].update(enabled=True, command=[sys.executable])
        self.r.config["work_memory"]["summary_enabled"] = True

    def upgrade(self, *, host="codex", config=None):
        self.r.db.execute("PRAGMA user_version=17")
        with patch("subprocess.Popen", side_effect=AssertionError("migration must not launch a model")):
            upgraded = Runtime(self.home, config=config if config is not None else copy.deepcopy(self.r.config), host=host)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 19)
        return upgraded

    def test_first_and_current_models_survive_upgrade_without_rebinding_to_next_conversation(self):
        self.r.admin_observe_model(self.project, "first-root", "original-first-model")
        self.r.admin_observe_model(self.project, "later-root", "later-current-model")
        self.r.admin_configure(self.project, True, model_mode="first")
        task = self.r.task_update(self.project, "first-root", "Preserve this task")
        before = dict(self.r.db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
        first = self.r.admin_first_model_binding(self.project)
        old_epoch = self.r.session_epoch()
        upgraded = self.upgrade()
        self.assertGreater(upgraded.session_epoch(), old_epoch)
        self.assertEqual(upgraded.resolve_admin_executor(self.project)["model"], "original-first-model")
        carried = upgraded.admin_first_model_binding(self.project)
        self.assertEqual({key: carried[key] for key in ("model", "conversation", "observed", "version")},
                         {key: first[key] for key in ("model", "conversation", "observed", "version")})
        upgraded.admin_observe_model(self.project, "after-upgrade", "different-new-model")
        self.assertEqual(upgraded.resolve_admin_executor(self.project)["model"], "original-first-model")
        upgraded.admin_configure(self.project, True, model_mode="current")
        self.assertEqual(upgraded.resolve_admin_executor(self.project, "later-root")["model"], "later-current-model")
        self.assertEqual(dict(upgraded.db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()), before)
        epoch = upgraded.session_epoch()
        reopened = Runtime(self.home, config=copy.deepcopy(upgraded.config))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.session_epoch(), epoch)
        self.assertEqual(reopened.admin_first_model_binding(self.project)["model"], "original-first-model")

    def test_each_host_carries_only_its_current_account_not_an_older_account(self):
        self.r.admin_observe_model(self.project, "old-account-root", "old-account-model")
        self.r.scope_account_changed()
        self.r.admin_observe_model(self.project, "current-account-root", "current-account-model")
        self.r.admin_configure(self.project, True, model_mode="first")
        native = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(native.close)
        native.admin_observe_model(self.project, native.host_conversation("native-root"), "native-first-model")
        native.admin_configure(self.project, True, model_mode="first")
        # Make the native account epoch differ from the governance/codex epoch,
        # as happens when only one of several logged-in hosts switches accounts.
        native.db.execute("UPDATE host_scopes SET epoch=0 WHERE scope='antigravity:default'")
        native.db.execute("UPDATE admin_first_models SET session_epoch=0 WHERE host_scope='antigravity:default'")
        native.db.execute("UPDATE admin_model_observations SET session_epoch=0 WHERE host_scope='antigravity:default'")
        upgraded = self.upgrade()
        self.assertEqual(upgraded.resolve_admin_executor(self.project)["model"], "current-account-model")
        observation = upgraded.db.execute("SELECT session_epoch FROM admin_model_observations WHERE conversation='old-account-root'").fetchone()
        self.assertNotEqual(observation[0], upgraded.session_epoch())
        with patch("subprocess.Popen", side_effect=AssertionError("migration must not launch a model")):
            upgraded_native = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(upgraded_native.close)
        self.assertEqual(upgraded_native.resolve_admin_executor(self.project)["model"], "native-first-model")

    def test_account_with_only_historical_observations_does_not_regain_a_model(self):
        self.r.admin_observe_model(self.project, "old-account-root", "historical-only-model")
        self.r.admin_configure(self.project, True, model_mode="first")
        self.r.scope_account_changed()
        upgraded = self.upgrade()
        self.assertIsNone(upgraded.admin_first_model_binding(self.project))
        with self.assertRaisesRegex(ContractError, "administrator_first_model_unavailable"):
            upgraded.resolve_admin_executor(self.project)

    def test_inactive_model_observation_stays_inactive_after_upgrade(self):
        self.r.admin_observe_model(self.project, "closed-root", "inactive-model")
        self.r.admin_observe_model(self.project, "closed-root", active=False)
        self.r.admin_configure(self.project, True, model_mode="current")
        upgraded = self.upgrade()
        row = upgraded.db.execute("SELECT session_epoch,active FROM admin_model_observations WHERE conversation='closed-root'").fetchone()
        self.assertEqual(tuple(row), (upgraded.session_epoch(), 0))
        with self.assertRaisesRegex(ContractError, "administrator_current_model_unavailable"):
            upgraded.resolve_admin_executor(self.project, "closed-root")

    def test_known_prelaunch_reservation_is_released_and_other_active_calls_stay_unknown(self):
        now = time.time()
        states = [("known-reserved", "reserved", "owner", None, None),
                  ("intent", "launch_intent", "owner", now, None),
                  ("started", "started", "owner", now, now),
                  ("legacy-reserved", "reserved", None, None, None)]
        with self.r.transaction():
            for call_id, state, owner, intent, started in states:
                self.r.db.execute(
                    "INSERT INTO admin_calls(id,project,problem,revision,day,state,created,host_epoch,owner,lease_until,launch_intent,started) "
                    "VALUES(?,?,?,1,?,?,?,?,?,?,?,?)",
                    (call_id, self.project, "case-" + call_id, time.strftime("%Y-%m-%d", time.gmtime()), state,
                     now, self.r.host_epoch(), owner, now + 100, intent, started))
                reserve_usage(self.r, call_id)
        upgraded = self.upgrade()
        records = {row["id"]: dict(row) for row in upgraded.db.execute("SELECT * FROM admin_calls")}
        self.assertEqual(records["known-reserved"]["state"], "not_started")
        for call_id in ("intent", "started", "legacy-reserved"):
            self.assertEqual(records[call_id]["state"], "orphaned_unknown")
            self.assertEqual(records[call_id]["retry_allowed"], 0)
            self.assertIsNone(records[call_id]["lease_until"])
        usage = admin_project_budget_status(upgraded, self.project)
        self.assertEqual((usage["counted_attempts"], usage["result_unknown_attempts"],
                          usage["not_started_attempts"], usage["accounted_tokens"]), (3, 3, 1, 12000))

    def summary(self, conversation="summary-root"):
        task = self.r.task_update(self.project, conversation, "Collect this stage")
        for index in range(2):
            with self.r.transaction():
                raw = {"tool_name": "write_file", "tool_input": {"path": "src/file" + str(index) + ".py"},
                       "tool_response": {"exit_code": 0, "output": "Updated project source " + str(index)}}
                event, _ = self.r._event(self.project, conversation, "PostToolUse", raw, outcome="succeeded",
                                         task_id=task["id"], task_revision=task["revision"],
                                         task_write_version=task["write_version"], execution_epoch=task["execution_epoch"])
                self.r._work_capture_event(self.project, event["id"], self.workspace, raw=raw)
        result = self.r._work_queue_summary(self.project, conversation, self.workspace, expected_epoch=self.r.host_epoch())
        self.assertEqual(result["status"], "pending", result)
        return result["problem_id"]

    def test_pending_summary_keeps_its_identity_sources_and_failed_review_lock(self):
        problem = self.summary()
        with self.r.transaction():
            call = claim_call(self.r, self.project, problem, 1, "failed-summary", self.r.host_epoch(), 30)
        mark_launch_intent(self.r, call["call_id"], call["owner"], call["host_epoch"])
        mark_started(self.r, call["call_id"], call["owner"])
        finish_call(self.r, call["call_id"], "failed")
        self.r.db.execute("UPDATE work_summary_links SET call_id=? WHERE problem_id=?", (call["call_id"], problem))
        before = dict(self.r.db.execute("SELECT * FROM work_summary_links WHERE problem_id=?", (problem,)).fetchone())
        upgraded = self.upgrade()
        after = dict(upgraded.db.execute("SELECT * FROM work_summary_links WHERE problem_id=?", (problem,)).fetchone())
        for key in before.keys() - {"session_epoch", "governance_epoch"}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual((after["session_epoch"], after["governance_epoch"]),
                         (upgraded.session_epoch(), upgraded.host_epoch()))
        self.assertEqual(upgraded.work_summary_guard(self.project, problem)["problem_id"], problem)
        query, args = eligible_query("unchanged", problem, include_cached=True, summary_enabled=True,
                                    summary_host_scope=upgraded.host_scope)
        self.assertEqual(upgraded.db.execute(query, (self.project, *args)).fetchall(), [])
        record = upgraded.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (call["call_id"],)).fetchone()
        self.assertEqual(tuple(record), ("failed", 0))

    def test_awaiting_summary_continues_waiting_instead_of_becoming_new_work(self):
        problem = self.summary()
        self.r.db.execute("UPDATE work_summary_links SET state='awaiting_evidence' WHERE problem_id=?", (problem,))
        upgraded = self.upgrade()
        result = upgraded._work_queue_summary(self.project, "summary-root", self.workspace,
                                              expected_epoch=upgraded.host_epoch())
        self.assertEqual((result["status"], result["problem_id"]), ("awaiting_evidence", problem))
        self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM work_summary_links").fetchone()[0], 1)

    def test_summary_from_old_account_or_governance_epoch_is_not_revived(self):
        first = self.summary("old-account-summary")
        second = self.summary("old-governance-summary")
        old_session, old_governance = self.r.session_epoch(), self.r.host_epoch()
        self.r.db.execute("UPDATE host_scopes SET epoch=epoch+1 WHERE scope=?", (self.r.host_scope,))
        self.r.db.execute("UPDATE work_summary_links SET session_epoch=? WHERE problem_id=?", (old_session + 1, second))
        self.r.db.execute("UPDATE host_state SET epoch=epoch+1 WHERE id=1")
        upgraded = self.upgrade()
        one = upgraded.db.execute("SELECT session_epoch,governance_epoch FROM work_summary_links WHERE problem_id=?", (first,)).fetchone()
        two = upgraded.db.execute("SELECT session_epoch,governance_epoch FROM work_summary_links WHERE problem_id=?", (second,)).fetchone()
        self.assertEqual(tuple(one), (old_session, old_governance))
        self.assertEqual(tuple(two), (old_session + 1, old_governance))


if __name__ == "__main__":
    unittest.main()
