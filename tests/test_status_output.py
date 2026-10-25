"""Status projections preserve health facts without copying large or private snapshots."""
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning import __version__
from agent_learning.status_output import MAX_MODEL_CHARS, StatusSummary, status_summary


def fixture():
    usage = {"scope": "project", "project_id": "synthetic-project", "utc_day": "2026-10-25",
             "next_reset_at": 1792992000, "counted_attempts": 5, "completed_reviews": 1,
             "failed_attempts": 2, "result_unknown_attempts": 1, "active_attempts": 1,
             "usage_unknown_attempts": 3, "reported_tokens": None, "token_reservations": 12000,
             "remaining_calls": 95, "remaining_reported_tokens": None, "admission_status": "available",
             "budget_mode": "legacy", "call_limit_enforced": True}
    return {"project_id": "synthetic-project", "home": "synthetic knowledge home", "host_scope": "dsh:default",
        "host_epoch": 5, "storage_schema_version": 25, "knowledge_scope": "shared_project",
        "admin_enabled": True, "admin_admission_enabled": False,
        "admin_routing": {"policy": {"enabled": True, "provider": "host", "model_mode": "first", "model": ""},
            "executor": {"provider": "dsh", "model_mode": "first", "model": "google/synthetic-model"},
            "routing_ready": True, "ready": True, "reason": None,
            "recent_calls": [{"private": "secret-call"}] * 1000, "command": ["secret-executable"]},
        "admin_dispatch_diagnostic": {"phase": "model_started", "reason": "model_started", "next_due": None,
            "last_observation": {"details": "secret-details"}},
        "project_usage": usage, "global_usage": {**usage, "scope": "ALP_HOME", "counted_attempts": 10},
        "admission": {"admission_status": "available", "blocking_scope": None, "project_usage": usage},
        "workload": {"available": True, "stages": {"blocked": 4, "pending_review": 12,
            "awaiting_evidence": 3, "awaiting_approval": 1}},
        "work_memory": {"index_version": 2, "index_rebuild_pending": 0, "capture_repair_pending": 0,
            "settings": {"enabled": True, "automatic_context": False, "summary_enabled": False}},
        "work_summary_policy": {"enabled": False}, "hindsight_enabled": False,
        "scheduler": {"enabled": False, "continuous_service_installed": False, "items": ["secret-job"] * 1000},
        "host_capture": {"state": "observed", "public_delivery": "unknown", "public_response": "partial", "reason": None},
        "history_import": {"evidence": "secret-history" * 10000}, "capability": "secret-capability",
        "context_handle": "secret-handle", "_meta": {"access_token": "secret-token"}}


def wire_size(value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    envelope = json.dumps({"content": [{"type": "text", "text": body}], "isError": False},
                          ensure_ascii=False, separators=(",", ":"))
    return len(envelope.encode("utf-16-le", errors="surrogatepass")) // 2


class StatusOutputTests(unittest.TestCase):
    def test_health_identity_actual_admission_and_two_ledgers_are_preserved(self):
        original = fixture()
        frozen = copy.deepcopy(original)
        value = status_summary(original)
        self.assertIsInstance(value, StatusSummary)
        self.assertEqual(original, frozen)
        for name in ("project_id", "home", "host_scope", "host_epoch", "storage_schema_version"):
            self.assertEqual(value[name], original[name])
        self.assertEqual(value["version"], __version__)
        self.assertEqual(value["host"], "dsh")
        self.assertTrue(value["admin_enabled"])
        self.assertFalse(value["admin_admission_enabled"])
        self.assertEqual(value["admin_dispatch_diagnostic"]["phase"], "model_started")
        self.assertEqual(value["admin_routing"]["executor"]["model"], "google/synthetic-model")
        for name, count in (("project_usage", 5), ("global_usage", 10)):
            self.assertEqual(value[name]["counted_attempts"], count)
            self.assertEqual(value[name]["result_unknown_attempts"], 1)
            self.assertEqual(value[name]["active_attempts"], 1)
            self.assertIsNone(value[name]["reported_tokens"])
        self.assertLessEqual(wire_size(value), MAX_MODEL_CHARS)
        self.assertEqual(value["full_view"], {"tool": "learning_status", "arguments": {"view": "full"}})

    def test_projection_has_no_private_snapshots_or_arbitrary_fields(self):
        encoded = json.dumps(status_summary(fixture()))
        for forbidden in ("secret-", "recent_calls", "last_observation", "history_import", "capability", "_meta"):
            self.assertNotIn(forbidden, encoded)

    def test_missing_and_invalid_metrics_remain_unknown_without_global_fallback(self):
        value = fixture()
        value.pop("project_usage")
        value.pop("admin_admission_enabled")
        value["global_usage"].update(reported_tokens=math.nan, active_attempts=math.inf,
                                     remaining_calls=10**10000, failed_attempts=True)
        result = status_summary(value)
        self.assertEqual(result["project_usage"], {})
        self.assertIsNone(result["admin_admission_enabled"])
        for name in ("reported_tokens", "active_attempts", "remaining_calls", "failed_attempts"):
            self.assertIsNone(result["global_usage"][name])
        json.dumps(result, allow_nan=False)

    def test_string_escaping_and_maximum_counters_cannot_exceed_total_reply_budget(self):
        for character in ('"', "\\", "\n", "中文", "\U0001f642", '\U0001f642"\\'):
            with self.subTest(character=repr(character)):
                value = fixture()
                def flood(data):
                    for name, item in data.items():
                        if isinstance(item, dict):
                            flood(item)
                        elif isinstance(item, str):
                            data[name] = character * 5000
                        elif type(item) in (int, float):
                            data[name] = 2**63 - 1
                flood(value)
                result = status_summary(value)
                self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)
                self.assertTrue(result["strings_truncated"])
                self.assertFalse(result["admin_admission_enabled"])
                self.assertIn("result_unknown_attempts", result["project_usage"])

    def test_every_optional_section_short_escaped_strings_and_long_identity_stay_bounded(self):
        for length in (8, 16, 512):
            value = fixture()
            value["admin_dispatch_diagnostic"].update(blocking_scope="project", blocked_review_scope="host")
            value["scheduler"].update(total_jobs=3, sync_dirty_count=2,
                worker={"state": "running", "host_epoch": 5, "lease_until": 123},
                items=[{"kind": "admin", "state": "waiting", "wait_reason": "input_limit", "next_due": 123}] * 3)
            value["host_capture"].update(scan_limited=False,
                public_delivery={"state": "observed", "value": "unknown", "available": False, "reason": "missing"},
                public_response={"state": "observed", "value": "partial", "available": True, "reason": "unverified"},
                input_window={"state": "observed", "public_input": "available", "available": True, "reason": "bound"})
            def adversarial(data):
                for name, item in data.items():
                    if isinstance(item, dict):
                        adversarial(item)
                    elif isinstance(item, list):
                        for entry in item:
                            if isinstance(entry, dict):
                                adversarial(entry)
                    elif isinstance(item, str):
                        data[name] = ('"\\' * length)[:length]
                    elif type(item) in (int, float):
                        data[name] = 2**63 - 1
            adversarial(value)
            value["home"] = ('D:\\"' * 2000)
            result = status_summary(value)
            with self.subTest(length=length):
                self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)
                self.assertFalse(result["admin_admission_enabled"])
                self.assertEqual(result["project_usage"]["active_attempts"], 2**63 - 1)
                self.assertEqual(result["global_usage"]["result_unknown_attempts"], 2**63 - 1)
                self.assertIsNone(result["project_usage"]["reported_tokens"])
                self.assertEqual(result["full_view"]["arguments"], {"view": "full"})
                self.assertTrue(result["details_omitted"])

    def test_capture_object_observations_are_whitelisted_instead_of_becoming_null(self):
        value = {"host_capture": {
            "state": "observed", "reason": None,
            "public_response": {"state": "observed", "value": "available", "reason": "unverified", "private": "secret"},
            "public_delivery": {"value": "unknown", "available": False},
            "input_window": {"state": "observed", "public_input": "available", "available": True,
                             "reason": "bound", "path": "secret"}}}
        result = status_summary(value)["host_capture"]
        self.assertEqual(result["public_response"], {"state": "observed", "value": "available", "reason": "unverified"})
        self.assertEqual(result["public_delivery"], {"value": "unknown", "available": False})
        self.assertEqual(result["input_window"]["reason"], "bound")
        self.assertNotIn("secret", json.dumps(result))


class StatusProducerTests(unittest.TestCase):
    def test_real_runtime_status_preserves_actual_memory_scheduler_and_capture_shapes(self):
        from agent_learning.antigravity import AntigravityAdapter
        from agent_learning.runtime import Runtime
        root = Path(__file__).resolve().parents[1]
        area = Path(os.environ.get("ALP_TEST_ROOT", root / "build/local/opt1426/status-summary/cases"))
        area.mkdir(parents=True, exist_ok=True)
        area_temp = tempfile.TemporaryDirectory(prefix="status-producer-", dir=area)
        self.addCleanup(area_temp.cleanup)
        base = Path(area_temp.name)
        workspace = base / "workspace"
        workspace.mkdir()
        runtime = Runtime(base / "home", config={"work_memory": {"automatic_context": False}}, host="antigravity")
        self.addCleanup(runtime.close)
        project = runtime.project(workspace)
        runtime.admin_configure(project, True, "host", "first", "", expected_policy_version=0,
                                expected_epoch=runtime.session_epoch())
        runtime.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated) VALUES(?,?,0,1,?)",
                           (project, runtime.host_scope, time.time()))
        runtime.db.execute("INSERT INTO scheduler_jobs(project,kind,state,signature,next_due,wait_reason,created,updated) "
                           "VALUES(?,'sync','waiting','synthetic',123,'sync_retry_time',0,0)", (project,))
        runtime.db.execute("INSERT INTO scheduler_worker VALUES(1,'synthetic-token',?,'running',?,0)",
                           (runtime.host_epoch(), time.time() + 60))
        AntigravityAdapter(runtime, workspace).handle("Stop", {"conversationId": "synthetic-capture",
            "workspacePaths": [str(workspace)], "executionNum": 0, "fullyIdle": True, "terminationReason": "model_stop"})
        with patch("subprocess.Popen") as model:
            full = runtime.status(project)
            before = runtime.db.total_changes
            result = status_summary(full)
        model.assert_not_called()
        self.assertEqual(runtime.db.total_changes, before)
        self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)
        self.assertTrue(result["admin_enabled"])
        self.assertFalse(result["admin_admission_enabled"])
        self.assertFalse(result["work_memory"]["settings"]["automatic_context"])
        self.assertEqual(result["scheduler"]["worker"]["state"], full["scheduler"]["worker"]["state"])
        self.assertEqual(result["scheduler"]["items"][0]["wait_reason"], full["scheduler"]["items"][0]["wait_reason"])
        self.assertEqual(result["host_capture"]["public_delivery"], full["host_capture"]["public_delivery"])
        self.assertEqual(result["host_capture"]["input_window"]["reason"], full["host_capture"]["input_window"]["reason"])
        self.assertEqual(result["project_usage"]["scope"], "project")
        self.assertEqual(result["global_usage"]["scope"], "ALP_HOME")


if __name__ == "__main__":
    unittest.main()
