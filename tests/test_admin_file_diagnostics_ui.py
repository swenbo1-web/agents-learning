"""Exercise the shared shipped UI without network, model or host access."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import HTML, JAVASCRIPT
from tests.test_phase_browser_ui import HARNESS as PHASE_HARNESS
from tests.test_admin_browser_loading import HARNESS as LOADING_HARNESS


HARNESS = PHASE_HARNESS.split("(async()=>{")[0] + r'''
(async()=>{
 const value=clone(latest);value.details_separate=false;
 value.queue={eligible:0,workspace_links:{roots:[],linked_workspaces:['D:/chosen/engine'],workspace_verified:false,reload_required:true,execution_permission:false}};
 value.recovery={capture:{},blocked_reviews:[],history_separate:true,file_capture_diagnostics:{available:true,events:4,target_count:5,accepted_count:2,filtered_count:3,truncated:true,reasons:{outside_workspace:2,sensitive_target:1,unknown_private_reason:'PRIVATE COMMAND',invalid_target:'PRIVATE TOKEN'},event_id:'PRIVATE EVENT',command:'PRIVATE COMMAND',text:'PRIVATE CONTENT'}};
 if(scenario==='mode_status'){
  value.phase_review.policy={...value.phase_review.policy,mode:'legacy',configured:false,version:0};
  value.enabled=false;value.policy={policy_version:0,enabled:false,model_mode:'default',model:''};
  mount(value);language('en');let text=textOfTree(get('phase-current-policy'));
  assert(text.includes('Legacy case review'));assert(text.includes('compatibility default'));
  assert(text.includes('admission: paused'));assert(text.includes('budget gate: available'));assert(text.includes('running: no'));
  assert(text.includes('unsaved choice'));assert.equal(get('phase-mode').value,'phase');
  value.phase_review.policy.configured=true;value.phase_review.policy.version=1;mount(value);
  text=textOfTree(get('phase-current-policy'));assert(text.includes('explicitly saved'));
  assert(!text.includes('unsaved choice'));assert.equal(get('phase-mode').value,'legacy');
  fire('phase-mode','phase');text=textOfTree(get('phase-current-policy'));
  assert(text.includes('Current review mode: Legacy'));assert(text.includes('unsaved choice'));
  language('zh-CN');text=textOfTree(get('phase-current-policy'));
  assert(text.includes('当前实际审核方式：原逐案审核'));assert(text.includes('已显式保存'));
  assert(text.includes('新审核准入：已暂停'));assert(text.includes('正在运行：否'));assert(text.includes('待保存选择'));
 }else if(scenario==='admission_is_not_running'){
  value.enabled=true;value.runner.running=false;mount(value);language('en');
  let text=textOfTree(get('phase-current-policy'));assert(text.includes('admission: enabled'));assert(text.includes('running: no'));
  value.enabled=false;value.runner.running=true;value.admission.admission_status='token_budget_exhausted';mount(value);
  text=textOfTree(get('phase-current-policy'));assert(text.includes('admission: paused'));assert(text.includes('budget gate: waiting or blocked'));assert(text.includes('running: yes'));
  value.phase_review.policy.mode='phase';mount(value);text=textOfTree(get('phase-current-policy'));
  assert(text.includes('Current review mode: Completed-stage batches'));assert(text.includes('running: yes'));
 }else if(scenario==='unknown_old_fields'){
  value.phase_review={phases:{}};delete value.enabled;delete value.runner.running;delete value.routing.ready;value.admission.admission_status='future_status';
  delete value.queue.workspace_links;delete value.recovery.file_capture_diagnostics;mount(value);language('en');
  let text=textOfTree(get('phase-current-policy'));
  assert(text.includes('mode: Unknown'));assert(text.includes('Setting: Unknown'));
  assert(text.includes('admission: Unknown'));assert(text.includes('budget gate: Unknown'));assert(text.includes('running: Unknown'));
  assert(textOfTree(get('admin-queue-details')).includes('unavailable from this service'));
  assert(textOfTree(get('admin-recovery')).includes('Missing observations remain unknown'));
  assert.equal(get('admin-save').disabled,false);assert(phaseIds.every(id=>get(id).disabled));
  run('renderAdminState()');assert(phaseIds.every(id=>get(id).disabled));assert.equal(get('admin-save').disabled,false);
  assert.equal(run("adminConfiguration('save').phase_review"),undefined);
  delete value.phase_review;mount(value);assert(textOfTree(get('phase-current-policy')).includes('mode: Unknown'));
  language('zh-CN');assert(textOfTree(get('phase-current-policy')).includes('当前实际审核方式：未知'));
 }else if(scenario==='linked_roots'){
  mount(value);language('en');let text=textOfTree(get('admin-queue-details'));
  assert(text.includes('D:/chosen/engine'));assert(text.includes('Explicitly linked directories: 1'));
  assert(text.includes('requires a host reload'));assert(text.includes('no verified checkout context'));
  assert(text.includes('do not merge projects or knowledge stores'));assert(text.includes('local CLI'));
  language('zh-CN');text=textOfTree(get('admin-queue-details'));
  assert(text.includes('显式关联目录：1 个'));assert(text.includes('需要重载宿主'));assert(text.includes('不会合并项目或知识库'));
  value.queue.workspace_links.linked_workspaces=[];value.queue.workspace_links.reload_required=false;mount(value);
  text=textOfTree(get('admin-queue-details'));assert(text.includes('尚未显式关联其他工程目录'));assert(!text.includes('需要重载宿主'));
  value.queue.workspace_links.reload_required=null;mount(value);assert(textOfTree(get('admin-queue-details')).includes('是否需要重载宿主尚未知'));
 }else if(scenario==='filtered_counts'){
  mount(value);language('en');let text=textOfTree(get('admin-recovery'));
  assert(text.includes('parsed targets 5'));assert(text.includes('passed filtering 2'));assert(text.includes('filtered 3'));
  assert(text.includes('does not confirm that a file link was saved'));
  assert(text.includes('Outside the allowed directories: 2'));assert(text.includes('Sensitive target excluded: 1'));
  assert(text.includes('bounded recent sample'));assert(!text.includes('PRIVATE'));assert(!text.includes('unknown_private_reason'));
  language('zh-CN');text=textOfTree(get('admin-recovery'));assert(text.includes('已过滤 3'));
  assert(text.includes('超出允许目录：2'));assert(text.includes('敏感目标已排除：1'));assert(!text.includes('PRIVATE'));
  value.recovery.file_capture_diagnostics.events='PRIVATE COUNT';mount(value);text=textOfTree(get('admin-recovery'));
  assert(text.includes('抽样事件 未知'));assert(!text.includes('PRIVATE'));
  value.recovery.file_capture_diagnostics.available=false;mount(value);text=textOfTree(get('admin-recovery'));
  assert(text.includes('缺少的观察保持未知'));assert(!text.includes('通过过滤 2'));
  delete value.recovery.file_capture_diagnostics.available;mount(value);text=textOfTree(get('admin-recovery'));
  assert(text.includes('缺少的观察保持未知'));assert(!text.includes('通过过滤 2'));
 }else throw new Error('unknown diagnostic scenario '+scenario);
 assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''

LOADING = LOADING_HARNESS.split("(async()=>{")[0].replace(
    "section==='activity'&&detailGate", "section===detailTarget&&detailGate") + r'''
let detailTarget='queue',mismatch='';
const originalFetch=context.fetch;
context.fetch=async(url,options={})=>{
 const response=await originalFetch(url,options);
 if(!url.startsWith('/api/admin-control/details')||!response.ok)return response;
 const result=await response.json(),captured=result.versions[2];
 if(result.section==='queue')result.data.workspace_links={roots:[],linked_workspaces:['D:/chosen/version-'+captured],workspace_verified:false,reload_required:true};
 if(result.section==='recovery')result.data.file_capture_diagnostics={available:true,events:captured,target_count:captured,accepted_count:captured,filtered_count:0,reasons:{}};
 if(mismatch==='project')result.project_id='foreign-project';
 if(mismatch==='host')result.host_scope='dsh:foreign';
 if(mismatch==='session')result.versions[3]++;
 if(mismatch==='governance')result.versions[4]++;
 return {...response,json:async()=>result};
};
const textOfTree=element=>[element?.textContent||'',...(element?.children||[]).map(child=>typeof child==='string'?child:textOfTree(child))].join(' ');
(async()=>{
 if(scenario==='stale_queue'||scenario==='stale_recovery'){
  detailTarget=scenario==='stale_queue'?'queue':'recovery';
  let release;detailGate=new Promise(resolve=>{release=resolve;});
  await run('loadAdminState()');await flush();version=2;
  await run('loadAdminState()');await flush();await flush();release();await flush();await flush();
  assert.equal(run('state.adminData.settings_version'),2);
  if(detailTarget==='queue'){
   assert.deepEqual(Array.from(run('state.adminData.queue.workspace_links.linked_workspaces')),['D:/chosen/version-2']);
   assert(!textOfTree(get('admin-queue-details')).includes('version-1'));
  }else{
   assert.equal(run('state.adminData.recovery.file_capture_diagnostics.events'),2);
   assert(textOfTree(get('admin-recovery')).includes('Sampled events 2'));
  }
 }else if(scenario==='boundary_mismatch'){
  await run('loadAdminState()');await flush();await flush();
  for(const boundary of ['project','host','session','governance']){
   mismatch=boundary;await run("loadAdminSection('queue',true)");
   assert.equal(run('adminSections.queue.error.uiKey'),'adminSectionChanged');
   assert.equal(get('admin-save').disabled,false);
   mismatch='';await run("loadAdminSection('queue',true)");assert.equal(run('adminSections.queue.error'),null);
  }
 }else throw new Error('unknown loading scenario '+scenario);
 assert(!calls.some(call=>call.options.method==='POST'));console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which("node"), "Node.js required for the shipped browser UI")
class AdminFileDiagnosticsUITests(unittest.TestCase):
    def scenario(self, name):
        result = subprocess.run([shutil.which("node"), "-e", HARNESS],
            input=json.dumps({"script": JAVASCRIPT, "scenario": name}),
            capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_actual_compatibility_mode_is_distinct_from_first_enable_suggestion(self):
        self.scenario("mode_status")

    def test_enabled_admission_budget_gate_and_running_are_independent(self):
        self.scenario("admission_is_not_running")

    def test_old_service_missing_fields_stay_unknown_and_basic_settings_work(self):
        self.scenario("unknown_old_fields")

    def test_linked_directory_and_reload_guidance_are_localized_read_only(self):
        self.scenario("linked_roots")

    def test_bounded_filter_counts_are_localized_without_raw_diagnostic_fields(self):
        self.scenario("filtered_counts")

    def test_one_shared_page_has_the_effective_policy_readout(self):
        self.assertIn('id="phase-current-policy"', HTML)

    def loading(self, name):
        result = subprocess.run([shutil.which("node"), "-e", LOADING],
            input=json.dumps({"script": JAVASCRIPT, "scenario": name}),
            capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_late_linked_directory_detail_cannot_cross_a_settings_version(self):
        self.loading("stale_queue")

    def test_late_file_counts_cannot_cross_a_settings_version(self):
        self.loading("stale_recovery")

    def test_project_host_session_governance_mismatch_keeps_details_out_of_forms(self):
        self.loading("boundary_mismatch")
