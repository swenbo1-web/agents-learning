"""Protected old calls are distinct from current runnable work and live execution."""
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from agent_learning.admin_queue import dispatch_diagnostic
from agent_learning.phase_review import configure
from agent_learning.status_output import MAX_MODEL_CHARS, status_summary
from agent_learning.browser_ui import JAVASCRIPT
from tests import test_admin_recovery as recovery_fixtures
from tests import test_admin_recovery_classification as classification_fixtures
from tests import test_panel_recovery_reason_ui as panel_fixtures
from tests.test_admin_browser_loading import HARNESS as ADMIN_HARNESS
from tests.test_status_output import fixture, wire_size
from tests.test_panel_ui import HARNESS as PANEL_HARNESS


class ProtectedSchedulerStatusTests(unittest.TestCase):
    setUp = recovery_fixtures.AdminRecoveryTests.setUp
    failure = classification_fixtures.RecoveryClassificationTests.failure

    def scheduler_state(self):
        config = {**self.r.config["scheduler"], "enabled": True}
        before = self.r.db.total_changes
        with patch("subprocess.Popen") as launch:
            result = self.r._scheduler_eval_admin_for_scope(self.p, config, self.r._session_scope())
        launch.assert_not_called()
        self.assertEqual(self.r.db.total_changes, before)
        return result

    def test_other_host_failure_does_not_make_this_scheduler_wait_for_review(self):
        self.failure("timeout_unknown", host_scope="antigravity:default")
        self.r.admin_configure(self.p, enabled=True)
        _, state, due, reason = self.scheduler_state()
        self.assertEqual(state, "idle")
        self.assertIsNone(due)
        self.assertNotEqual(reason, "review_required")

    def test_current_legacy_case_still_waits_without_automatic_retry(self):
        call = self.failure("timeout_unknown")
        self.r.admin_configure(self.p, enabled=True)
        _, state, due, reason = self.scheduler_state()
        self.assertEqual((state, due, reason), ("waiting", None, "review_required"))
        self.assertEqual(self.r.db.execute("SELECT retry_allowed FROM admin_calls WHERE id=?", (call,)).fetchone()[0], 0)

    def test_old_case_protection_under_phase_policy_is_idle_and_preserved(self):
        call = self.failure("orphaned_unknown")
        self.r.admin_configure(self.p, enabled=True)
        configure(self.r, self.p, {"mode": "phase", "every_phases": 1}, 0)
        before = tuple(self.r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?", (call,)).fetchone())
        _, state, due, reason = self.scheduler_state()
        self.assertEqual((state, due, reason), ("idle", None, "protected_history_only"))
        diagnostic = dispatch_diagnostic(self.r, self.p)
        self.assertFalse(diagnostic["queue_blocked_by_protected_calls"])
        self.assertEqual(tuple(self.r.db.execute("SELECT state,retry_allowed,usage FROM admin_calls WHERE id=?", (call,)).fetchone()), before)


class ProtectedSummaryTests(unittest.TestCase):
    def test_public_delivery_reason_uses_only_the_fixed_enum(self):
        from agent_learning.antigravity_health import DELIVERY_RECOVERY_REASONS
        for reason in DELIVERY_RECOVERY_REASONS:
            with self.subTest(reason=reason):
                original = {"host_scope": "antigravity:default", "host_capture": {
                    "state": "observed", "public_delivery": "unavailable", "delivery_recovery_reason": reason}}
                result = status_summary(original)
                self.assertEqual(result["host_capture"]["delivery_recovery_reason"], reason)
                self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)

    def test_public_delivery_summary_never_echoes_arbitrary_reason_or_path(self):
        for reason in ("private-token /private/transcript/path", None, 42, {"private": "body"}):
            with self.subTest(reason=reason):
                original = {"host_scope": "antigravity:default", "host_capture": {
                    "state": "observed", "public_delivery": "unavailable", "delivery_recovery_reason": reason}}
                result = status_summary(original)
                self.assertNotIn("delivery_recovery_reason", result["host_capture"])
                self.assertNotIn("private", json.dumps(result))

    def test_summary_keeps_current_protection_boolean_not_historical_activity(self):
        original = fixture()
        original["admin_dispatch_diagnostic"] = {"phase": "idle", "reason": "protected_history_only",
            "queue_blocked_by_protected_calls": False,
            "last_observation": {"reason": "model_started", "details": "private historical data"}}
        result = status_summary(original)
        self.assertEqual(result["admin_dispatch_diagnostic"]["reason"], "protected_history_only")
        self.assertIs(result["admin_dispatch_diagnostic"]["queue_blocked_by_protected_calls"], False)
        self.assertNotIn("private historical data", json.dumps(result))
        self.assertLessEqual(wire_size(result), MAX_MODEL_CHARS)

    def test_unknown_or_malformed_protection_is_not_a_false_boolean(self):
        for value in (None, "private invalid field", 0):
            with self.subTest(value=value):
                original = fixture()
                original["admin_dispatch_diagnostic"]["queue_blocked_by_protected_calls"] = value
                result = status_summary(original)
                self.assertIsNone(result["admin_dispatch_diagnostic"]["queue_blocked_by_protected_calls"])
                self.assertNotIn("private invalid field", json.dumps(result))
        original = fixture()
        self.assertNotIn("queue_blocked_by_protected_calls", status_summary(original)["admin_dispatch_diagnostic"])

    def test_new_field_does_not_expand_large_unicode_status_envelope(self):
        original = fixture()
        original["home"] = "\U0001f680" * 3000
        original["admin_dispatch_diagnostic"]["queue_blocked_by_protected_calls"] = True
        self.assertLessEqual(wire_size(status_summary(original)), MAX_MODEL_CHARS)


class ProtectedStatusUITests(unittest.TestCase):
    LABELS = {
        "zh-CN": {"protected_history_only": "现有调用保护仍保留，当前没有可直接送审的新工作；待处置阶段另列，旧调用不会自动重试。"},
        "en": {"protected_history_only": "Existing call protections remain; no new work is directly reviewable. Stages awaiting action are listed separately; old calls will not retry automatically."},
    }

    def exercise_control(self, method):
        case = panel_fixtures.PanelRecoveryReasonUITests(method)
        with patch.object(panel_fixtures, "LABELS", copy.deepcopy(self.LABELS)):
            getattr(case, method)()

    def test_codex_card_labels_old_protection_without_retrying(self):
        self.exercise_control("test_codex_card_translates_summary_and_debug_without_actions")

    def test_ag_sidecar_labels_old_protection_without_retrying(self):
        self.exercise_control("test_antigravity_sidecar_translates_summary_and_debug")

    def test_dsh_panel_labels_old_protection_without_retrying(self):
        self.exercise_control("test_dsh_native_panel_displays_and_translates_diagnostic_reason")

    def run_node(self, code, script, scenario="capture_recovery"):
        if not shutil.which("node"):
            self.skipTest("Node.js required to execute the shipped UI")
        result = subprocess.run([shutil.which("node"), "-e", code],
            input=json.dumps({"script": script, "scenario": scenario}),
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_browser_capture_explanation_is_localized_and_boundary_checked(self):
        code = ADMIN_HARNESS.split("(async()=>{")[0] + r'''
const capture={version:1,scope:'project_recent_capture',host_scope:'antigravity:default',state:'observed',public_delivery:'unavailable',delivery_recovery_reason:'stop_transcript_not_terminal'};
const read=value=>run('captureRecoveryExplanation('+JSON.stringify(value)+',"antigravity:default")');
for(const language of ['en','zh-CN']){
 run('state.language='+JSON.stringify(language));
 assert(read(capture).includes(language==='en'?'complete terminal output':'完整终态'));
 const unknown=read({...capture,delivery_recovery_reason:'private-token /private/path'});
 assert(unknown.includes(language==='en'?'next complete invocation':'下一次完整调用'));
 assert(!unknown.includes('private'));
 assert(read({...capture,delivery_recovery_reason:{toString:'private'}}).includes(language==='en'?'next complete invocation':'下一次完整调用'));
 assert.equal(read({...capture,host_scope:'antigravity:other'}),null);
 assert(!read({...capture,delivery_recovery_reason:'stop_public_output_recovered'}).includes(language==='en'?'was recovered':'已从同一调用'));
}
assert(!calls.some(item=>item.options.method==='POST'));
'''
        self.run_node(code, JAVASCRIPT)

    def test_ag_capture_recovery_explanation_keeps_missing_and_recovered_separate(self):
        html = (Path(__file__).resolve().parents[1] / "host_resources/antigravity/sidecars/control/index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>([\s\S]*)</script>", html).group(1)
        code = r'''
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict'),nodes=new Map();
const element=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false,children:[],listeners:{},dataset:{},replaceChildren(){this.children=[];},append(...v){this.children.push(...v);},addEventListener(k,v){this.listeners[k]=v;}});return nodes.get(id);};
const window={parent:{},addEventListener(){}},document={documentElement:{},getElementById:element,createElement:()=>({dataset:{},addEventListener(){},append(){}}),querySelectorAll:()=>[]};
const at=script.indexOf('  render(); hide("waiting");');assert(at>0);
const context={window,document,navigator:{language:'en'}};vm.createContext(context);
vm.runInContext(script.slice(0,at)+'\n globalThis.show=(s)=>{summary=s;capability="cap";render();};})();',context);
const base={project_id:'p',host_scope:'antigravity:default',counts:{}};
const capture={version:1,scope:'project_recent_capture',host_scope:base.host_scope,state:'observed',public_response:'available',public_delivery:'unavailable'};
for(const language of ['en','zh-CN']){
 const local=()=>{element('language').value=language;element('language').listeners.change();return element('admin').textContent;};
 context.show({...base,host_capture:{...capture,delivery_recovery_reason:'stop_window_unbound'}});
 assert(local().includes(language==='en'?'verifiable complete conversation window':'可核对的完整对话窗口'));
 context.show({...base,host_capture:{...capture,delivery_recovery_reason:'secret /private/path'}});
 assert(local().includes(language==='en'?'next complete invocation':'下一次完整调用'));assert(!local().includes('private'));
 context.show({...base,host_capture:{...capture,delivery_recovery_reason:'stop_public_output_recovered',public_delivery:'complete'}});
 assert(local().includes(language==='en'?'was recovered':'已从同一调用'));
 context.show({...base,host_capture:{...capture,delivery_recovery_reason:'stop_public_output_recovered',state:'pending'}});
 assert(!local().includes(language==='en'?'was recovered':'已从同一调用'));
 context.show({...base,host_capture:{...capture,delivery_recovery_reason:'stop_window_unbound',host_scope:'antigravity:other'}});
 assert(!local().includes(language==='en'?'verifiable complete conversation window':'可核对的完整对话窗口'));
}
'''
        self.run_node(code, script)

    def test_codex_debug_does_not_echo_unrecognized_capture_recovery_reason(self):
        scenario = r'''
if(scenario==='capture_recovery'){
 await boot();
 for(const locale of ['en','zh-CN']){
  element('language').value=locale;element('language').onchange({target:element('language')});
  const s={...snapshot,host_scope:'antigravity:default'};
  let promise=element('refresh').onclick(),request=sent.filter(m=>m.method==='tools/call').at(-1);
  respond(request,result(false,s));await promise;await flush();
  for(const reason of ['stop_input_unverified','private /private/path']){
   promise=element('debug').onclick();request=sent.filter(m=>m.method==='tools/call').at(-1);
   const response=result(false,s);
   response._meta.agentLearningPanel.diagnostics={version:1,project:{id:'p1',host_scope:s.host_scope},capture:{version:1,scope:'project_recent_capture',host_scope:s.host_scope,state:'observed',public_delivery:'unavailable',delivery_recovery_reason:reason}};
   respond(request,response);await promise;await flush();
   const text=element('debugOutput').textContent;assert(!text.includes('/private/path'));
   assert(text.includes(locale==='en'?(reason==='stop_input_unverified'?'input source is unverified':'next complete invocation'):(reason==='stop_input_unverified'?'当前输入来源未确认':'下一次完整调用')));
  }
 }
 assert(!sent.some(m=>['ui/message','ui/update-model-context'].includes(m.method)));
}else '''
        code = PANEL_HARNESS.replace("if(scenario==='bootstrap'){", scenario + "if(scenario==='bootstrap'){")
        code = code.replace("host_scope:'codex:default'", "host_scope:'antigravity:default'")
        from agent_learning.panel_ui import HTML
        script = re.search(r"<script>([\s\S]*)</script>", HTML).group(1)
        self.run_node(code, script)

    @unittest.skipUnless(shutil.which("node"), "Node.js required to execute the shipped UI")
    def test_browser_uses_current_queue_protection_and_live_phase(self):
        code = ADMIN_HARNESS.split("(async()=>{")[0] + r'''
const historical={...core(),diagnostic:{phase:'idle',reason:'protected_history_only',queue_blocked_by_protected_calls:false,
 last_observation:{reason:'model_started'}}};
const evaluate=data=>run('adminReadinessStatus('+JSON.stringify(data)+',true)');
assert.equal(evaluate(historical),'adminIdleProtected');
assert.equal(evaluate({...historical,diagnostic:{phase:'idle',reason:'unknown_call_requires_review',queue_blocked_by_protected_calls:true}}),'adminWaiting');
assert.equal(evaluate({...historical,diagnostic:{phase:'model_started',reason:'model_started'}}),'adminRunning');
assert.equal(evaluate({...historical,enabled:false}),'adminDisabled');
assert.equal(evaluate({...historical,diagnostic:{phase:'idle',reason:'ready'}}),'adminReady');
for(const language of ['en','zh-CN']){
 run('state.language='+JSON.stringify(language));
 const label=run("adminReason('protected_history_only')");
 assert(label.includes(language==='en'?'will not retry automatically':'不会自动重试'));
}
assert(!calls.some(item=>item.options.method==='POST'));
console.log('ok');
'''
        result = subprocess.run([shutil.which("node"), "-e", code],
            input=json.dumps({"script": JAVASCRIPT, "scenario": "protected_status"}),
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
