"""第六模块队列集成：真实 SQLite 与内存审核器，无外部模型或宿主 Hook。"""
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_batch, admin_once
from agent_learning.admin_lifecycle import recover_calls, retry_call
from agent_learning.admin_queue import claim_dispatch, scan_signature
from agent_learning.runtime import Runtime


def fake_reviewer(decision="skip", callback=None):
    class Reviewer:
        def __init__(self, args, **kwargs):
            self.output = Path(args[args.index("--output-last-message") + 1])
            self.returncode = 0
            self.pid = None
            self.timeout = None

        def communicate(self, data=None, timeout=None):
            self.timeout = timeout
            packet = json.loads(data.decode("utf-8").split("<untrusted_evidence>", 1)[1]
                                .rsplit("</untrusted_evidence>", 1)[0])
            if callback:
                callback(packet, self)
            candidate = None
            if decision == "candidate":
                problem = packet["problem"]
                candidate = {"problem_id": problem["id"], "problem_revision": problem["revision"],
                             "title": "Queue candidate", "content": "Only supplied evidence",
                             "category": problem["category"], "applicability": "Same observation",
                             "limitations": "Reported only", "evidence_refs": [e["id"] for e in packet["events"]],
                             "validation_level": "reported", "knowledge_kind": "solution", "metadata": {}}
            self.output.write_text(json.dumps({"decision": decision, "reason": "Controlled queue test",
                                               "candidate": candidate}), encoding="utf-8")
            return None, None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

        def wait(self, timeout=None):
            return self.returncode

    return Reviewer


class Module6QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module6-queue-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "queueinvariant")
        self.configure(self.r)

    def configure(self, runtime, **values):
        runtime.config["admin"].update(enabled=True, command=["memory-reviewer"], max_calls_per_day=100,
                                       max_input_chars=16000, timeout_seconds=10, max_cases_per_batch=20,
                                       max_calls_per_batch=2, max_batch_seconds=30, auto_publish=False)
        runtime.config["admin"].update(values)

    def problem(self, summary="alphaerror", **values):
        return self.r.feedback(self.p, "chat", summary, category="operation", **values)

    def hook(self, turn):
        return self.r.handle_hook({"hook_event_name": "Stop", "session_id": "chat", "cwd": str(self.workspace),
                                   "turn_id": turn})

    def call_count(self):
        return self.r.db.execute("SELECT COUNT(*) FROM admin_calls WHERE state!='deduplicated'").fetchone()[0]

    def test_two_runtimes_allow_only_one_project_model_claim(self):
        self.problem("alphaerror")
        self.problem("betaerror")
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def block(packet, process):
            entered.set()
            if not release.wait(timeout=5):
                raise ValueError("test coordinator did not release reviewer")

        def first_worker():
            runtime = Runtime(self.home)
            try:
                self.configure(runtime)
                results.append(admin_once(runtime, self.p))
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                runtime.close()

        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer(callback=block)) as popen:
            thread = threading.Thread(target=first_worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                other = Runtime(self.home)
                try:
                    self.configure(other)
                    second = admin_once(other, self.p)
                finally:
                    other.close()
                self.assertEqual(second["status"], "waiting_for_review")
            finally:
                release.set()
                thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "skip")
        self.assertEqual(self.call_count(), 1)

    def test_concurrent_stop_persists_one_dispatch_and_spawns_once(self):
        self.problem()
        barrier = threading.Barrier(2)
        spawns, errors = [], []

        def record_launch(runtime, workspace, dispatch_token=None):
            spawns.append(dispatch_token)

        def worker(turn):
            runtime = Runtime(self.home)
            try:
                self.configure(runtime)
                barrier.wait(timeout=5)
                runtime.handle_hook({"hook_event_name": "Stop", "session_id": "chat", "cwd": str(self.workspace),
                                     "turn_id": turn})
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                runtime.close()

        with patch.object(Runtime, "_launch_admin", record_launch):
            threads = [threading.Thread(target=worker, args=("stop-" + str(i),)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
        self.assertEqual(errors, [])
        self.assertEqual(len(spawns), 1)
        self.assertTrue(spawns[0])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 1)
        self.assertEqual(self.call_count(), 0)

    def test_unchanged_completed_review_does_not_wake_on_stop(self):
        self.problem()
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        with patch.object(self.r, "_launch_admin") as launch:
            self.hook("reviewed-stop")
        launch.assert_not_called()

    def test_unchanged_preflight_does_not_wake_or_reserve_call(self):
        self.problem(resolution="X" * 8000)
        self.configure(self.r, max_input_chars=1000)
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            self.assertEqual(admin_once(self.r, self.p)["status"], "needs_evidence")
        popen.assert_not_called()
        with patch.object(self.r, "_launch_admin") as launch:
            self.hook("blocked-stop")
        launch.assert_not_called()
        self.assertEqual(self.call_count(), 0)

    def test_unrelated_publication_rechecks_signature_without_model_call(self):
        self.problem("alphaerror")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        before = scan_signature(self.r, self.p)
        source = self.problem("zebraknowledge")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (source["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=source["id"], problem_revision=source["revision"],
                                            title="zebraknowledge", content="zebra behavior", category="operation",
                                            applicability="zebra", limitations="reported", validation_level="reported",
                                            evidence_refs=refs)
        self.r.publish(self.p, candidate["candidate_id"])
        self.assertNotEqual(before, scan_signature(self.r, self.p))
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_batch(self.r, self.p)
        popen.assert_not_called()
        self.assertEqual(result["invocations"], 0)
        self.assertEqual(result["stop_reason"], "idle")

    def test_batch_stops_at_call_limit(self):
        for i in range(4):
            self.problem("differenterror" + str(i))
        self.configure(self.r, max_calls_per_batch=2)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            result = admin_batch(self.r, self.p)
        self.assertEqual(result["invocations"], 2)
        self.assertEqual(result["stop_reason"], "call_limit")
        self.assertEqual(self.call_count(), 2)

    def test_batch_stops_at_case_limit_without_model_calls(self):
        for i in range(5):
            self.problem("blockederror" + str(i), resolution="X" * 8000)
        self.configure(self.r, max_input_chars=1000, max_cases_per_batch=3)
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_batch(self.r, self.p)
        popen.assert_not_called()
        self.assertEqual(result["scanned_count"], 3)
        self.assertEqual(result["stop_reason"], "case_limit")

    def test_batch_deadline_stops_next_call_and_limits_current_timeout(self):
        self.problem("alphaerror")
        self.problem("betaerror")
        clock = {"value": 0.0}
        timeouts = []

        def advance(packet, process):
            timeouts.append(process.timeout)
            clock["value"] = 2.0

        self.configure(self.r, max_batch_seconds=1)
        with patch("agent_learning.admin.time.monotonic", side_effect=lambda: clock["value"]):
            with patch("agent_learning.admin.subprocess.Popen", fake_reviewer(callback=advance)):
                result = admin_batch(self.r, self.p)
        self.assertEqual(timeouts, [1])
        self.assertEqual(result["invocations"], 1)
        self.assertEqual(result["stop_reason"], "batch_time_exhausted")

    def test_batch_bypasses_eleven_blocked_cases_to_reach_later_case(self):
        blocked = [self.problem("blockederror" + str(i), resolution="X" * 8000) for i in range(11)]
        final = self.problem("usableerror")
        self.r.db.executemany("UPDATE problems SET created=? WHERE id=?",
                             [(index, item["id"]) for index, item in enumerate(blocked + [final], 1)])
        # 预算足够容纳普通案例，而长证据包在此预算下不可完整装入。
        self.configure(self.r, max_input_chars=3200, max_cases_per_batch=20, max_calls_per_batch=1)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            result = admin_batch(self.r, self.p)
        self.assertEqual(result["invocations"], 1)
        self.assertEqual(result["scanned_count"], 12)
        row = self.r.db.execute("SELECT problem FROM admin_calls WHERE state='skip'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], final["id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_preflights").fetchone()[0], len(blocked))

    def test_failed_invocation_not_reused_retry_allows_once(self):
        problem = self.problem()

        def bad_response(packet, process):
            process.returncode = 1

        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer(callback=bad_response)):
            failed = admin_once(self.r, self.p)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE call_id IS NOT NULL").fetchone()[0], 0)
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            self.assertEqual(admin_once(self.r, self.p)["status"], "idle")
        popen.assert_not_called()
        retry_call(self.r, self.p, failed["call_id"], "Explicit controlled retry")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            retried = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(retried["status"], "skip")
        self.assertEqual(self.call_count(), 2)
        self.assertEqual(retry_call(self.r, self.p, failed["call_id"], "Replay retry request")["status"], "retry_consumed")
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            self.assertEqual(admin_once(self.r, self.p)["status"], "idle")
        popen.assert_not_called()
        self.assertEqual(self.call_count(), 2)

    def test_timeout_unknown_does_not_become_reusable_review(self):
        self.problem()

        def timeout(packet, process):
            raise subprocess.TimeoutExpired("memory-reviewer", process.timeout)

        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer(callback=timeout)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "timeout_unknown")
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            self.assertEqual(admin_once(self.r, self.p)["status"], "idle")
        popen.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE call_id IS NOT NULL").fetchone()[0], 0)

    def test_late_lease_response_cannot_create_candidate_or_receipt(self):
        self.problem()

        def expire(packet, process):
            self.r.db.execute("UPDATE admin_calls SET lease_until=? WHERE project=? AND state='started'",
                              (time.time() - 1, self.p))
            recover_calls(self.r, self.p)

        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate", callback=expire)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "orphaned_unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts WHERE call_id IS NOT NULL").fetchone()[0], 0)

    def test_dispatch_token_and_batch_release(self):
        self.problem()
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(token)
        self.assertIsNone(claim_dispatch(self.r, self.p))
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            result = admin_batch(self.r, self.p, dispatch_token=token)
        self.assertEqual(result["invocations"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)

    def test_expired_dispatch_cannot_start_models(self):
        self.problem()
        token = claim_dispatch(self.r, self.p)
        self.r.db.execute("UPDATE admin_dispatch SET lease_until=? WHERE project=?", (time.time() - 1, self.p))
        with patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_batch(self.r, self.p, dispatch_token=token)
        popen.assert_not_called()
        self.assertEqual(result["status"], "dispatch_expired")
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
