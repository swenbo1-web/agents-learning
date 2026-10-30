"""Exercise the shipped AG page's recovery without a host, model, or real data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "host_resources/antigravity/sidecars/control/index.html"


HARNESS = r'''
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {pathToFileURL} = require('node:url');
const html=readFileSync(process.env.ALP_RECOVERY_HTML,'utf8');
const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
const scenario=process.argv[1];
class Element {
 constructor(id=''){this.id=id;this.dataset={};this.hidden=false;this.disabled=false;this.children=[];this.events={};this.value='';this.textContent='';this.style={};this.attributes={};}
 addEventListener(name,fn){(this.events[name]??=[]).push(fn);}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[...nodes];}
 setAttribute(name,value){this.attributes[name]=String(value);}
 getAttribute(name){return this.attributes[name]??null;}
 click(){if(!this.disabled)for(const fn of this.events.click||[])fn({type:'click'});}
}
const elements=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element(m[1])]));
for(const match of html.matchAll(/<\w+\b([^>]+)>/g)){
 const id=/\bid="([^"]+)"/.exec(match[1]);
 if(id&&/\bhidden\b/.test(match[1]))elements[id[1]].hidden=true;
}
const document={documentElement:{},getElementById:id=>elements[id],createElement:()=>new Element(),querySelectorAll:selector=>selector==='#panel button'?['refresh','debug','browser','review','history'].map(id=>elements[id]).concat(elements.presets?.children||[]):[]};
const windowEvents={};
const uri=pathToFileURL(process.env.ALP_RECOVERY_WORKSPACE).href;
const otherUri=pathToFileURL(process.env.ALP_RECOVERY_OTHER).href;
let currentUris=[uri],sdkQueries=0,changed;
let reader=()=>Promise.resolve(currentUris.slice());
const requests=[];
const window={parent:{},addEventListener(name,fn){(windowEvents[name]??=[]).push(fn);},removeEventListener(name,fn){windowEvents[name]=(windowEvents[name]||[]).filter(x=>x!==fn);},confirm:()=>true,sidecar:{conversationId:'native-current',getWorkspaceUris(){sdkQueries++;return reader();},onWorkspaceChange(fn){changed=fn;},fetch(url,opts){return new Promise((resolve,reject)=>requests.push({url,body:JSON.parse(opts.body),resolve,reject}));}}};
const timers=new Map();let nextTimer=0;
const context={window,document,navigator:{language:'en'},crypto:require('node:crypto').webcrypto,URL,console,setTimeout(fn,delay){const id=++nextTimer;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);}};
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
const snapshot=(name='current',project='p1')=>({project_id:project,project_name:name,workspace:name==='other'?'other workspace':'current workspace',counts:{active:2},expected:{expected_project_budget_version:1},project_usage:{scope:'project',project_id:project,max_calls_per_day:100,counted_attempts:1,completed_reviews:1,failed_attempts:0,remaining_calls:99},global_usage:{counted_attempts:1,remaining_calls:299}});
const reply=(request,value,ok=true)=>request.resolve({ok,status:ok?200:503,json:async()=>value});
const valid=(cap='current-cap',summary=snapshot())=>({visible:true,capability:cap,summary});
const pageEvent=async(name)=>{for(const fn of windowEvents[name]||[])fn({type:name,persisted:true});await flush();};
async function click(id){assert(elements[id],`missing ${id}`);assert.equal(elements[id].hidden,false,`${id} is hidden`);assert.equal(elements[id].disabled,false,`${id} is disabled`);elements[id].click();await flush();}
async function boot(){await flush();assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/bootstrap');reply(requests[0],valid());await flush();assert.equal(elements.panel.hidden,false);assert.equal(elements.reconnect.hidden,true);}
async function failAction(name='debug',response=null){await click(name);const req=requests.at(-1);assert.equal(req.body.action,name==='history'?'archive_history':name);if(response)reply(req,response);else req.reject(new Error('simulated connection loss'));await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);return req;}
async function reconnect(){const before=requests.length;const queries=sdkQueries;await click('reconnect');assert.equal(sdkQueries,queries+1);assert.equal(requests.length,before+1);const req=requests.at(-1);assert.equal(req.url,'/api/bootstrap');assert.equal(req.body.conversation_id,window.sidecar.conversationId);assert.deepEqual(req.body.workspace_uris,currentUris);assert.equal(req.body.capability,undefined);return req;}

(async()=>{
 let rejectInitialRead;
 if(scenario==='initial_context_missing') {currentUris=[];window.sidecar.conversationId=null;}
 if(scenario==='late_initial_sdk_rejection')reader=()=>new Promise((_,reject)=>{rejectInitialRead=reject;});
 runInNewContext(script,context);
 if(scenario==='initial_uninstalled'){
   await flush();assert.equal(requests.length,1);reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);
 }else if(scenario==='initial_connection_failure'){
   await flush();assert.equal(requests.length,1);requests[0].reject(new Error('initial connection unavailable'));await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);assert.equal(requests.length,1);
 }else if(scenario==='initial_context_missing'){
   await flush();assert.equal(requests.length,0);assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,true);
 }else if(scenario==='late_initial_sdk_rejection'){
   await flush();assert.equal(requests.length,0);changed([uri]);await flush();assert.equal(requests.length,1);reply(requests[0],valid('new-workspace-cap'));await flush();assert.equal(elements.panel.hidden,false);rejectInitialRead(new Error('obsolete initial SDK failure'));await flush();assert.equal(elements.panel.hidden,false,'late initial SDK rejection cleared the current panel');assert.equal(elements.project.textContent,'current');assert.equal(elements.reconnect.hidden,true);assert.equal(requests.length,1);await click('debug');assert.equal(requests.at(-1).body.capability,'new-workspace-cap');reply(requests.at(-1),{visible:true,summary:snapshot()});await flush();assert.equal(elements.panel.hidden,false);
 }else if(scenario==='debug_failure_reconnect'){
   await boot();await failAction();const before=requests.length;const req=await reconnect();
   elements.reconnect.click();await flush();assert.equal(requests.length,before+1,'parallel reconnect duplicated bootstrap');
   reply(req,valid('fresh-cap'));await flush();assert.equal(elements.panel.hidden,false);assert.equal(elements.reconnect.hidden,true);assert.equal(requests.filter(r=>r.body.action==='debug').length,1,'debug was replayed');
   await click('debug');assert.equal(requests.at(-1).body.capability,'fresh-cap');reply(requests.at(-1),{visible:true,summary:snapshot(),diagnostics:{version:1,project:{id:'p1'},connection:{ui_bridge:'responding'}}});await flush();assert.equal(elements.debugPanel.hidden,false);
 }else if(scenario==='invisible_action_reconnect'){
   await boot();await failAction('debug',{visible:false,reason:'panel_refresh_required'});const req=await reconnect();reply(req,valid('new-process-cap'));await flush();assert.equal(elements.panel.hidden,false);assert.equal(requests.filter(r=>r.body.action).length,1);
 }else if(scenario==='recoverable_bootstrap_response'){
   await boot();await failAction();
   for(const reason of ['connection_unavailable','panel_refresh_required']){
     const req=await reconnect();const before=requests.length;reply(req,{visible:false,reason});await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false,`${reason} removed explicit recovery`);assert.equal(elements.reconnect.disabled,false);assert.equal(requests.length,before,'recoverable bootstrap retried itself');
   }
   const restored=await reconnect();reply(restored,valid('recovered-cap'));await flush();assert.equal(elements.panel.hidden,false);assert.equal(requests.filter(r=>r.body.action==='debug').length,1);
   await failAction();const uninstalled=await reconnect();reply(uninstalled,{visible:false,reason:'workspace_not_bound'});await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false,'a current workspace lost its read-only registration check');
 }else if(scenario==='mutating_unknown_not_replayed'){
   await boot();const req=await failAction('history');assert.equal(req.body.confirm,true);assert.equal(typeof req.body.request_id,'string');const before=requests.length;const next=await reconnect();reply(next,valid('fresh-after-unknown'));await flush();assert.equal(requests.length,before+1);assert.equal(requests.filter(r=>r.body.action==='archive_history').length,1);assert.equal(elements.result.textContent,'');
 }else if(scenario==='reconnect_reads_new_host_context'){
   await boot();await failAction();currentUris=[otherUri];window.sidecar.conversationId='native-new';const req=await reconnect();assert.equal(req.body.conversation_id,'native-new');reply(req,valid('new-context-cap',snapshot('other','p2')));await flush();assert.equal(elements.panel.hidden,false);assert.equal(elements.project.textContent,'other');assert.equal(requests.filter(r=>r.body.action).length,1);
 }else if(scenario==='reconnect_ambiguous_context'||scenario==='reconnect_missing_conversation'){
   await boot();await failAction();const before=requests.length;if(scenario==='reconnect_ambiguous_context')currentUris=[uri,otherUri];else window.sidecar.conversationId=null;await click('reconnect');assert.equal(requests.length,before);assert.equal(elements.panel.hidden,true);
 }else if(scenario==='workspace_switch_discards_old_action'){
   await boot();await click('history');const old=requests.at(-1);currentUris=[otherUri];window.sidecar.conversationId='native-other';changed(currentUris);await flush();assert.equal(elements.panel.hidden,true);const next=requests.at(-1);assert.equal(next.url,'/api/bootstrap');assert.deepEqual(next.body.workspace_uris,[otherUri]);reply(next,valid('other-cap',snapshot('other','p2')));await flush();reply(old,{visible:true,summary:snapshot('stale'),history:{state:'materialized',archived_cases:99}});await flush();assert.equal(elements.project.textContent,'other');assert.equal(elements.result.textContent,'');assert.equal(requests.filter(r=>r.body.action==='archive_history').length,1);
 }else if(scenario==='workspace_ambiguity_invalidates_bound_state'){
   await boot();await click('debug');const old=requests.at(-1);const before=requests.length;currentUris=[uri,otherUri];changed(currentUris);await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,true);assert.equal(requests.length,before);reply(old,{visible:true,summary:snapshot('stale')});await flush();assert.equal(elements.panel.hidden,true);elements.debug.click();await flush();assert.equal(requests.length,before,'hidden control retained its old capability');
 }else if(scenario==='late_bootstrap_discarded'){
   await flush();const old=requests[0];currentUris=[otherUri];window.sidecar.conversationId='native-other';changed(currentUris);await flush();const next=requests.at(-1);reply(next,valid('other-cap',snapshot('other','p2')));await flush();reply(old,valid('stale-cap',snapshot('stale')));await flush();assert.equal(elements.project.textContent,'other');assert.equal(elements.panel.hidden,false);
 }else if(scenario==='pageshow_rebinds_no_write_replay'){
   await boot();await click('history');const old=requests.at(-1);await pageEvent('pagehide');assert.equal(elements.panel.hidden,true);const before=requests.length;changed([otherUri]);await flush();assert.equal(requests.length,before,'disposed view sent a workspace bootstrap');reply(old,{visible:true,summary:snapshot('stale'),history:{state:'materialized',archived_cases:99}});await flush();assert.equal(elements.panel.hidden,true);currentUris=[otherUri];window.sidecar.conversationId='native-resumed';const queries=sdkQueries;await pageEvent('pageshow');assert.equal(sdkQueries,queries+1);assert.equal(requests.length,before+1);const next=requests.at(-1);assert.equal(next.url,'/api/bootstrap');assert.equal(next.body.conversation_id,'native-resumed');assert.deepEqual(next.body.workspace_uris,[otherUri]);reply(next,valid('resumed-cap',snapshot('other','p2')));await flush();assert.equal(elements.panel.hidden,false);assert.equal(elements.project.textContent,'other');assert.equal(elements.result.textContent,'');assert.equal(requests.filter(r=>r.body.action==='archive_history').length,1);const count=requests.length;await pageEvent('pageshow');assert.equal(requests.length,count,'duplicate pageshow rebound a live view');
 }else if(scenario==='late_workspace_read_after_pagehide'){
   await boot();await failAction();let resolveRead;reader=()=>new Promise(resolve=>{resolveRead=resolve;});await click('reconnect');const before=requests.length;await pageEvent('pagehide');resolveRead([uri]);await flush();assert.equal(requests.length,before);assert.equal(elements.panel.hidden,true);
 }else if(scenario==='hung_workspace_read_timeout'){
   await boot();await failAction();let resolveRead;reader=()=>new Promise(resolve=>{resolveRead=resolve;});const before=requests.length;await click('reconnect');assert.equal(requests.length,before);const deadline=[...timers.values()].find(timer=>timer.delay===15000);assert(deadline,'SDK workspace read needs a bounded local deadline');deadline.fn();await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);assert.equal(elements.reconnect.disabled,false);assert.equal(requests.length,before,'SDK timeout retried automatically');resolveRead([uri]);await flush();assert.equal(requests.length,before,'late SDK result bootstrapped after timeout');reader=()=>Promise.resolve(currentUris.slice());const next=await reconnect();reply(next,valid('after-timeout-cap'));await flush();assert.equal(elements.panel.hidden,false);assert.equal(requests.filter(r=>r.body.action==='debug').length,1,'original action was replayed after SDK timeout');
 }else if(scenario==='recovery_intent_pagehide_sdk'||scenario==='recovery_intent_pagehide_bootstrap'){
   await boot();await failAction('history');let resolveOldRead;
   if(scenario==='recovery_intent_pagehide_sdk')reader=()=>new Promise(resolve=>{resolveOldRead=resolve;});
   await click('reconnect');const before=requests.length;const oldBootstrap=scenario==='recovery_intent_pagehide_bootstrap'?requests.at(-1):null;
   if(oldBootstrap)assert.equal(oldBootstrap.url,'/api/bootstrap');
   await pageEvent('pagehide');assert.equal(elements.panel.hidden,true);let resolveResumeRead;reader=()=>new Promise(resolve=>{resolveResumeRead=resolve;});await pageEvent('pageshow');assert.equal(requests.length,before);
   const deadline=[...timers.values()].filter(timer=>timer.delay===15000).at(-1);assert(deadline,'resumed SDK read has no deadline');deadline.fn();await flush();assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false,'pagehide lost in-flight recovery intent');assert.equal(elements.reconnect.disabled,false);assert.equal(requests.length,before);
   if(resolveOldRead)resolveOldRead([uri]);if(oldBootstrap)reply(oldBootstrap,valid('obsolete-before-pagehide'));resolveResumeRead([uri]);await flush();assert.equal(requests.length,before);assert.equal(elements.panel.hidden,true);reader=()=>Promise.resolve(currentUris.slice());const next=await reconnect();reply(next,valid('after-resume-timeout'));await flush();assert.equal(elements.panel.hidden,false);assert.equal(requests.filter(r=>r.body.action==='archive_history').length,1,'unknown mutation was replayed during recovery');
 }else if(scenario==='late_workspace_read_after_switch'){
   await boot();await failAction();let resolveRead;reader=()=>new Promise(resolve=>{resolveRead=resolve;});await click('reconnect');const before=requests.length;currentUris=[otherUri];window.sidecar.conversationId='native-other';changed(currentUris);await flush();assert.equal(requests.length,before+1);const next=requests.at(-1);resolveRead([uri]);await flush();assert.equal(requests.length,before+1,'late SDK read restored old workspace');reply(next,valid('other-cap',snapshot('other','p2')));await flush();assert.equal(elements.project.textContent,'other');
 }else if(scenario==='initial_pageshow_no_duplicate'){
   await flush();assert.equal(requests.length,1);await pageEvent('pageshow');assert.equal(requests.length,1);reply(requests[0],valid());await flush();assert.equal(elements.panel.hidden,false);
 }else throw new Error('unknown recovery scenario');
 assert(!requests.some(r=>r.url.includes('send-message')));
 console.log(JSON.stringify({scenario,passed:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for AG page recovery checks")
class AntigravityPanelRecoveryTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(os.environ.get("ALP_TEST_ROOT") or ROOT / "build/local/fix1428")
        base = test_root / "panel-recovery-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name)
        self.workspace = self.area / "workspace"
        self.other = self.area / "other workspace"
        for name in ("workspace", "other workspace", "home", "runtime-releases", "codex", "tmp"):
            (self.area / name).mkdir()
        self.config = self.area / "config.json"
        self.config.write_text("{}", encoding="utf-8")

    def scenario(self, name):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ALP_")}
        env.update({"ALP_HOME": str(self.area / "home"), "ALP_CONFIG": str(self.config),
                    "ALP_RUNTIME_STORE": str(self.area / "runtime-releases"),
                    "CODEX_HOME": str(self.area / "codex"), "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(ROOT), "TMP": str(self.area / "tmp"),
                    "TEMP": str(self.area / "tmp"), "TMPDIR": str(self.area / "tmp"),
                    "ALP_RECOVERY_HTML": str(HTML), "ALP_RECOVERY_WORKSPACE": str(self.workspace),
                    "ALP_RECOVERY_OTHER": str(self.other)})
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, name], env=env,
                                cwd=self.area, capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_reconnect_is_outside_hidden_controls(self):
        class Structure(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.parents = None
            def handle_starttag(self, tag, attrs):
                ident = dict(attrs).get("id")
                if ident == "reconnect":
                    self.parents = list(self.stack)
                if tag not in {"meta", "link", "br", "input", "img", "hr"}:
                    self.stack.append((tag, ident))
            def handle_endtag(self, tag):
                for i in range(len(self.stack) - 1, -1, -1):
                    if self.stack[i][0] == tag:
                        del self.stack[i:]
                        break
        parser = Structure()
        parser.feed(HTML.read_text(encoding="utf-8"))
        self.assertIsNotNone(parser.parents, "Reconnect must be reachable when controls are hidden")
        self.assertNotIn("panel", [ident for _, ident in parser.parents])

    def test_debug_failure_can_reconnect_without_replaying(self):
        self.scenario("debug_failure_reconnect")

    def test_invisible_action_can_reconnect_without_replaying(self):
        self.scenario("invisible_action_reconnect")

    def test_only_recoverable_bootstrap_failures_preserve_explicit_retry(self):
        self.scenario("recoverable_bootstrap_response")

    def test_unknown_mutation_is_never_replayed(self):
        self.scenario("mutating_unknown_not_replayed")

    def test_reconnect_reads_current_sdk_scope(self):
        self.scenario("reconnect_reads_new_host_context")

    def test_reconnect_rejects_ambiguous_or_missing_context(self):
        for name in ("reconnect_ambiguous_context", "reconnect_missing_conversation"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_workspace_switch_discards_late_mutation_result(self):
        self.scenario("workspace_switch_discards_old_action")

    def test_ambiguous_workspace_invalidates_previous_capability(self):
        self.scenario("workspace_ambiguity_invalidates_bound_state")

    def test_old_bootstrap_cannot_restore_previous_scope(self):
        self.scenario("late_bootstrap_discarded")

    def test_pageshow_rebinds_without_mutation_replay(self):
        self.scenario("pageshow_rebinds_no_write_replay")

    def test_workspace_read_finishing_after_pagehide_is_discarded(self):
        self.scenario("late_workspace_read_after_pagehide")

    def test_hung_sdk_read_times_out_without_replaying_or_accepting_late_data(self):
        self.scenario("hung_workspace_read_timeout")

    def test_inflight_recovery_intent_survives_pagehide_and_resumed_timeout(self):
        for name in ("recovery_intent_pagehide_sdk", "recovery_intent_pagehide_bootstrap"):
            with self.subTest(name=name):
                self.scenario(name)

    def test_workspace_read_finishing_after_scope_change_is_discarded(self):
        self.scenario("late_workspace_read_after_switch")

    def test_first_pageshow_does_not_duplicate_initial_bootstrap(self):
        self.scenario("initial_pageshow_no_duplicate")

    def test_uninstalled_project_has_no_controls_but_can_recheck_registration(self):
        self.scenario("initial_uninstalled")

    def test_first_connection_failure_allows_read_only_context_recheck(self):
        self.scenario("initial_connection_failure")

    def test_initial_missing_context_does_not_request(self):
        self.scenario("initial_context_missing")

    def test_late_initial_sdk_rejection_cannot_clear_new_workspace_binding(self):
        self.scenario("late_initial_sdk_rejection")


if __name__ == "__main__":
    unittest.main()
