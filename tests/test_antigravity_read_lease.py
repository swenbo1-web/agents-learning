"""Read-only AG lease renewal never replays actions or crosses project identity."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests import test_antigravity_panel_recovery as recovery


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "host_resources/antigravity/sidecars/control/index.html"


def replaced(source, old, new):
    assert source.count(old) == 1, old
    return source.replace(old, new)


HARNESS = replaced(recovery.HARNESS, "const scenario=process.argv[1];", "const scenario=process.argv[1];let clock=0;")
HARNESS = replaced(HARNESS, "const context={window,document,", "const context={performance:{now:()=>clock},window,document,")
HARNESS = replaced(HARNESS, "navigator:{language:'en'}", "navigator:{language:scenario==='lease_success_zh'?'zh-CN':'en'}")
HARNESS = replaced(HARNESS, "expected:{expected_project_budget_version:1}",
                   "host_scope:'antigravity:default',expected:{expected_project_budget_version:1,expected_session_epoch:2,expected_governance_epoch:3}")
HARNESS = replaced(HARNESS, "({visible:true,capability:cap,summary})", "({visible:true,capability:cap,summary,installation_fingerprint:'a'.repeat(64)})")
HARNESS = replaced(HARNESS, "runInNewContext(script,context);", r'''runInNewContext(script.replace(
 '  function changePreset(preset) {',
 '  window.__statusRead = () => action("status");\n  function changePreset(preset) {'),context);''')

SCENARIOS = r'''
 if(scenario.startsWith('lease_')){
   if(scenario==='lease_clock_rollback')clock=500000;
   if(scenario==='lease_clock_missing')delete context.performance;
   if(scenario==='lease_dispatch_clock'||scenario==='lease_initial_fingerprint_missing'||scenario==='lease_initial_identity_missing'){
     await flush();assert.equal(requests.length,1);clock=100000;reply(requests[0],valid());await flush();
   }else await boot();
   if(scenario==='lease_fresh_read'||scenario==='lease_unknown_read_error'){
     clock=239999;
     await click('debug');assert.equal(requests.length,2);assert.equal(requests[1].body.action,'debug');
     if(scenario==='lease_unknown_read_error')requests[1].reject(new Error('unknown network failure'));
     else reply(requests[1],{visible:true,summary:snapshot(),diagnostics:{version:1,project:{id:'p1'}}});
     await flush();assert.equal(requests.length,2,'a normal read failure must not retry or bootstrap');
     assert.equal(elements.panel.hidden,scenario==='lease_unknown_read_error');
   }else if(scenario==='lease_initial_fingerprint_missing'||scenario==='lease_initial_identity_missing'){
     // A real legacy component remains usable; automatic renewal requires stronger identity.
     await click('refresh');const bootstrap=requests.at(-1),legacy=valid('legacy-cap');
     if(scenario==='lease_initial_fingerprint_missing')delete legacy.installation_fingerprint;
     else delete legacy.summary.host_scope;
     reply(bootstrap,legacy);await flush();clock=480002;const count=requests.length;
     await click('debug');assert.equal(requests.length,count,'incomplete identity must not cause automatic renewal/read');
     assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);
     const next=await reconnect();reply(next,valid('explicitly-rebound'));await flush();assert.equal(elements.panel.hidden,false);
   }else if(scenario.startsWith('lease_old_write')){
     clock=240001;const control=scenario==='lease_old_write_browser'?'browser':scenario==='lease_old_write_review'?'review':'history';
     if(scenario==='lease_old_write_preset'){elements.presets.children.find(button=>button.textContent==='Balanced · 100/day').click();await flush();}
     else await click(control);assert.equal(requests.length,2);
     const expected=scenario==='lease_old_write_preset'?'set_admin_preset':control==='history'?'archive_history':'open_'+control;
     assert.equal(requests[1].body.action,expected);requests[1].reject(new Error('unknown write result'));
     await flush();assert.equal(requests.length,2,'an expired mutation must never bootstrap or replay');
     assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);
   }else if(scenario==='lease_fresh_scope_microtask'){
     elements.debug.click();currentUris=[otherUri];window.sidecar.conversationId='native-other';changed(currentUris);await flush();
     assert.equal(requests.length,2);assert.equal(requests[1].url,'/api/bootstrap');
     assert.equal(requests.filter(r=>r.body.action).length,0,'fresh read crossed its await boundary');
     reply(requests[1],valid('other-cap',snapshot('other','p2')));await flush();assert.equal(elements.project.textContent,'other');
   }else if(scenario==='lease_clock_rollback'||scenario==='lease_clock_missing'){
     clock=scenario==='lease_clock_rollback'?499999:240001;
     await click('debug');assert.equal(requests.length,2);assert.equal(requests[1].url,'/api/action');
     requests[1].reject(new Error('unknown age'));await flush();assert.equal(requests.length,2);
   }else{
     clock=240001;
     if(scenario==='lease_sdk_changed')currentUris=[otherUri];
     if(scenario==='lease_sdk_hung'){reader=()=>new Promise(()=>{});}
     if(scenario==='lease_status'){window.__statusRead();await flush();}else await click('debug');
     if(scenario==='lease_sdk_changed'){
       assert.equal(requests.length,1,'workspace mismatch must be caught before bootstrap');
       assert.equal(elements.panel.hidden,true);
     }else if(scenario==='lease_sdk_hung'){
       assert.equal(requests.length,1);const deadline=[...timers.values()].find(x=>x.delay===15000);assert(deadline);
       deadline.fn();await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);
     }else{
       assert.equal(requests.length,2);const renewal=requests[1];
       assert.equal(renewal.url,'/api/bootstrap','old read lease must be renewed before sending its action');
       assert(elements.notice.textContent.includes(scenario==='lease_success_zh'?'正在刷新本地只读连接':'Refreshing the local read connection'));
       assert.equal(renewal.body.capability,undefined);assert.equal(renewal.body.conversation_id,'native-current');
       assert.deepEqual(renewal.body.workspace_uris,[uri]);
       elements.debug.click();elements.browser.click();await flush();assert.equal(requests.length,2,'renewal must stay single-flight');
       if(scenario==='lease_bootstrap_failed'||scenario==='lease_uninstalled'){
         if(scenario==='lease_bootstrap_failed')renewal.reject(new Error('unknown bootstrap failure'));
         else reply(renewal,{visible:false,reason:'workspace_not_bound'});
         await flush();assert.equal(requests.length,2);assert.equal(elements.panel.hidden,true);
       }else if(scenario==='lease_bootstrap_timeout'||scenario==='lease_bootstrap_json_timeout'){
         let finishJson;
         if(scenario==='lease_bootstrap_json_timeout')renewal.resolve({ok:true,json:()=>new Promise(resolve=>{finishJson=resolve;})});
         await flush();const deadline=[...timers.values()].find(x=>x.delay===15000);assert(deadline,'read/bootstrap needs a bounded deadline');
         deadline.fn();await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);
         if(finishJson)finishJson(valid('late-cap'));else reply(renewal,valid('late-cap'));
         await flush();assert.equal(requests.length,2,'late renewal must not launch a read');assert.equal(elements.panel.hidden,true);
       }else if(scenario==='lease_workspace_switch'||scenario==='lease_pagehide'||scenario==='lease_conversation_switch'){
         if(scenario==='lease_pagehide')await pageEvent('pagehide');
         else if(scenario==='lease_conversation_switch')window.sidecar.conversationId='changed-without-workspace-event';
         else {currentUris=[otherUri];window.sidecar.conversationId='native-other';changed(currentUris);await flush();}
         reply(renewal,valid('late-old-cap'));await flush();
         assert.equal(requests.filter(r=>r.body.action==='debug').length,0,'late renewal crossed the original boundary');
         assert.equal(elements.panel.hidden,true);
         if(scenario==='lease_workspace_switch'){
           assert.equal(requests.length,3);reply(requests[2],valid('other-cap',snapshot('other','p2')));await flush();
           assert.equal(elements.project.textContent,'other');assert.equal(elements.panel.hidden,false);
         }
       }else{
         const next=snapshot();
         if(scenario==='lease_project_changed')next.project_id='other-project';
         if(scenario==='lease_workspace_changed')next.workspace='different workspace';
         if(scenario==='lease_host_changed')next.host_scope='antigravity:other';
         if(scenario==='lease_session_changed')next.expected.expected_session_epoch++;
         if(scenario==='lease_governance_changed')next.expected.expected_governance_epoch++;
         if(scenario==='lease_identity_missing')delete next.host_scope;
         if(scenario==='lease_settings_changed')Object.assign(next.expected,{expected_project_budget_version:99,expected_policy_version:99,expected_settings_version:99});
         const bound=valid('renewed-cap',next);
         if(scenario==='lease_installation_changed')bound.installation_fingerprint='b'.repeat(64);
         if(scenario==='lease_fingerprint_missing')delete bound.installation_fingerprint;
         if(scenario==='lease_fingerprint_invalid')bound.installation_fingerprint='not-a-receipt';
         reply(renewal,bound);await flush();
         if(!['lease_success','lease_success_zh','lease_status','lease_dispatch_clock','lease_settings_changed','lease_renewed_read_error','lease_read_timeout'].includes(scenario)){
           assert.equal(requests.length,2,'changed identity must not continue the read');assert.equal(elements.panel.hidden,true);
         }else{
           assert.equal(requests.length,3);const read=requests[2];assert.equal(read.url,'/api/action');
           assert.equal(read.body.action,scenario==='lease_status'?'status':'debug');assert.equal(read.body.capability,'renewed-cap');
           assert.equal(read.body.request_id,undefined,'read renewal must not create write identities');
           if(scenario==='lease_renewed_read_error'){
             requests[2].reject(new Error('unknown renewed read failure'));await flush();assert.equal(elements.panel.hidden,true);
             assert.equal(requests.length,3,'a renewed read failure must not start another bootstrap');
           }else if(scenario==='lease_read_timeout'){
             const deadline=[...timers.values()].find(x=>x.delay===15000);assert(deadline);deadline.fn();await flush();
             assert.equal(elements.panel.hidden,true);reply(read,{visible:true,summary:snapshot('late')});await flush();
             assert.equal(elements.panel.hidden,true);assert.equal(requests.length,3,'timed-out read was retried');
           }else{
             reply(read,{visible:true,summary:snapshot(),...(scenario==='lease_status'?{}:{diagnostics:{version:1,project:{id:'p1'}}})});await flush();
             assert.equal(elements.panel.hidden,false);assert.equal(elements.debugPanel.hidden,scenario==='lease_status');
             assert.equal(elements.reconnect.hidden,true);assert.equal(requests.length,3);
             await click('debug');assert.equal(requests.length,4);assert.equal(requests[3].body.capability,'renewed-cap');
             reply(requests[3],{visible:true,summary:snapshot()});await flush();assert.equal(timers.size,0);
           }
         }
       }
     }
   }
   assert(!requests.some(r=>r.body.action&&!['status','debug','archive_history','open_browser','open_review','set_admin_preset'].includes(r.body.action)));
 }else
'''
HARNESS = replaced(HARNESS, " if(scenario==='initial_uninstalled'){", SCENARIOS + " if(scenario==='initial_uninstalled'){")


@unittest.skipUnless(shutil.which("node"), "Node.js required for AG read lease checks")
class AntigravityReadLeaseTests(unittest.TestCase):
    setUp = recovery.AntigravityPanelRecoveryTests.setUp

    def scenario(self, name):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ALP_")}
        env.update(ALP_HOME=str(self.area / "home"), ALP_CONFIG=str(self.config),
                   ALP_RUNTIME_STORE=str(self.area / "runtime-releases"), CODEX_HOME=str(self.area / "codex"),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT), TMP=str(self.area / "tmp"),
                   TEMP=str(self.area / "tmp"), TMPDIR=str(self.area / "tmp"), ALP_RECOVERY_HTML=str(HTML),
                   ALP_RECOVERY_WORKSPACE=str(self.workspace), ALP_RECOVERY_OTHER=str(self.other))
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, name], env=env, cwd=self.area,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_old_read_lease_renews_once_and_resumes_only_debug(self):
        for name in ("lease_success", "lease_success_zh", "lease_dispatch_clock"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_status_uses_the_same_read_only_lease_path(self):
        self.scenario("lease_status")

    def test_fresh_read_and_unknown_error_never_renew(self):
        for name in ("lease_fresh_read", "lease_unknown_read_error"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_old_mutation_is_never_rebound_or_replayed(self):
        for name in ("lease_old_write", "lease_old_write_browser", "lease_old_write_review", "lease_old_write_preset"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_missing_or_backwards_clock_never_assumes_safe_renewal(self):
        for name in ("lease_clock_missing", "lease_clock_rollback"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_current_sdk_workspace_must_match_the_original_scope(self):
        self.scenario("lease_sdk_changed")

    def test_sdk_read_timeout_does_not_continue_the_read(self):
        self.scenario("lease_sdk_hung")

    def test_bootstrap_failure_or_uninstall_never_retries(self):
        for name in ("lease_bootstrap_failed", "lease_uninstalled"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_bootstrap_fetch_and_json_deadline_discard_late_data(self):
        for name in ("lease_bootstrap_timeout", "lease_bootstrap_json_timeout"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_workspace_conversation_and_pagehide_boundaries_cancel_old_renewal(self):
        for name in ("lease_workspace_switch", "lease_pagehide", "lease_conversation_switch"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_changed_or_incomplete_identity_never_resumes_the_read(self):
        for name in ("lease_project_changed", "lease_workspace_changed", "lease_host_changed",
                     "lease_session_changed", "lease_governance_changed", "lease_identity_missing"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_read_timeout_has_no_retry_or_late_repaint(self):
        self.scenario("lease_read_timeout")

    def test_read_failure_after_renewal_never_repeats_bootstrap(self):
        self.scenario("lease_renewed_read_error")

    def test_fresh_read_cannot_cross_its_await_microtask_scope_boundary(self):
        self.scenario("lease_fresh_scope_microtask")

    def test_policy_and_budget_updates_do_not_masquerade_as_identity_changes(self):
        self.scenario("lease_settings_changed")

    def test_missing_initial_identity_requires_explicit_reconnection(self):
        for name in ("lease_initial_fingerprint_missing", "lease_initial_identity_missing"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_installation_fingerprint_must_remain_valid_and_identical(self):
        for name in ("lease_installation_changed", "lease_fingerprint_missing", "lease_fingerprint_invalid"):
            with self.subTest(name=name):
                self.scenario(name)


if __name__ == "__main__":
    unittest.main()
