"""管理员离线回归：真实 SQLite 与本地假执行器，不调用模型服务。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.admin import RESPONSE_SCHEMA, _fingerprint, admin_once
from agent_learning.mcp import CANDIDATE
from agent_learning.runtime import ContractError, Runtime


REVIEWER = r'''
import json,pathlib,sys,time
mode=sys.argv[1]
if mode=='sleep':
    time.sleep(10)
packet=json.loads(sys.stdin.read().split('<untrusted_evidence>',1)[1].rsplit('</untrusted_evidence>',1)[0])
def check(node):
    if isinstance(node,dict):
        if node.get('type')=='object':
            assert set(node.get('required',[]))==set(node.get('properties',{}))
            assert node.get('additionalProperties') is False
        for item in node.values(): check(item)
    elif isinstance(node,list):
        for item in node: check(item)
check(json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text(encoding='utf-8')))
candidate=None
decision='skip'
if mode in ('candidate','tool_candidate'):
    problem=packet['problem']
    candidate={'problem_id':problem['id'],'problem_revision':problem['revision'],
               'title':'Local review','content':'Only the supplied observation',
               'category':problem['category'],'applicability':'Same environment',
               'limitations':'No product acceptance',
               'evidence_refs':[event['id'] for event in packet['events']],
               'validation_level':'tool_success' if mode=='tool_candidate' else 'reported',
               'knowledge_kind':'solution',
               'metadata':dict.fromkeys(('tool','tool_version','error_code','environment','verification'),'')}
    decision='candidate'
reason=json.dumps({'snapshot_revision':packet['problem']['revision'],
                   'related_knowledge':len(packet.get('related_knowledge',[])),
                   'related_candidates':len(packet.get('related_candidates',[]))})
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(
    json.dumps({'decision':decision,'reason':reason,'candidate':candidate}),encoding='utf-8')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':123,'cached_input_tokens':45,'output_tokens':7}}))
'''


class AdminOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-admin-test-")
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.runtime = Runtime(self.home)
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "chat-a", "Review tools", {"environment": "A"})
        self.reviewer = Path(self.temp.name) / "isolated-reviewer.py"
        self.reviewer.write_text(REVIEWER, encoding="utf-8")
        self.configure()

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def configure(self, mode="candidate", **options):
        settings = dict(enabled=True, command=[sys.executable, str(self.reviewer), mode],
                        max_calls_per_day=5, max_input_chars=16000, timeout_seconds=5, auto_publish=False)
        settings.update(options)
        self.runtime.config["admin"].update(settings)

    def problem(self, conversation="chat-a", summary="A repeatable tool issue"):
        return self.runtime.feedback(self.project, conversation, summary, category="operation")

    def calls(self):
        return list(self.runtime.db.execute("SELECT * FROM admin_calls ORDER BY created"))

    def test_completed_historical_task_can_be_reviewed(self):
        problem = self.problem()
        self.runtime.task_update(self.project, "chat-a", "Review tools", status="delivered")
        self.runtime.task_update(self.project, "chat-a", "Another task", new_task=True)
        result = admin_once(self.runtime, self.project, problem["id"])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(self.calls()[0]["revision"], problem["revision"])

    def test_preflight_failure_does_not_reserve_and_can_recover(self):
        self.problem()
        self.runtime.config["admin"]["command"] = []
        with self.assertRaises(ContractError):
            admin_once(self.runtime, self.project)
        self.assertEqual(self.calls(), [])
        self.configure()
        self.runtime.config["admin"]["max_input_chars"] = 1000
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "needs_evidence")
        self.assertEqual(self.calls(), [])
        self.runtime.config["admin"]["max_input_chars"] = 16000
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "candidate")

    def test_oversized_old_problem_does_not_starve_small_new_problem(self):
        self.configure(max_input_chars=3500)
        old_problem = self.problem(summary="An oversized old issue " + "x" * 1900)
        new_problem = self.problem(summary="A small new issue")
        self.runtime.db.executemany("UPDATE problems SET created=? WHERE id=?",
                                   [(1, old_problem["id"]), (2, new_problem["id"])])
        self.assertTrue(self.runtime.review_packet(self.project, old_problem["id"])["incomplete"])
        self.assertEqual(admin_once(self.runtime, self.project, old_problem["id"])["status"], "needs_evidence")
        self.assertEqual(self.calls(), [])

        result = admin_once(self.runtime, self.project)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["skipped_problem_ids"], [old_problem["id"]])
        self.assertEqual([call["problem"] for call in self.calls()], [new_problem["id"]])
        self.assertEqual(self.runtime.db.execute("SELECT status FROM problems WHERE id=?",
                                                (old_problem["id"],)).fetchone()[0], "pending")

        blocked = admin_once(self.runtime, self.project)
        self.assertEqual(blocked["status"], "needs_evidence")
        self.assertEqual(blocked["skipped_problem_ids"], [old_problem["id"]])
        self.assertEqual(len(self.calls()), 1)
        self.runtime.config["admin"]["max_calls_per_day"] = 0
        with patch.object(self.runtime, "review_packet", side_effect=AssertionError("zero budget must not scan")):
            self.assertEqual(admin_once(self.runtime, self.project)["status"], "budget_exhausted")
        self.configure()
        self.assertEqual(admin_once(self.runtime, self.project, old_problem["id"])["status"], "candidate")
        self.assertEqual([call["problem"] for call in self.calls()], [new_problem["id"], old_problem["id"]])

    def test_preflight_records_advance_across_calls_and_budget_changes(self):
        self.configure(max_input_chars=3500)
        old_problems = [self.problem(summary=f"Oversized old issue {index} " + "x" * 1900)
                        for index in range(11)]
        new_problem = self.problem(summary="A small issue behind eleven oversized items")
        # 夹具明确队列先后，不依赖 Windows 时钟能分辨连续写入。
        self.runtime.db.executemany("UPDATE problems SET created=? WHERE id=?",
                                   [(index, problem["id"]) for index, problem in enumerate(old_problems + [new_problem], 1)])
        original_packet = self.runtime.review_packet
        with patch.object(self.runtime, "review_packet", wraps=original_packet) as packets:
            first = admin_once(self.runtime, self.project)
            self.assertEqual(first["status"], "needs_evidence")
            self.assertEqual(first["skipped_problem_ids"], [problem["id"] for problem in old_problems[:10]])
            self.assertEqual(packets.call_count, 10)
            self.assertEqual(self.calls(), [])

        # 关闭后重开也保留扫描进度，不能只依靠进程内游标。
        self.runtime.close()
        self.runtime = Runtime(self.home)
        self.configure(max_input_chars=3500)
        with patch.object(self.runtime, "review_packet", wraps=self.runtime.review_packet) as packets:
            second = admin_once(self.runtime, self.project)
            self.assertEqual(second["status"], "candidate")
            self.assertEqual(second["skipped_problem_ids"], [old_problems[10]["id"]])
            # 一次旧超限项、一次新审核包，接受响应前还需核对比较资料未变化。
            self.assertEqual(packets.call_count, 3)
        self.assertEqual([call["problem"] for call in self.calls()], [new_problem["id"]])
        with patch.object(self.runtime, "review_packet", side_effect=AssertionError("unchanged preflights must not repeat")):
            blocked = admin_once(self.runtime, self.project)
        self.assertEqual(blocked["status"], "needs_evidence")
        self.assertEqual(len(blocked["skipped_problem_ids"]), 10)
        self.assertEqual(len(self.calls()), 1)

        # 当前任务交付不改变历史事件时点，既有超限结论无需再次取包。
        self.runtime.task_update(self.project, "chat-a", "Review tools", status="delivered")
        with patch.object(self.runtime, "review_packet", wraps=self.runtime.review_packet) as packets:
            changed_snapshot = admin_once(self.runtime, self.project)
            self.assertEqual(changed_snapshot["status"], "needs_evidence")
            self.assertEqual(packets.call_count, 0)
        self.runtime.feedback(self.project, "chat-a", old_problems[0]["summary"],
                              category="operation", resolution="Additional evidence")
        with patch.object(self.runtime, "review_packet", wraps=self.runtime.review_packet) as packets:
            changed_evidence = admin_once(self.runtime, self.project)
            self.assertEqual(changed_evidence["status"], "needs_evidence")
            self.assertEqual(packets.call_count, 1)
            self.assertIn(old_problems[0]["id"], changed_evidence["skipped_problem_ids"])
        self.assertEqual(len(self.calls()), 1)

        self.configure(max_input_chars=16000)
        recovered = admin_once(self.runtime, self.project)
        self.assertEqual(recovered["status"], "candidate")
        self.assertEqual([call["problem"] for call in self.calls()], [new_problem["id"], old_problems[0]["id"]])

    def test_definite_start_failure_is_retryable_and_not_budgeted(self):
        self.problem()
        self.runtime.config["admin"].update(max_calls_per_day=1)
        # 执行器通过只读可用性检查后，启动仍可能明确失败；保留此边界。
        with patch("agent_learning.admin.subprocess.Popen", side_effect=FileNotFoundError("controlled launch failure")):
            first = admin_once(self.runtime, self.project)
        self.assertEqual(first["status"], "not_started")
        self.assertIsNone(self.calls()[0]["started"])
        self.configure()
        self.runtime.config["admin"]["max_calls_per_day"] = 1
        waiting = admin_once(self.runtime, self.project)
        self.assertEqual(waiting["status"], "executor_not_ready")
        self.assertEqual(len(self.calls()), 1)
        # Advance the isolated executor cooldown without sleeping or spending another call.
        self.runtime.db.execute("UPDATE admin_executor_failures SET next_try=0")
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "candidate")
        self.assertEqual(sum(call["started"] is not None for call in self.calls()), 1)

    def test_missing_executable_is_not_ready_without_call_or_budget_reservation(self):
        self.problem()
        self.runtime.config["admin"].update(command=[str(self.home / "missing-executable")], max_calls_per_day=1)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = admin_once(self.runtime, self.project)
        launch.assert_not_called()
        self.assertEqual(result["status"], "executor_not_ready")
        self.assertNotIn("call_id", result)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_call_budgets").fetchone()[0], 0)

    def test_packet_and_reservation_are_atomic_and_model_runs_outside(self):
        problem = self.problem()
        ready, go, committed = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def writer():
            other = Runtime(self.home)
            ready.set()
            try:
                if not go.wait(5):
                    raise AssertionError("writer was not released")
                other.feedback(self.project, "chat-a", problem["summary"], category="operation", resolution="New evidence")
                committed.set()
            except BaseException as error:
                errors.append(error)
            finally:
                other.close()

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(ready.wait(5))
        original_packet = self.runtime.review_packet
        original_popen = subprocess.Popen

        def packet(*args):
            self.assertTrue(self.runtime.db.in_transaction)
            return original_packet(*args)

        def launch(*args, **kwargs):
            self.assertFalse(self.runtime.db.in_transaction)
            go.set()
            self.assertTrue(committed.wait(5))
            return original_popen(*args, **kwargs)

        try:
            with patch.object(self.runtime, "review_packet", side_effect=packet), patch(
                    "agent_learning.admin.subprocess.Popen", side_effect=launch):
                first = admin_once(self.runtime, self.project)
            self.assertEqual(first["status"], "failed")
            self.assertEqual(self.calls()[0]["revision"], problem["revision"])
            self.assertEqual(admin_once(self.runtime, self.project)["status"], "candidate")
            self.assertEqual([call["revision"] for call in self.calls()], [1, 2])
        finally:
            go.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_cross_problem_exact_duplicate_skips_without_spending_budget(self):
        self.problem()
        self.runtime.config["admin"]["max_calls_per_day"] = 1
        first = admin_once(self.runtime, self.project)
        self.runtime.task_update(self.project, "chat-b", "Review tools", {"environment": "A"})
        second_problem = self.problem("chat-b")
        second = admin_once(self.runtime, self.project, second_problem["id"])
        self.assertEqual(second["status"], "skip")
        self.assertEqual(second["duplicate_of"], first["call_id"])
        self.assertEqual(self.calls()[-1]["state"], "deduplicated")
        self.assertIsNone(self.calls()[-1]["started"])
        self.problem("chat-b", "A genuinely different issue")
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "budget_exhausted")

    def test_different_scope_is_not_deduplicated(self):
        self.problem()
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "candidate")
        self.runtime.task_update(self.project, "chat-b", "Review tools", {"environment": "B"})
        self.problem("chat-b")
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "candidate")
        self.assertEqual(len(self.calls()), 2)

    def test_fingerprint_ignores_transport_but_keeps_business_inputs(self):
        packet = {"problem": {"summary": "Same", "category": "operation"}, "task": {"scope": {}},
                  "events": [{"id": "old", "created": 1, "type": "PostToolUse", "outcome": "succeeded",
                              "payload": {"turn_id": "old", "metadata": {}, "tool_input": {"id": "asset-a", "timestamp": 1}}}]}
        updated = copy.deepcopy(packet)
        updated["events"][0].update(id="new", created=2)
        updated["events"][0]["payload"].update(turn_id="new", metadata={"tool": "", "environment": ""})
        self.assertEqual(_fingerprint(packet), _fingerprint(updated))
        updated["events"][0]["payload"]["tool_input"]["id"] = "asset-b"
        self.assertNotEqual(_fingerprint(packet), _fingerprint(updated))
        updated = copy.deepcopy(packet)
        updated["events"][0]["payload"]["tool_input"]["timestamp"] = 2
        self.assertNotEqual(_fingerprint(packet), _fingerprint(updated))

    def test_strict_model_schema_does_not_change_optional_mcp_metadata(self):
        self.assertNotIn("metadata", CANDIDATE["required"])
        self.assertNotIn("knowledge_kind", CANDIDATE["required"])
        candidate = RESPONSE_SCHEMA["properties"]["candidate"]["anyOf"][0]
        self.assertIn("metadata", candidate["required"])
        self.assertIn("knowledge_kind", candidate["required"])
        metadata = candidate["properties"]["metadata"]
        self.assertEqual(set(metadata["required"]), set(metadata["properties"]))
        self.assertFalse(metadata["additionalProperties"])

    def test_usage_contains_only_reported_numbers_and_elapsed(self):
        self.configure(mode="skip")
        self.problem()
        result = admin_once(self.runtime, self.project)
        usage = json.loads(self.calls()[0]["usage"])
        self.assertEqual(usage, result["usage"])
        self.assertEqual(usage["token_usage"], [{"input_tokens": 123, "cached_input_tokens": 45, "output_tokens": 7}])
        self.assertGreaterEqual(usage["elapsed_seconds"], 0)
        self.assertIsNotNone(self.calls()[0]["started"])
        self.assertIsNotNone(self.calls()[0]["finished"])
        self.assertNotIn("total_tokens", usage["token_usage"][0])
        self.assertEqual(list(self.home.rglob("events.log")), [])

    def test_truncated_evidence_can_propose_but_never_auto_publish(self):
        self.configure(mode="tool_candidate")
        self.runtime.config["admin"]["auto_publish"] = True
        self.runtime.config["event_max_chars"] = 256
        self.runtime.handle_hook({"hook_event_name": "PreToolUse", "session_id": "chat-a",
                                  "cwd": str(self.workspace), "tool_use_id": "success", "tool_name": "Bash"})
        self.runtime.handle_hook({"hook_event_name": "PostToolUse", "session_id": "chat-a",
                                  "cwd": str(self.workspace), "tool_use_id": "success", "tool_name": "Bash",
                                  "tool_response": {"exit_code": 0, "output": "x" * 2000}})
        event_id = self.runtime.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        problem = self.runtime.feedback(self.project, "chat-a", "A result with a long log", category="operation", event_refs=[event_id])
        packet = self.runtime.review_packet(self.project, problem["id"])
        self.assertTrue(packet["evidence_truncated"])
        self.assertFalse(packet["incomplete"])
        result = admin_once(self.runtime, self.project)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["publication"], {"status": "not_published", "reason": "evidence truncated"})
        candidate = json.loads(self.runtime.db.execute("SELECT body FROM candidates WHERE id=?", (result["candidate_id"],)).fetchone()[0])
        self.assertIn("未核对完整原文", candidate["limitations"])
        self.assertEqual(self.runtime.status(self.project)["counts"]["active"], 0)

    def test_timeout_is_counted_and_not_blindly_retried(self):
        self.configure(mode="sleep")
        self.runtime.config["admin"].update(timeout_seconds=1, max_calls_per_day=1)
        self.problem()
        first = admin_once(self.runtime, self.project)
        self.assertEqual(first["status"], "timeout_unknown")
        self.assertEqual(first["usage"]["token_usage"], [])
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "idle")
        self.problem(summary="Another issue")
        # Executor health is checked before reserving a different case; the
        # timeout's used allowance is still retained independently.
        self.assertEqual(admin_once(self.runtime, self.project)["status"], "executor_not_ready")
        from agent_learning.admin_usage import admin_admission_status
        self.assertEqual(admin_admission_status(self.runtime, self.project)["admission_status"], "budget_exhausted")
        self.assertEqual(len(self.calls()), 1)
