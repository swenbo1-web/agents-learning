"""Run the shipped card JavaScript against a deterministic MCP host bridge; no browser or network."""
import json
import re
import shutil
import subprocess
import unittest

from agent_learning.panel_ui import HTML, MIME_TYPE, RESOURCE_URI, resource_content


HARNESS = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {script,scenario}=JSON.parse(fs.readFileSync(0,'utf8'));
const nodes=new Map(),listeners=new Map(),sent=[],timers=new Map();let timer=0,rid=0;
const element=id=>{if(!nodes.has(id))nodes.set(id,{id,hidden:['controls','confirmBox'].includes(id),disabled:false,textContent:'',value:'',dataset:{},querySelectorAll:()=>[...nodes.values()].filter(n=>['refresh','browser','review','apply','history','confirm','cancel','preset'].includes(n.id))});return nodes.get(id);};
const parent={postMessage:message=>sent.push(JSON.parse(JSON.stringify(message)))};
const window={parent,addEventListener:(name,callback)=>{const callbacks=listeners.get(name)||[];callbacks.push(callback);listeners.set(name,callbacks);}};
const context={window,document:{getElementById:element,querySelectorAll:()=>[]},navigator:{language:'en'},crypto:{randomUUID:()=>`stable-${++rid}`},setTimeout:(callback,delay)=>{const id=++timer;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id),console};
vm.runInNewContext(script,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const dispatch=(message,source=parent)=>{for(const callback of listeners.get('message')||[])callback({source,data:message});};
const respond=(request,result)=>dispatch({jsonrpc:'2.0',id:request.id,result});
const snapshot={project_id:'p1',project_name:'Sample project',workspace:'/project',host_scope:'codex:default',admin_admission_enabled:false,model_mode:'first',model:null,remaining_calls:100,max_calls_per_day:100,counts:{active:1,work_memory:2,pending_problems:3,candidates:4},budget_scope:'project',project_usage:{scope:'project',project_id:'p1',counted_attempts:0,completed_reviews:0,failed_attempts:0,remaining_calls:15,max_calls_per_day:15},global_usage:{counted_attempts:32,remaining_calls:68},reason:'disabled',expected:{expected_policy_version:1,expected_project_budget_version:2,expected_settings_version:3,expected_session_epoch:0,expected_governance_epoch:0}};
const result=(bootstrap=true,summary=snapshot)=>({content:[{type:'text',text:'public'}],structuredContent:{visible:true,summary},_meta:{agentLearningPanel:{visible:true,summary,context_handle:'real-handle',...(bootstrap?{capability:'private-capability'}:{})}}});
async function boot(tools=true){assert.equal(element('controls').hidden,true);const initialize=sent.find(m=>m.method==='ui/initialize');assert(initialize);respond(initialize,{protocolVersion:'2026-01-26',hostCapabilities:tools?{serverTools:{}}:{},hostContext:{}});await flush();dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{context_handle:'real-handle'}}});dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:result()});await flush();}
async function clickAndRespond(id){const before=sent.length,promise=element(id).onclick();const request=sent.slice(before).find(m=>m.method==='tools/call');assert(request);respond(request,result(false));await promise;await flush();return request;}
(async()=>{
if(scenario==='bootstrap'){
 const spoof={jsonrpc:'2.0',method:'ui/notifications/tool-result',params:result()};dispatch(spoof,{});assert.equal(element('controls').hidden,true);
 const initialize=sent.find(m=>m.method==='ui/initialize');dispatch(spoof);assert.equal(element('controls').hidden,true);respond(initialize,{protocolVersion:'2026-01-26',hostCapabilities:{serverTools:{}}});await flush();assert.equal(element('controls').hidden,false);assert.equal(element('project').textContent,'Sample project');assert.equal(element('active').textContent,'1');assert(![...nodes.values()].some(n=>n.textContent.includes('private-capability')));
}else if(scenario==='project_usage'){
 await boot();assert.match(element('budget').textContent,/Project review attempts: 0/);assert.match(element('budget').textContent,/Valid review results: 0/);assert.match(element('budget').textContent,/Project calls remaining: 15 \/ 15/);assert.match(element('budget').textContent,/All-project attempts: 32/);
}else if(scenario==='legacy_usage'||scenario==='foreign_project_usage'){
 const value=JSON.parse(JSON.stringify(snapshot));if(scenario==='legacy_usage'){delete value.project_usage;delete value.expected.expected_project_budget_version;value.expected.expected_budget_version=99;}else value.project_usage.project_id='other-project';
 const init=sent.find(m=>m.method==='ui/initialize');respond(init,{protocolVersion:'2026-01-26',hostCapabilities:{serverTools:{}}});await flush();dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:result(true,value)});await flush();assert.equal(element('controls').hidden,false);assert.match(element('budget').textContent,/Project usage unavailable/);assert(!element('budget').textContent.includes('Project calls remaining: 100'));assert.equal(element('apply').disabled,true);const before=sent.length;element('apply').onclick();assert.equal(element('confirmBox').hidden,true);assert.equal(sent.length,before);
}else if(scenario==='missing_tools'){
 await boot(false);assert.equal(element('controls').hidden,true);assert(!sent.some(m=>m.method==='tools/call'));
}else if(scenario==='direct_actions'){
 await boot();for(const [id,action] of [['refresh','status'],['browser','open_browser'],['review','open_review']]){const request=await clickAndRespond(id);assert.equal(request.params.name,'learning_panel_action');assert.equal(request.params.arguments.action,action);assert.equal(request.params.arguments.context_handle,'real-handle');assert.equal(request.params.arguments.capability,'private-capability');if(action!=='status')assert.equal(typeof request.params.arguments.request_id,'string');}
 assert(!sent.some(m=>['ui/message','ui/update-model-context'].includes(m.method)));
}else if(scenario==='confirmation'){
 await boot();element('preset').value='active';let before=sent.length;element('apply').onclick();assert.equal(element('confirmBox').hidden,false);assert.equal(sent.length,before);element('cancel').onclick();assert.equal(sent.length,before);element('apply').onclick();const frozen=JSON.parse(JSON.stringify(snapshot.expected));element('confirm').onclick();const request=sent.slice(before).find(m=>m.method==='tools/call');assert(request);assert.equal(request.params.arguments.preset,'active');assert.equal(request.params.arguments.confirm,true);assert.deepEqual(request.params.arguments.expected,frozen);assert.equal(typeof request.params.arguments.request_id,'string');assert([...timers.values()].some(t=>t.delay===20000));respond(request,result(false));await flush();assert.equal(element('controls').hidden,false);
}else if(scenario==='archive_and_retry'){
 await boot();const before=sent.length;element('history').onclick();assert.equal(sent.length,before);element('confirm').onclick();const first=sent.slice(before).find(m=>m.method==='tools/call');assert(first);assert.equal(first.params.arguments.action,'archive_history');assert.equal(first.params.arguments.confirm,true);const timeout=[...timers.values()].find(t=>t.delay===210000);assert(timeout);assert(![...timers.values()].some(t=>t.delay===20000));timeout.callback();await flush();const calls=sent.slice(before).filter(m=>m.method==='tools/call');assert.equal(calls.length,2);assert.deepEqual(calls[0].params.arguments,calls[1].params.arguments);assert([...timers.values()].some(t=>t.delay===210000));respond(calls[1],result(false));await flush();assert.equal(element('controls').hidden,false);assert(!sent.some(m=>m.method==='ui/message'));
}else if(scenario==='archive_paused'||scenario==='archive_unknown'){
 await boot();element('history').onclick();element('confirm').onclick();const request=sent.filter(m=>m.method==='tools/call').at(-1),response=result(false);response._meta.agentLearningPanel.history={state:scenario==='archive_paused'?'paused':'unknown',archived_cases:2,review_requested:false};respond(request,response);await flush();assert.equal(element('controls').hidden,false);assert(element('notice').textContent.includes(scenario==='archive_paused'?'batch is paused':'status is unknown'));assert(element('notice').textContent.includes('publication were not requested'));
}else if(scenario==='invalidate'){
 await boot();const expiry=[...timers.values()].find(t=>t.delay===300000);assert(expiry);expiry.callback();assert.equal(element('controls').hidden,true);const before=sent.length;await element('refresh').onclick();assert.equal(sent.length,before);
}else if(scenario==='mismatch'){
 await boot();dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{context_handle:'other-handle'}}});assert.equal(element('controls').hidden,true);
 dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{context_handle:'real-handle'}}});dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:result()});assert.equal(element('controls').hidden,true);
}else if(scenario==='error'){
 await boot();
 const promise=element('refresh').onclick(),request=sent.filter(m=>m.method==='tools/call').at(-1);respond(request,{isError:true,content:[{type:'text',text:'expired'}]});await promise;assert.equal(element('controls').hidden,true);
}else if(scenario==='project_switch'){
 await boot();dispatch({jsonrpc:'2.0',method:'ui/notifications/host-context-changed',params:{workspace:'/different'}});assert.equal(element('controls').hidden,true);
}else if(scenario==='teardown'){
 await boot();const promise=element('refresh').onclick(),request=sent.filter(m=>m.method==='tools/call').at(-1);dispatch({jsonrpc:'2.0',id:request.id,method:'ui/resource-teardown',params:{reason:'closed'}});await promise;assert.equal(element('controls').hidden,true);assert(sent.some(m=>m.id===request.id&&m.result&&Object.keys(m.result).length===0));assert.equal(sent.filter(m=>m.method==='tools/call').length,1);
}else throw Error('unknown scenario');
console.log(JSON.stringify({scenario,passed:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class PanelUITests(unittest.TestCase):
    def test_resource_is_self_contained_and_has_no_ambient_permissions(self):
        resource = resource_content()
        self.assertEqual(resource["uri"], RESOURCE_URI)
        self.assertEqual(resource["mimeType"], MIME_TYPE)
        self.assertEqual(resource["_meta"]["ui"]["csp"], {"connectDomains": [], "resourceDomains": [], "frameDomains": []})
        self.assertNotRegex(HTML, r"(?i)<(?:iframe|img)|<script[^>]+src=|\b(?:fetch|XMLHttpRequest|localStorage|sessionStorage)\b")
        self.assertIn('<main id="controls" hidden>', HTML)

    def scenario(self, name):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for the shipped JavaScript bridge check")
        script = re.search(r"<script>([\s\S]*)</script>", HTML).group(1)
        result = subprocess.run([node, "-e", HARNESS], input=json.dumps({"script": script, "scenario": name}),
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_only_verified_host_bootstrap_displays_controls(self): self.scenario("bootstrap")
    def test_project_counts_are_distinct_from_global_counts(self): self.scenario("project_usage")
    def test_old_runtime_does_not_relabel_global_counts_as_project_usage(self): self.scenario("legacy_usage")
    def test_foreign_project_usage_cannot_enable_project_settings(self): self.scenario("foreign_project_usage")
    def test_missing_host_tool_capability_keeps_controls_hidden(self): self.scenario("missing_tools")
    def test_buttons_call_mcp_directly_without_a_model_message(self): self.scenario("direct_actions")
    def test_setting_change_requires_confirmation_and_frozen_expected_versions(self): self.scenario("confirmation")
    def test_history_archive_requires_confirmation_and_timeout_retry_keeps_request_id(self): self.scenario("archive_and_retry")
    def test_paused_history_archive_reports_partial_progress(self): self.scenario("archive_paused")
    def test_unknown_history_archive_requests_status_check(self): self.scenario("archive_unknown")
    def test_expired_card_cannot_issue_further_requests(self): self.scenario("invalidate")
    def test_context_mismatch_hides_controls_and_replay_cannot_reopen(self): self.scenario("mismatch")
    def test_tool_error_hides_controls(self): self.scenario("error")
    def test_project_switch_hides_old_controls(self): self.scenario("project_switch")
    def test_teardown_request_id_collision_is_acknowledged_without_retry(self): self.scenario("teardown")
