"""送审包真实 SQLite 回归；模型入口只使用本地固定假执行器。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint, admin_once
from agent_learning.review_packet import ReviewPacketMixin, review_instruction
from agent_learning.runtime import ContractError, Runtime


_REVIEWER = r'''
import json,pathlib,sys
packet=json.loads(sys.stdin.read().split('<untrusted_evidence>',1)[1].rsplit('</untrusted_evidence>',1)[0])
p=packet['problem']
body={'problem_id':p['id'],'problem_revision':p['revision'],'title':'Packet review',
      'content':'Only reported evidence','category':p['category'],'applicability':'Same task',
      'limitations':'No acceptance claim','evidence_refs':[e['id'] for e in packet['events']],
      'validation_level':'reported','knowledge_kind':'solution',
      'metadata':dict.fromkeys(('tool','tool_version','error_code','environment','verification'),'')}
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(
    json.dumps({'decision':'candidate','reason':'Local fixture','candidate':body}),encoding='utf-8')
'''


PacketRuntime = Runtime if issubclass(Runtime, ReviewPacketMixin) else type(
    "PacketRuntime", (ReviewPacketMixin, Runtime), {})


class Module5PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module5-packet-")
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.r = PacketRuntime(self.home)
        self.p = self.r.project(self.workspace)
        self.reviewer = Path(self.temp.name) / "isolated-packet-reviewer.py"
        self.reviewer.write_text(_REVIEWER, encoding="utf-8")

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def configure(self, limit=16000):
        self.r.config["admin"].update(enabled=True, command=[sys.executable, str(self.reviewer)],
                                       max_calls_per_day=5, max_input_chars=limit, timeout_seconds=5)

    def problem(self, summary="A short report", **kwargs):
        return self.r.feedback(self.p, "chat", summary, category="operation", **kwargs)

    def test_41_short_events_are_complete_and_reach_local_reviewer(self):
        self.configure()
        for i in range(41):
            problem = self.problem(resolution=f"Observation {i}")
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertEqual(packet["evidence_count"], 41)
        self.assertEqual(len(packet["events"]), 41)
        self.assertFalse(packet["incomplete"])
        self.assertEqual(packet["omitted_count"], 0)
        self.assertLessEqual(len(review_instruction(packet)), 16000)
        self.assertEqual(admin_once(self.r, self.p, problem["id"])["status"], "candidate")

    def test_actual_small_input_fits_2200_character_budget(self):
        self.configure(2200)
        problem = self.problem(summary="x")
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertEqual(len(packet["events"]), 1)
        self.assertFalse(packet["incomplete"])
        self.assertLessEqual(len(review_instruction(packet)), 2200)
        self.assertEqual(admin_once(self.r, self.p, problem["id"])["status"], "candidate")

    def test_large_scope_is_explicit_and_historical_json_is_pageable(self):
        self.configure()
        task = self.r.task_update(self.p, "chat", "Review path issue", {"source_note": "x" * 18000})
        problem = self.problem()
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertTrue(packet["task_truncated"])
        self.assertTrue(packet["incomplete"])
        self.assertTrue(packet["task"]["scope_truncated"])
        self.assertIsNone(packet["task"]["scope"])
        self.assertLessEqual(len(review_instruction(packet)), 16000)
        content, offset, digest = [], 0, None
        while True:
            page = self.r.review_packet(self.p, problem["id"], offset=offset,
                                        expected_revision=problem["revision"], max_chars=1500, section="task")
            self.assertLessEqual(len(review_instruction(page)), 1500)
            content.append(page["task_page"]["content"])
            digest = digest or page["task_page"]["digest"]
            self.assertEqual(digest, page["task_page"]["digest"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        restored = json.loads("".join(content))
        self.assertEqual(restored["scope"], task["scope"])
        self.assertEqual(restored["id"], task["id"])
        with patch("agent_learning.admin.subprocess.Popen", side_effect=AssertionError("incomplete must not start")):
            self.assertEqual(admin_once(self.r, self.p, problem["id"])["status"], "needs_evidence")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_pagination_checks_problem_revision(self):
        problem = self.problem()
        for section in ("task", "events"):
            with self.assertRaises(ContractError):
                self.r.review_packet(self.p, problem["id"], offset=1, section=section)
        self.problem(resolution="new evidence")
        with self.assertRaises(ContractError):
            self.r.review_packet(self.p, problem["id"], expected_revision=problem["revision"])

    def test_missing_historical_state_is_not_complete_evidence(self):
        self.r.task_update(self.p, "chat", "Historical task")
        problem = self.problem()
        self.r.db.execute("DELETE FROM task_states WHERE project=?", (self.p,))
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertTrue(packet["task_snapshot_missing"])
        self.assertTrue(packet["incomplete"])
        self.assertIn("source_task_snapshot_missing", packet["omission_reasons"])
        with self.assertRaises(ContractError):
            self.r.review_packet(self.p, problem["id"], section="task")

    def test_first_page_always_makes_progress_when_one_equal_event_fits(self):
        for i in range(3):
            problem = self.problem(resolution=str(i) + "x" * 400)
        # 等长输入包含时间字段；真实时钟的浮点精度不能决定本用例的页面长度。
        self.r.db.execute("UPDATE events SET created=1700000000.0 WHERE project=?", (self.p,))
        full = self.r.review_packet(self.p, problem["id"])
        # 按真实一个事件的页面开销求取预算，不依赖固定预留。
        limit = 1000
        while limit <= 4000:
            first = self.r.review_packet(self.p, problem["id"], max_chars=limit)
            if len(first["events"]) == 1:
                break
            limit += 25
        self.assertLessEqual(limit, 4000)
        self.assertEqual(first["events"][0]["id"], full["events"][0]["id"])
        self.assertEqual(first["next_offset"], 1)
        shown = {first["events"][0]["id"]}
        offset = first["next_offset"]
        while offset is not None:
            page = self.r.review_packet(self.p, problem["id"], offset=offset,
                                        expected_revision=problem["revision"], max_chars=limit)
            shown.update(event["id"] for event in page["events"])
            self.assertTrue(page["next_offset"] is None or page["next_offset"] > offset)
            offset = page["next_offset"]
        self.assertEqual(shown, {event["id"] for event in full["events"]})

    def test_longer_next_event_reports_required_budget_and_resumes_without_loss(self):
        for i in range(3):
            problem = self.problem(resolution=str(i) + "x" * 400)
        rows = self.r.db.execute("SELECT id FROM events WHERE project=? ORDER BY created,id",
                                 (self.p,)).fetchall()
        for row, created in zip(rows, (1700000000.0, 1700000001.1234567, 1700000002.1234567)):
            self.r.db.execute("UPDATE events SET created=? WHERE id=?", (created, row["id"]))
        self.r.db.execute("UPDATE problems SET created=? WHERE id=?", (1700000000.1234567, problem["id"]))
        full = self.r.review_packet(self.p, problem["id"])
        # 找首页首条完整证据的最小预算，避免把格式开销硬编码进保护回归。
        low, high = 1000, 4000
        self.assertTrue(self.r.review_packet(self.p, problem["id"], max_chars=high)["events"])
        while low < high:
            middle = (low + high) // 2
            page = self.r.review_packet(self.p, problem["id"], max_chars=middle)
            if page["events"]:
                high = middle
            else:
                low = middle + 1
        first = self.r.review_packet(self.p, problem["id"], max_chars=low)
        self.assertEqual([event["id"] for event in first["events"]], [full["events"][0]["id"]])
        self.assertEqual(first["next_offset"], 1)
        blocked = self.r.review_packet(self.p, problem["id"], offset=1,
                                       expected_revision=problem["revision"], max_chars=low)
        self.assertEqual(blocked["events"], [])
        self.assertEqual(blocked["next_offset"], 1)
        self.assertTrue(blocked["incomplete"])
        self.assertEqual(blocked["pagination_blocked"], "increase_max_chars")
        self.assertGreater(blocked["required_chars"], low)
        self.assertLessEqual(len(review_instruction(blocked)), low)
        resumed = self.r.review_packet(self.p, problem["id"], offset=1,
                                       expected_revision=problem["revision"], max_chars=blocked["required_chars"])
        self.assertEqual(resumed["events"], [full["events"][1]])
        self.assertEqual(resumed["next_offset"], 2)
        self.assertLessEqual(len(review_instruction(resumed)), blocked["required_chars"])
        last = self.r.review_packet(self.p, problem["id"], offset=2,
                                    expected_revision=problem["revision"], max_chars=blocked["required_chars"])
        self.assertEqual(last["events"], [full["events"][2]])
        self.assertIsNone(last["next_offset"])
        self.assertEqual(first["events"] + resumed["events"] + last["events"], full["events"])

    def test_role_priority_and_continuation_preserve_all_event_ids(self):
        self.configure(2500)
        refs = []
        for i in range(8):
            e = self.r.record_event(self.p, "chat", "PostToolUse", {"tool_name": "compiler", "output": "x" * 450},
                                    outcome="failed" if i == 0 else "unknown")
            refs.append(e["id"])
            self.r.db.execute("UPDATE events SET created=? WHERE id=?", (i + 1, e["id"]))
        problem = self.problem(event_refs=refs, resolution="The final explanation")
        self.r.db.execute("UPDATE problem_events SET role='failure' WHERE event=?", (refs[0],))
        feedback = self.r.db.execute("SELECT event FROM problem_events WHERE problem=? AND event NOT IN (" +
                                     ",".join("?" for _ in refs) + ")", (problem["id"], *refs)).fetchone()[0]
        self.r.db.execute("UPDATE problem_events SET role='feedback' WHERE event=?", (feedback,))
        self.r.db.execute("UPDATE events SET created=9 WHERE id=?", (feedback,))
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertTrue(packet["incomplete"])
        self.assertLessEqual(len(review_instruction(packet)), 2500)
        shown = {event["id"] for event in packet["events"]}
        self.assertIn(refs[0], shown)
        self.assertIn(feedback, shown)
        offset = packet["next_offset"]
        while offset is not None:
            page = self.r.review_packet(self.p, problem["id"], offset=offset,
                                        expected_revision=problem["revision"], max_chars=4000)
            shown.update(event["id"] for event in page["events"])
            next_offset = page["next_offset"]
            self.assertTrue(next_offset is None or next_offset > offset)
            offset = next_offset
        self.assertEqual(shown, set(refs + [feedback]))

    def test_bounded_window_is_not_misreported_as_complete(self):
        self.configure(200000)
        for i in range(520):
            problem = self.problem(resolution=str(i))
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertTrue(packet["incomplete"])
        self.assertLessEqual(len(packet["events"]), 518)
        self.assertEqual(packet["omitted_count"], 520 - len(packet["events"]))
        self.assertIsNotNone(packet["next_offset"])
        with patch("agent_learning.admin.subprocess.Popen", side_effect=AssertionError("bounded incomplete must not start")):
            self.assertEqual(admin_once(self.r, self.p, problem["id"])["status"], "needs_evidence")

    def test_current_operation_health_is_in_packet_and_fingerprint(self):
        self.configure()
        self.r.task_update(self.p, "chat", "Check compiler")
        base = {"session_id": "chat", "cwd": str(self.workspace), "tool_name": "compiler", "tool_use_id": "op"}
        self.r.handle_hook(dict(base, hook_event_name="PreToolUse"))
        self.r.handle_hook(dict(base, hook_event_name="PostToolUse", tool_response={"exit_code": 0}))
        event = self.r.db.execute("SELECT id,operation FROM events WHERE type='PostToolUse'").fetchone()
        problem = self.problem(event_refs=[event["id"]])
        before = self.r.review_packet(self.p, problem["id"])
        result = next(e for e in before["events"] if e["id"] == event["id"])
        self.assertEqual(result["health"], "valid")
        self.assertTrue(result["current_tool_success"])
        self.r.db.execute("UPDATE operations SET conflict=1 WHERE project=? AND id=?", (self.p, event["operation"]))
        after = self.r.review_packet(self.p, problem["id"])
        result = next(e for e in after["events"] if e["id"] == event["id"])
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["health"], "conflicted")
        self.assertFalse(result["current_tool_success"])
        self.assertNotEqual(_fingerprint(before), _fingerprint(after))

    def test_actor_and_assignment_relations_participate_without_transport_ids(self):
        packet = {"problem": {"summary": "Same", "category": "operation"}, "task": {},
                  "events": [{"id": "one", "type": "Feedback", "outcome": "reported", "payload": {},
                              "agent": "old-child", "assignment_id": "old-assignment", "role": "feedback"},
                             {"id": "two", "type": "Feedback", "outcome": "reported", "payload": {"resolution": "ok"},
                              "agent": "old-child", "assignment_id": "old-assignment", "role": "feedback"}]}
        renamed = copy.deepcopy(packet)
        for event in renamed["events"]:
            event.update(agent="new-child", assignment_id="new-assignment")
        self.assertEqual(_fingerprint(packet), _fingerprint(renamed))
        renamed["events"][1]["assignment_id"] = "different-assignment"
        self.assertNotEqual(_fingerprint(packet), _fingerprint(renamed))
        changed = copy.deepcopy(packet)
        changed["events"][0]["role"] = "delivery"
        self.assertNotEqual(_fingerprint(packet), _fingerprint(changed))
        changed = copy.deepcopy(packet)
        changed["problem"]["knowledge_revision"] = 2
        self.assertNotEqual(_fingerprint(packet), _fingerprint(changed))

    def test_equal_timestamp_order_is_deterministic(self):
        for i in range(4):
            problem = self.problem(resolution=str(i))
        self.r.db.execute("UPDATE events SET created=1 WHERE type='Feedback'")
        first = self.r.review_packet(self.p, problem["id"])
        second = self.r.review_packet(self.p, problem["id"])
        ids = [event["id"] for event in first["events"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [event["id"] for event in second["events"]])
        self.assertEqual(_fingerprint(first), _fingerprint(second))

    def test_candidate_related_reuses_strong_match_gate(self):
        source = self.problem("检查电子表格")
        event_id = self.r.review_packet(self.p, source["id"])["events"][0]["id"]
        self.r.submit_candidate(self.p, problem_id=source["id"], problem_revision=source["revision"],
                                title="检查电子表格", content="Excel表格检查", category="operation",
                                applicability="Excel", limitations="No acceptance", evidence_refs=[event_id],
                                validation_level="reported")
        current = self.problem("查询C++崩溃")
        packet = self.r.review_packet(self.p, current["id"])
        self.assertEqual(packet["related_candidates"], [])


if __name__ == "__main__":
    unittest.main()
