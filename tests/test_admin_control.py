"""Settings capability, admission pause, receipts and read-only HTTP status."""
import http.client
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_control import apply_control, control_status, request_receipt
from agent_learning.admin_usage import admin_budget_policy
from agent_learning.knowledge_browser import make_browser_server
from agent_learning.runtime import Runtime, ContractError
from tests.test_module6_queue import fake_reviewer


class AdminControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-admin-control-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.home = root / "home"
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)
        self.r.admin_observe_model(self.p, "root-chat", "observed-model-v1")
        self.r.task_update(self.p, "root-chat", "Project work")
        self.problem = self.r.feedback(self.p, "root-chat", "A documented project observation")

    def body(self, **values):
        status = control_status(self.r, self.p)
        result = {"request_id": "control-test", "expected_policy_version": status["policy"]["policy_version"],
            "expected_budget_version": status["budget_policy"]["version"], "expected_settings_version": status["settings_version"],
            "expected_session_epoch": status["session_epoch"], "expected_governance_epoch": status["governance_epoch"],
            "enabled": True, "model_mode": "first", "model": "",
            "limits": {key: status["budget_policy"]["values"][key] for key in
                       ("max_calls_per_day", "max_calls_per_batch", "max_batch_seconds", "max_run_seconds")}, "start": False}
        result.update(values)
        return result

    def test_settings_bind_first_and_do_not_call_models_or_launch_without_start(self):
        with patch.object(self.r, "_launch_admin") as launch, patch("agent_learning.admin.subprocess.Popen") as model:
            response = apply_control(self.r, self.p, self.body())
        model.assert_not_called()
        launch.assert_not_called()
        self.assertTrue(response["settings"]["enabled"])
        self.assertEqual(response["settings"]["routing"]["executor"]["model"], "observed-model-v1")
        self.assertEqual(response["settings"]["budget_policy"]["values"]["max_calls_per_day"], 100)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_status_reports_effective_admission_without_starting_or_changing_settings(self):
        self.assertFalse(self.r.status(self.p)["admin_admission_enabled"])
        apply_control(self.r, self.p, self.body())
        pause = self.body(request_id="pause-for-status", enabled=False)
        apply_control(self.r, self.p, pause)
        before = self.r.db.total_changes
        with patch.object(self.r, "_launch_admin") as launch, patch("agent_learning.admin.subprocess.Popen") as model:
            paused = self.r.status(self.p)
        self.assertTrue(paused["admin_enabled"], "legacy policy remains enabled for in-flight results")
        self.assertFalse(paused["admin_admission_enabled"])
        self.assertEqual(self.r.db.total_changes, before, "status must not mutate the runtime")
        launch.assert_not_called()
        model.assert_not_called()
        apply_control(self.r, self.p, self.body(request_id="resume-for-status"))
        self.assertTrue(self.r.status(self.p)["admin_admission_enabled"])

    def test_history_budget_defaults_are_bounded_and_do_not_enable_review(self):
        from agent_learning.cli import parser
        args = parser().parse_args(["history-run", "chosen-job", "--confirm"])
        self.assertEqual((args.max_calls, args.max_seconds, args.allow_review), (10, 600, False))
        explicit = parser().parse_args(["history-run", "chosen-job", "--confirm", "--allow-review", "--max-calls", "2", "--max-seconds", "180"])
        self.assertEqual((explicit.max_calls, explicit.max_seconds), (2, 180))

    def test_explicit_summary_choice_is_project_host_scoped_versioned_and_idempotent(self):
        body = self.body(summary_enabled=True)
        with patch("subprocess.Popen") as model:
            response = apply_control(self.r, self.p, body)
            self.assertEqual(apply_control(self.r, self.p, body), response)
        self.assertTrue(response["settings"]["work_summary"]["enabled"])
        self.assertFalse(self.r.config["work_memory"]["summary_enabled"])
        self.assertEqual(self.r.status(self.p)["work_summary_policy"], response["settings"]["work_summary"])
        self.assertEqual(self.r.status(self.p)["admin_dispatch_diagnostic"]["reason"], "ready")
        model.assert_not_called()
        other = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        self.assertFalse(other.work_summary_enabled(self.p))

    def test_first_explicit_false_summary_choice_advances_control_version(self):
        initial = apply_control(self.r, self.p, self.body())
        before = initial["settings"]["settings_version"]
        response = apply_control(self.r, self.p, self.body(request_id="explicit-summary-off", summary_enabled=False))
        self.assertEqual(response["settings"]["settings_version"], before + 1)
        self.assertTrue(response["settings"]["work_summary"]["configured"])

    def test_start_attempt_is_once_and_receipt_replays_exactly(self):
        body = self.body(start=True)
        with patch.object(self.r, "_launch_admin") as launch:
            first = apply_control(self.r, self.p, body)
            repeated = apply_control(self.r, self.p, body)
        self.assertEqual(first, repeated)
        launch.assert_called_once()
        self.assertEqual(first["launch"]["status"], "accepted")
        self.assertEqual(request_receipt(self.r, self.p, body["request_id"])["response"], first)
        with self.assertRaisesRegex(ContractError, "reused"):
            apply_control(self.r, self.p, dict(body, enabled=False))

    def test_stale_versions_and_epoch_never_change_settings(self):
        stale = self.body()
        apply_control(self.r, self.p, dict(stale, request_id="newer"))
        with self.assertRaisesRegex(ContractError, "settings changed"):
            apply_control(self.r, self.p, stale)
        fresh = self.body(request_id="before-account-change")
        self.r.account_changed()
        with self.assertRaisesRegex(ContractError, "context changed"):
            apply_control(self.r, self.p, fresh)

    def test_disable_only_pauses_new_admission_current_result_commits(self):
        apply_control(self.r, self.p, self.body())
        version = self.r.admin_policy(self.p)["policy_version"]
        def pause(packet, process):
            response = apply_control(self.r, self.p, self.body(request_id="pause", enabled=False))
            self.assertFalse(response["settings"]["enabled"])
            self.assertEqual(response["settings"]["runner"]["state"], "stopping")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate", pause)) as model:
            result = admin_once(self.r, self.p)
            following = admin_once(self.r, self.p)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(following["status"], "disabled")
        self.assertEqual(self.r.admin_policy(self.p)["policy_version"], version)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_model_switch_during_call_waits_and_preserves_current_result(self):
        apply_control(self.r, self.p, self.body())
        def switch(packet, process):
            with self.assertRaisesRegex(ContractError, "wait for"):
                apply_control(self.r, self.p, self.body(request_id="switch", model_mode="fixed", model="new-model"))
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate", switch)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(self.r.resolve_admin_executor(self.p)["model"], "observed-model-v1")

    def test_budget_update_during_call_does_not_invalidate_paid_result(self):
        apply_control(self.r, self.p, self.body())
        def update(packet, process):
            body = self.body(request_id="budget-change")
            body["limits"]["max_calls_per_day"] = 0
            apply_control(self.r, self.p, body)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate", update)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(control_status(self.r, self.p)["usage"]["remaining_calls"], 0)

    def test_pause_between_outer_check_and_claim_prevents_new_model_call(self):
        apply_control(self.r, self.p, self.body())
        from agent_learning.admin import _select_review
        def paused(runtime, *args, **kwargs):
            runtime.db.execute("UPDATE admin_control_state SET enabled=0,version=version+1 WHERE project=? AND host_scope=?", (self.p, runtime._session_scope()))
            return _select_review(runtime, *args, **kwargs)
        with patch("agent_learning.admin._select_review", side_effect=paused), patch("subprocess.Popen") as model:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        model.assert_not_called()

    def test_pause_after_reservation_but_before_launch_refunds_unstarted_call(self):
        apply_control(self.r, self.p, self.body())
        from agent_learning.admin_providers import build_execution
        def paused(*args, **kwargs):
            apply_control(self.r, self.p, self.body(request_id="pause-before-launch", enabled=False))
            return build_execution(*args, **kwargs)
        with patch("agent_learning.admin_providers.build_execution", side_effect=paused), patch("subprocess.Popen") as model:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.r.db.execute("SELECT state FROM admin_calls").fetchone()[0], "not_started")
        self.assertEqual(control_status(self.r, self.p)["usage"]["used_calls"], 0)
        model.assert_not_called()

    def test_stale_account_dispatch_does_not_prevent_a_new_explicit_wake(self):
        import time
        apply_control(self.r, self.p, self.body())
        self.r.db.execute("INSERT INTO admin_dispatch(project,token,host_epoch,lease_until,created) VALUES(?,?,?,?,?)", (self.p, "old-dispatch", self.r.host_epoch(), time.time()+60, time.time()))
        self.r.account_changed()
        self.r.admin_observe_model(self.p, "new-root", "new-model")
        self.assertFalse(control_status(self.r, self.p)["runner"]["running"])
        body = self.body(request_id="new-account-wake")
        wake = {key: value for key, value in body.items() if key.startswith("expected_") or key == "request_id"}
        with patch.object(self.r, "_launch_admin") as launch:
            response = apply_control(self.r, self.p, wake, wake=True)
        self.assertEqual(response["launch"]["status"], "accepted")
        launch.assert_called_once()

    def test_shared_budget_changes_are_visible_from_other_hosts(self):
        body = self.body()
        body["limits"]["max_calls_per_day"] = 77
        apply_control(self.r, self.p, body)
        other = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        self.assertEqual(admin_budget_policy(other)["values"]["max_calls_per_day"], 77)
        self.assertFalse(other.admin_policy(self.p)["enabled"])

    def test_explicit_wake_is_idempotent_and_excludes_history(self):
        apply_control(self.r, self.p, self.body())
        original = self.body(request_id="wake-once")
        wake = {key: value for key, value in original.items() if key.startswith("expected_") or key == "request_id"}
        with patch.object(self.r, "_launch_admin") as launch:
            response = apply_control(self.r, self.p, wake, wake=True)
            self.assertEqual(apply_control(self.r, self.p, wake, wake=True), response)
        launch.assert_called_once()
        self.assertEqual(response["launch"]["status"], "accepted")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM history_jobs").fetchone()[0], 0)

    def test_real_queue_drains_more_than_one_small_batch_without_repeated_calls(self):
        from agent_learning.admin import admin_drain
        apply_control(self.r, self.p, self.body())
        for index in range(5):
            self.r.feedback(self.p, "root-chat", "independentfailure" + str(index))
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = admin_drain(self.r, self.p, max_seconds=30)
        self.assertEqual(model.call_count, 6)
        self.assertEqual(result["invocations"], 6)
        self.assertGreaterEqual(result["batches"], 2)
        self.assertEqual(result["stop_reason"], "idle")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)

    def test_account_change_after_save_never_launches_using_the_new_account(self):
        from agent_learning.admin_control import wake_admin
        def changed(runtime, project, **kwargs):
            runtime.account_changed()
            runtime.admin_observe_model(project, "other-account-root", "new-account-model")
            return wake_admin(runtime, project, **kwargs)
        with patch("agent_learning.admin_control.wake_admin", side_effect=changed), patch.object(self.r, "_launch_admin") as launch:
            response = apply_control(self.r, self.p, self.body(start=True))
        self.assertEqual(response["launch"]["reason"], "reconnect_required")
        self.assertEqual(response["launch"]["status"], "waiting")
        launch.assert_not_called()


class AdminControlHttpTests(unittest.TestCase):
    body = AdminControlTests.body

    def setUp(self):
        AdminControlTests.setUp(self)
        self.server = make_browser_server(self.home / "runtime.sqlite3", self.p, review=True, admin_control=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, **headers):
        supplied = {"Authorization": "Bearer " + self.server.access_token,
            "X-Admin-Token": self.server.admin_token, "Origin": self.server.origin, "Content-Type": "application/json"}
        supplied.update(headers)
        supplied = {key: value for key, value in supplied.items() if value is not None}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_get_is_readonly_and_never_constructs_runtime(self):
        before = self.r.db.total_changes
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("GET initialized Runtime")), patch("subprocess.Popen") as model:
            code, response = self.request("/api/admin-control")
        self.assertEqual(code, 200, response)
        self.assertEqual(response["budget_policy"]["scope"], "ALP_HOME")
        self.assertFalse(response["enabled"])
        self.assertEqual(self.r.db.total_changes, before)
        model.assert_not_called()
        self.assertNotIn(self.server.admin_token, json.dumps(response))

    def test_history_query_failure_does_not_hide_core_settings(self):
        with patch("agent_learning.admin_recovery.history_recovery_page", side_effect=sqlite3.OperationalError("interrupted")), \
                patch("subprocess.Popen") as model:
            code, core = self.request("/api/admin-control")
            self.assertEqual(code, 200, core)
            self.assertTrue(core["recovery"]["history_separate"])
            code, history = self.request("/api/admin-control/history")
            self.assertEqual(code, 503, history)
            self.assertEqual(history["reason"], "query_budget_exceeded")
            self.assertEqual(self.request("/api/admin-control")[0], 200)
        model.assert_not_called()

    def test_history_page_is_project_scoped_and_does_not_construct_runtime(self):
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("GET constructed Runtime")):
            code, history = self.request("/api/admin-control/history?limit=1&offset=0")
        self.assertEqual(code, 200, history)
        self.assertEqual(history["items"], [])
        self.assertEqual(self.request("/api/admin-control/history?project=another")[0], 400)
        self.assertEqual(self.request("/api/admin-control/history", Authorization="Bearer invalid")[0], 401)

    def test_settings_remain_available_when_each_detail_section_exceeds_budget(self):
        for section, target in (("queue", "agent_learning.admin_control._control_queue"),
                                ("recovery", "agent_learning.admin_recovery.recovery_status"),
                                ("activity", "agent_learning.admin_review_flow.review_activity")):
            with self.subTest(section=section), patch(target, side_effect=sqlite3.OperationalError("interrupted")), \
                    patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("GET initialized Runtime")), \
                    patch("subprocess.Popen") as model:
                code, core = self.request("/api/admin-control?view=settings")
                self.assertEqual(code, 200, core)
                self.assertTrue(core["details_separate"])
                self.assertNotIn("queue", core)
                self.assertNotIn("recovery", core)
                self.assertNotIn("review_activity", core)
                code, detail = self.request("/api/admin-control/details?section=" + section)
                self.assertEqual(code, 503, detail)
                self.assertEqual(detail["reason"], "query_budget_exceeded")
                other = "recovery" if section != "recovery" else "activity"
                code, good = self.request("/api/admin-control/details?section=" + other)
                self.assertEqual(code, 200, good)
                self.assertEqual(good["project_id"], self.p)
                self.assertEqual(good["section"], other)
                self.assertEqual(good["versions"][0], core["policy"]["policy_version"])
                self.assertEqual(self.request("/api/admin-control?view=settings")[0], 200)
            model.assert_not_called()

    def test_split_settings_parameters_and_sections_cannot_select_another_project(self):
        for path in ("/api/admin-control?view=other", "/api/admin-control?view=settings&project=other",
                     "/api/admin-control/details", "/api/admin-control/details?section=other",
                     "/api/admin-control/details?section=queue&project=other"):
            self.assertEqual(self.request(path)[0], 400, path)
        self.assertEqual(self.request("/api/admin-control/details?section=queue", Authorization="Bearer invalid")[0], 401)

    def test_browser_save_and_receipt_do_not_scan_detail_panels(self):
        body = self.body(request_id="compact-save", enabled=False, start=False)
        with patch("agent_learning.admin_control._control_queue", side_effect=sqlite3.OperationalError("interrupted")), \
                patch("agent_learning.admin_recovery.recovery_status", side_effect=AssertionError("save read recovery")), \
                patch("subprocess.Popen") as model:
            code, saved = self.request("/api/admin-control/configure", body)
            self.assertEqual(code, 200, saved)
            self.assertTrue(saved["settings"]["details_separate"])
            self.assertFalse(saved["settings"]["enabled"])
            self.assertEqual(self.request("/api/admin-control/configure", body), (200, saved))
            code, receipt = self.request("/api/admin-control/receipt?request_id=compact-save")
            self.assertEqual(code, 200, receipt)
            self.assertEqual(receipt["response"], saved)
        model.assert_not_called()

    def test_review_key_and_cross_origin_never_configure_administrator(self):
        for headers in ({"X-Admin-Token": None}, {"X-Admin-Token": self.server.review_token},
                        {"Origin": "https://outside.example"}, {"Origin": None}):
            code, _ = self.request("/api/admin-control/configure", self.body(), **headers)
            self.assertEqual(code, 403)
        self.assertFalse(self.r.admin_policy(self.p)["enabled"])

    def test_http_configure_and_receipt_persist_without_model(self):
        with patch("subprocess.Popen") as model:
            code, response = self.request("/api/admin-control/configure", self.body())
        self.assertEqual(code, 200, response)
        self.assertTrue(response["settings"]["enabled"])
        code, receipt = self.request("/api/admin-control/receipt?request_id=control-test")
        self.assertEqual(code, 200, receipt)
        self.assertEqual(receipt["response"], response)
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
