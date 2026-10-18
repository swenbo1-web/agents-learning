"""Execute the shipped browser code while settings and detail reads fail independently."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from tests.test_browser_reader_ui import HARNESS as READER_HARNESS

HARNESS = READER_HARNESS.split("(async()=>{")[0] + r'''
const timers=[];context.setTimeout=(callback,ms)=>{timers.push({callback,ms});return timers.length;};
const limits={max_calls_per_day:100,max_calls_per_batch:5,max_batch_seconds:180,max_run_seconds:600};
let version=1,failCore=false,failSection='',coreGate=null,detailGate=null,oldDetail=false;
const core=()=>({project_id:'project',host_scope:'codex:default',session_epoch:1,governance_epoch:1,
 settings_version:version,policy:{policy_version:1,enabled:true,model_mode:'first',model:''},enabled:true,
 routing:{ready:true,executor:{model:'fixture'},observed_models:[]},diagnostic:{reason:'queue_not_checked'},
 project_budget_policy:{scope:'project',project_id:'project',version:1,values:limits},global_budget_policy:{version:1,values:limits},
 budget_policy:{version:1,values:limits},project_usage:{scope:'project',project_id:'project',counted_attempts:2,completed_reviews:2,remaining_calls:98},
 global_usage:{counted_attempts:2,remaining_calls:98},admission:{admission_status:'available'},
 publication_policy:{enabled:true},work_summary:{enabled:false},details_separate:true,runner:{running:false}});
const calls=[];
context.fetch=async(url,options={})=>{
 calls.push({url,options});let result;
 if(url.startsWith('/api/admin-control/details')){
  const section=new URL(url,'http://local').searchParams.get('section'),captured=version;
  if(section===failSection)return {ok:false,status:503,json:async()=>({reason:'query_budget_exceeded',error:'query budget exceeded'})};
  if(section==='activity'&&detailGate){const pending=detailGate;detailGate=null;await pending;oldDetail=true;}
  result={project_id:'project',host_scope:'codex:default',section,versions:[1,1,captured,1,1],
   data:section==='activity'?{recent:captured===1&&oldDetail?[{title:'OLD STALE REVIEW'}]:[],upcoming:[],deferred:[]}:
        section==='recovery'?{capture:{conversations:1,work_memories:2},blocked_reviews:[],history_separate:true}:{eligible:2,workload:{stages:{},pending_total:2}}};
 }else if(url.startsWith('/api/admin-control/history'))result={items:[],offset:0,next_offset:null};
 else if(url.startsWith('/api/admin-control?view=settings')){
  const snapshot=core();if(coreGate){const pending=coreGate;coreGate=null;await pending;}
  if(failCore)return {ok:false,status:503,json:async()=>({reason:'query_budget_exceeded',error:'query budget exceeded'})};result=snapshot;
 }else throw new Error('unexpected admin route '+url);
 return {ok:true,status:200,json:async()=>result};
};
run("state.view='admin-control';state.session={admin_control_enabled:true};state.adminToken='admin';state.token='read';state.busy=false;");
(async()=>{
 if(scenario==='section_failure'){
  failSection='queue';await run('loadAdminState()');await flush();await flush();
  assert.equal(run('state.adminData.enabled'),true);assert.equal(get('admin-save').disabled,false);
  assert(run("adminSections.queue.error"));assert.equal(run('state.adminData.queue'),undefined);
  assert(get('admin-queue-details').children.some(x=>x.textContent.includes('paused')));
  assert.equal(run('state.adminData.recovery.capture.work_memories'),2);
  const before=calls.filter(x=>x.url.includes('section=queue')).length;
  await run('loadAdminState()');await flush();assert.equal(calls.filter(x=>x.url.includes('section=queue')).length,before);
  run("state.language='zh-CN';renderAdminState()");assert(get('admin-queue-details').children.some(x=>x.textContent.includes('暂停')));
  failSection='';await run("loadAdminSection('queue',true)");assert.equal(run('state.adminData.queue.eligible'),2);
  assert.equal(run('adminSections.queue.error'),null);
 }else if(scenario==='core_failure'){
  failCore=true;for(let i=0;i<3;i++)await run('loadAdminState()');
  assert.equal(run('state.adminPollFailures'),3);assert.equal(get('admin-read-retry').hidden,false);
  const before=timers.length;run('scheduleAdminPoll()');assert.equal(timers.length,before);
  assert(get('admin-poll-error').textContent.includes('paused'));assert.equal(run('state.busy'),false);
  failCore=false;await run('loadAdminState(true)');await flush();
  assert.equal(run('state.adminPollFailures'),0);assert.equal(get('admin-read-retry').hidden,true);
  assert.equal(get('admin-save').disabled,false);assert(timers.some(x=>x.ms===10000));
 }else if(scenario==='stale_detail'){
  let release;detailGate=new Promise(resolve=>{release=resolve;});
  await run('loadAdminState()');await flush();version=2;
  await run('loadAdminState()');await flush();release();await flush();await flush();
  assert.equal(run('state.adminData.settings_version'),2);
  assert(!JSON.stringify(run('state.adminData.review_activity')).includes('OLD STALE REVIEW'));
 }else if(scenario==='disconnect_loading'){
  let release;detailGate=new Promise(resolve=>{release=resolve;});
  await run('loadAdminState()');run('disconnect()');release();await flush();await flush();
  assert.equal(run('state.adminData'),null);assert.equal(run('adminReadScope'),'');
  assert.equal(Object.keys(run('adminSections')).length,0);
 }else if(scenario==='pending_receipt'){
  await run('loadAdminState()');await flush();
  run("state.adminPending={payload:{request_id:'original-request'},uncertain:true};state.adminDirty=true;");
  get('admin-model').value='my-unsaved-model';version=2;await run('loadAdminState(true)');await flush();
  assert.equal(get('admin-model').value,'my-unsaved-model');assert.equal(run('state.adminPending.payload.request_id'),'original-request');
  assert.equal(get('admin-save').disabled,true);
 }else if(scenario==='late_core_after_write'){
  await run('loadAdminState()');await flush();let release;
  coreGate=new Promise(resolve=>{release=resolve;});const pending=run('loadAdminState()');await flush();
  version=2;run("state.adminPending={payload:{request_id:'saved'}};");
  context.saved={request_id:'saved',settings:core(),status:'configured',launch:{status:'not_requested'}};
  await run('applyAdminResponse(saved)');release();await pending;await flush();
  assert.equal(run('state.adminData.settings_version'),2);
 }
 assert(!calls.some(x=>x.options.method==='POST'));
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class AdminBrowserLoadingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the shipped UI")
    def test_settings_recover_without_replaying_writes_or_waiting_for_details(self):
        for scenario in ("section_failure", "core_failure", "stale_detail", "disconnect_loading",
                         "pending_receipt", "late_core_after_write"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "-e", HARNESS],
                    input=json.dumps({"script": JAVASCRIPT, "scenario": scenario}),
                    capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ok", result.stdout)
