"""Read-only stage diagnostics preserve retry locks and delivery boundaries."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_lifecycle import claim_call
from agent_learning.admin_queue import dispatch_diagnostic
from agent_learning.phase_review import configure, delivery_registration, protected_backlog, status
from agent_learning.status_output import MAX_MODEL_CHARS, status_summary
from tests import test_phase_host_integration as fixtures
from tests.test_status_output import fixture, wire_size


class PhaseHealthProjectionTests(unittest.TestCase):
    runtime = fixtures.PhaseHostIntegrationTests.runtime
    host_events = fixtures.PhaseHostIntegrationTests.host_events
    deliver = fixtures.PhaseHostIntegrationTests.deliver
    stage = fixtures.PhaseHostIntegrationTests.stage

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-health-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.runtime_count = 0
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()

    def setup_host(self, host="codex", every=1):
        self.runtime_count += 1
        r, p = self.runtime(host, host + "-" + str(self.runtime_count))
        configure(r, p, {"mode": "phase", "every_phases": every}, 0)
        send, c = self.host_events(r)
        send("start", turn=0)
        return r, p, send, c

    def blocked(self, every=1):
        r, p, send, c = self.setup_host(every=every)
        for n in range(1, every + 1):
            self.stage(r, p, send, c, n)
        batch = r.db.execute("SELECT * FROM phase_review_batches WHERE state='pending'").fetchone()
        problem = r.db.execute("SELECT * FROM problems WHERE id=?", (batch["problem_id"],)).fetchone()
        with r.transaction():
            call = claim_call(r, p, problem["id"], problem["revision"], "failure-fixture", r.host_epoch(), 10)
            plan = r.resolve_admin_executor(p, require_enabled=False)
            r.record_admin_execution(p, call["call_id"], plan, require_enabled=False)
            r.db.execute("UPDATE admin_calls SET state='failed',finished=?,lease_until=NULL WHERE id=?",
                         (time.time(), call["call_id"]))
            r.db.execute("UPDATE phase_review_batches SET state='blocked' WHERE problem_id=?", (problem["id"],))
            r.db.execute("DELETE FROM admin_dispatch WHERE project=?", (p,))
        return r, p, batch, call["call_id"]

    def test_one_protected_batch_counts_each_associated_stage_once(self):
        r, p, _, _ = self.blocked(every=2)
        before = r.db.total_changes
        self.assertEqual(protected_backlog(r, p), {"batches": 1, "phases": 2})
        self.assertEqual(status(r, p)["protected_backlog"], {"batches": 1, "phases": 2})
        self.assertEqual(r.db.total_changes, before)

    def test_protected_backlog_does_not_block_new_work_or_grant_retry(self):
        r, p, _, call_id = self.blocked()
        old = tuple(r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?", (call_id,)).fetchone())
        before = r.db.total_changes
        with patch("subprocess.Popen") as model:
            value = dispatch_diagnostic(r, p)
        model.assert_not_called()
        self.assertEqual(value["phase"], "idle")
        self.assertFalse(value["queue_blocked_by_protected_calls"])
        self.assertEqual(value["protected_phase_backlog"], {"batches": 1, "phases": 1})
        self.assertEqual(tuple(r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?", (call_id,)).fetchone()), old)
        self.assertEqual(r.db.total_changes, before)

    def test_other_host_and_old_boundaries_are_excluded(self):
        for field, value in (("host_scope", "dsh:default"), ("session_epoch", -1), ("governance_epoch", -1)):
            r, p, batch, _ = self.blocked()
            r.db.execute("UPDATE phase_review_batches SET " + field + "=? WHERE problem_id=?", (value, batch["problem_id"]))
            self.assertEqual(protected_backlog(r, p), {"batches": 0, "phases": 0})

    def test_explicit_retry_permission_is_not_counted_as_protected(self):
        r, p, _, call = self.blocked()
        r.db.execute("UPDATE admin_calls SET retry_allowed=1 WHERE id=?", (call,))
        self.assertEqual(protected_backlog(r, p), {"batches": 0, "phases": 0})

    def test_local_acceptance_receipt_removes_protected_projection_only(self):
        r, p, batch, call = self.blocked()
        revision = r.db.execute("SELECT revision FROM problems WHERE id=?", (batch["problem_id"],)).fetchone()[0]
        r.db.execute("INSERT INTO review_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p, batch["problem_id"], revision, "local-accepted", "source", call, "skip", "local acceptance", "{}", time.time(), "", "[]", None))
        self.assertEqual(protected_backlog(r, p), {"batches": 0, "phases": 0})
        self.assertEqual(r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (call,)).fetchone()[:], ("failed", 0))

    def test_no_task_reply_is_explained_without_creating_a_stage(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                r, p, send, _ = self.setup_host(host)
                send("stop", turn=2, text="A complete public reply without a registered task")
                before = r.db.total_changes
                value = delivery_registration(r, p)
                self.assertEqual((value["state"], value["reason"]), ("not_registered", "task_missing"))
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 0)
                self.assertEqual(r.db.total_changes, before)

    def test_active_task_reply_is_information_not_a_fabricated_delivery(self):
        r, p, send, c = self.setup_host()
        r.task_update(p, c, "Continue a real unfinished task", new_task=True)
        send("prompt", turn=1)
        send("stop", turn=2, text="Work is still ongoing")
        value = delivery_registration(r, p)
        self.assertEqual(value["reason"], "task_not_delivered")
        self.assertEqual(r.task(p, c)["status"], "active")
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 0)

    def test_registered_stage_and_followup_use_the_original_task_version(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                r, p, send, c = self.setup_host(host)
                self.stage(r, p, send, c, 1)
                self.assertEqual(delivery_registration(r, p)["reason"], "phase_registered")
                send("stop", turn=99, text="A follow-up about the same completed delivery")
                self.assertEqual(delivery_registration(r, p)["reason"], "delivery_already_registered")
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 1)

    def test_partial_new_reply_cannot_borrow_old_complete_registration(self):
        r, p, send, c = self.setup_host("antigravity")
        self.stage(r, p, send, c, 1)
        send("stop", turn=99, text="Partial later output", partial=True)
        value = delivery_registration(r, p)
        self.assertEqual((value["state"], value["reason"]), ("unobserved", "public_reply_unverified"))

    def test_unsealed_stage_is_pending_instead_of_already_registered(self):
        r, p, send, c = self.setup_host()
        self.stage(r, p, send, c, 1)
        r.db.execute("UPDATE review_phases SET state='awaiting_stop',event_id=NULL")
        value = delivery_registration(r, p)
        self.assertEqual((value["state"], value["reason"]), ("pending", "awaiting_capture_seal"))

    def test_resume_between_deliveries_cannot_borrow_an_old_registered_stage(self):
        r, p, send, c = self.setup_host("antigravity")
        self.stage(r, p, send, c, 1)
        first = r.db.execute("SELECT id FROM review_phases").fetchone()[0]
        task = r.task(p, c)
        r.task_update(p, c, task["goal"], resume=True, status="active",
                      expected_task_id=task["id"], expected_write_version=task["write_version"])
        self.deliver(r, p, c)
        send("stop", turn=99, text="A new delivery after a genuine resume")
        r.db.execute("DELETE FROM review_phases WHERE id!=?", (first,))
        self.assertEqual(delivery_registration(r, p)["reason"], "delivery_not_registered")

    def test_light_settings_skip_new_health_queries_and_full_status_reuses_projection(self):
        from agent_learning.admin_control import control_status
        r, p, _, _ = self.blocked()
        statements = []
        r.db.set_trace_callback(statements.append)
        try:
            control_status(r, p, details=False)
            self.assertFalse(any("WITH protected AS" in sql or "ORDER BY rowid DESC LIMIT 128" in sql for sql in statements))
            statements.clear()
            control_status(r, p, details=True)
            self.assertEqual(sum("WITH protected AS" in sql for sql in statements), 1)
            statements.clear()
            r.status(p)
            self.assertEqual(sum("WITH protected AS" in sql for sql in statements), 1)
        finally:
            r.db.set_trace_callback(None)

    def test_browser_loads_phase_health_from_detail_and_retains_it_after_read_failure(self):
        from agent_learning.admin_control import control_status, control_detail
        from agent_learning.browser_ui import JAVASCRIPT
        from tests.test_admin_browser_loading import HARNESS
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        r, p, _, _ = self.blocked()
        core, detail = control_status(r, p, details=False), control_detail(r, p, "queue")
        self.assertNotIn("protected_backlog", core["phase_review"])
        self.assertEqual(detail["data"]["phase_health"]["protected_backlog"], {"batches": 1, "phases": 1})
        harness = HARNESS.split("(async()=>{")[0].replace("const {script,scenario}=", "const {script,scenario,coreData,queueDetail}=")
        code = harness + r'''
let failQueue=false;
context.fetch=async(url,options={})=>{
 assert.notEqual(options.method,'POST');let data;
 if(url.startsWith('/api/admin-control?view=settings'))data=coreData;
 else if(url.startsWith('/api/admin-control/details')){
  const section=new URL(url,'http://local').searchParams.get('section');
  if(section==='queue'&&failQueue)return {ok:false,status:503,json:async()=>({reason:'query_budget_exceeded',error:'query budget exceeded'})};
  data=section==='queue'?queueDetail:{...queueDetail,section,data:section==='activity'?{recent:[],upcoming:[],deferred:[]}:{capture:{},blocked_reviews:[],history_separate:true}};
 }else if(url.startsWith('/api/admin-control/history'))data={items:[],offset:0,next_offset:null};
 else throw Error('Unexpected route '+url);
 return {ok:true,status:200,json:async()=>data};
};
(async()=>{
 await run('loadAdminState(true)');await flush();await flush();
 assert.equal(run('state.adminData.queue.phase_health.protected_backlog.phases'),1);
 assert(get('admin-recovery').children.some(x=>x.textContent.includes('1 protected failed batches')));
 failQueue=true;await run("loadAdminSection('queue',true)");
 assert.equal(run('state.adminData.queue.phase_health.protected_backlog.phases'),1);
 assert(get('admin-queue-details').children.some(x=>x.textContent.includes('paused')));
 assert(get('admin-recovery').children.some(x=>x.textContent.includes('1 protected failed batches')));
 run("state.language='zh-CN';renderAdminState()");
 assert(get('admin-recovery').children.some(x=>x.textContent.includes('1 个受保护失败批次')));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([node, "-e", code], input=json.dumps({"script": JAVASCRIPT, "scenario": "phase_health", "coreData": core, "queueDetail": detail}),
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_thousand_protected_batches_fit_a_linear_sql_work_budget(self):
        r, p, batch, call = self.blocked()
        template_problem = dict(r.db.execute("SELECT * FROM problems WHERE id=?", (batch["problem_id"],)).fetchone())
        template_phase = dict(r.db.execute("SELECT * FROM review_phases").fetchone())
        template_call = dict(r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
        def insert(table, value):
            r.db.execute("INSERT INTO " + table + "(" + ",".join(value) + ") VALUES(" + ",".join("?" for _ in value) + ")", tuple(value.values()))
        with r.transaction():
            for n in range(1100):
                problem_id, phase_id, call_id = "scale-problem-" + str(n), "scale-phase-" + str(n), "scale-call-" + str(n)
                insert("problems", {**template_problem, "id": problem_id})
                insert("review_phases", {**template_phase, "id": phase_id, "task_id": "scale-task-" + str(n)})
                insert("phase_review_batches", {**dict(batch), "problem_id": problem_id, "phase_ids": json.dumps([phase_id]), "state": "blocked"})
                insert("admin_calls", {**template_call, "id": call_id, "problem": problem_id, "fingerprint": "scale-" + str(n)})
        steps = 0
        def bounded():
            nonlocal steps
            steps += 1000
            return steps > 2000000
        r.db.set_progress_handler(bounded, 1000)
        try:
            self.assertEqual(protected_backlog(r, p), {"batches": 1101, "phases": 1101})
            self.assertEqual(status(r, p, include_health=False)["blocked_phase_count"], 1101)
        finally:
            r.db.set_progress_handler(None, 0)

    def test_bounded_tail_does_not_scan_old_project_history(self):
        r, p, send, _ = self.setup_host()
        send("stop", turn=2, text="Old complete public reply")
        with r.transaction():
            for n in range(130):
                r._event(p, "other-activity", "PostToolUse", {"number": n})
        self.assertEqual(delivery_registration(r, p)["state"], "unobserved")

    def test_delivery_projection_never_returns_public_text_or_task_identifiers(self):
        r, p, send, c = self.setup_host()
        send("stop", turn=2, text="private-public-reply marker")
        encoded = json.dumps(delivery_registration(r, p))
        self.assertNotIn("private-public-reply", encoded)
        self.assertNotIn(c, encoded)


class PhaseHealthSummaryAndUiTests(unittest.TestCase):
    def test_summary_keeps_bounded_counts_without_raw_history(self):
        value = fixture()
        value["admin_dispatch_diagnostic"].update(protected_phase_backlog={"batches": 18, "phases": 20, "private": "secret"})
        value["phase_review"] = {"latest_delivery_registration": {"state": "not_registered", "reason": "task_missing", "observed_at": 1, "private": "secret"}}
        result = status_summary(value)
        self.assertEqual(result["admin_dispatch_diagnostic"]["protected_phase_backlog"], {"batches": 18, "phases": 20})
        self.assertEqual(result["latest_delivery_registration"]["reason"], "task_missing")
        self.assertNotIn("secret", json.dumps(result))
        self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)

    def test_summary_never_echoes_arbitrary_registration_reason(self):
        value = fixture()
        value["phase_review"] = {"latest_delivery_registration": {"state": "not_registered", "reason": "private /some/path"}}
        self.assertNotIn("latest_delivery_registration", status_summary(value))

    def test_summary_rejects_malformed_registration_and_negative_or_boolean_counts(self):
        for bad in ({"reason": {}}, {"state": [], "reason": "task_missing"}, {"state": "pending", "reason": []}):
            value = fixture()
            value["phase_review"] = {"latest_delivery_registration": bad}
            self.assertNotIn("latest_delivery_registration", status_summary(value))
        value = fixture()
        value["admin_dispatch_diagnostic"]["protected_phase_backlog"] = {"batches": -1, "phases": True}
        result = status_summary(value)
        self.assertEqual(result["admin_dispatch_diagnostic"]["protected_phase_backlog"], {"batches": None, "phases": None})
        self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)

    def test_all_ui_projection_helpers_show_backlog_and_hide_unknown_reasons(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        root = Path(__file__).resolve().parents[1]
        files = ("agent_learning/browser_ui.py", "agent_learning/panel_ui.py",
                 "host_resources/antigravity/sidecars/control/index.html", "host_resources/dsh/client/index.js")
        program = """const fs=require('fs'),assert=require('assert/strict');const input=JSON.parse(fs.readFileSync(0,'utf8'));
global.text=global.t=(key,args)=>key==='stageProtectedBacklog'?'{batches}/{phases}':key;
const fn=eval('('+input.fn+')'),words={stageProtectedBacklog:'{batches}/{phases}',stageRegistration_task_missing:'task_missing'};
let lines=fn({protected_backlog:{batches:18,phases:20},latest_delivery_registration:{state:'not_registered',reason:'task_missing'}},words);
assert.equal(lines.length,2);assert(lines[1].includes('task_missing'));
assert.equal(fn({protected_backlog:{batches:0,phases:0},latest_delivery_registration:{state:'not_registered',reason:'private/path'}},words).length,0);
assert.equal(fn({protected_backlog:{batches:-1,phases:2}},words).length,0);
"""
        for name in files:
            with self.subTest(file=name):
                source = (root / name).read_text(encoding="utf-8")
                match = re.search(r"function phaseHealthLines\([^\n]+\n", source)
                self.assertIsNotNone(match)
                result = subprocess.run([node, "-e", program], input=json.dumps({"fn": match[0].strip()}),
                                        text=True, encoding="utf-8", capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_dsh_transport_notice_is_typed_and_keeps_restart_limit_explicit(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        path = Path(__file__).resolve().parents[1] / "host_resources/dsh/client/index.js"
        source = path.read_text(encoding="utf-8")
        match = re.search(r"function captureTransportLine\([^\n]+\n", source)
        self.assertIsNotNone(match)
        self.assertIn("宿主重启不能恢复", source)
        self.assertIn("host restart cannot recover", source)
        program = """const fs=require('fs'),assert=require('assert/strict');const input=JSON.parse(fs.readFileSync(0,'utf8'));
const fn=eval('('+input.fn+')'),words={captureTransportPending:'pending={pending},retryable={retryable},unresolved={unresolved}'};
const value={persistence:'bridge_memory_only',pending_events:2,retryable_events:1,unresolved_events:1};
assert.equal(fn(value,words),'pending=2,retryable=1,unresolved=1');
assert.equal(fn({...value,pending_events:0,retryable_events:0,unresolved_events:0},words),'');
assert.equal(fn({...value,retryable_events:3},words),'');
assert.equal(fn({...value,pending_events:'secret'},words),'');
assert.equal(fn({...value,persistence:'unknown'},words),'');
assert.equal(fn({...value,pending_events:0,retryable_events:0},words),'pending=0,retryable=0,unresolved=1');
"""
        result = subprocess.run([node, "-e", program], input=json.dumps({"fn": match[0].strip()}),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
