"""Execute the shipped stage-review browser UI with a deterministic local API."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from tests.test_browser_reader_ui import HARNESS as READER_HARNESS


HARNESS = READER_HARNESS.split("(async()=>{")[0] + r'''
const phaseIds=['phase-mode','phase-every','phase-daily','phase-input','phase-output'];
const legacyIds=['admin-frequency-preset','admin-daily-calls','admin-batch-calls','admin-summary-enabled'];
const limits={max_calls_per_day:100,max_calls_per_batch:5,max_batch_seconds:300,max_run_seconds:600,timeout_seconds:120,max_reported_tokens_per_day:1000000};
const clone=value=>JSON.parse(JSON.stringify(value));
let latest={project_id:'project',host_scope:'codex:default',session_epoch:1,governance_epoch:1,settings_version:1,
 policy:{policy_version:1,enabled:true,model_mode:'first',model:''},enabled:true,
 routing:{ready:true,executor:{model:'offline-fixture'},observed_models:[]},diagnostic:{reason:'queue_not_checked'},
 project_budget_policy:{scope:'project',project_id:'project',version:1,values:clone(limits)},
 global_budget_policy:{version:1,values:clone(limits)},budget_policy:{version:1,values:clone(limits)},
 project_usage:{scope:'project',project_id:'project',counted_attempts:2,completed_reviews:1,failed_attempts:0,
  remaining_calls:98,reported_tokens:1200,token_reservations:20000,accounted_tokens:21200,
  remaining_reported_tokens:978800,max_reported_tokens_per_day:1000000,budget_mode:'tokens'},
 global_usage:{counted_attempts:2,remaining_calls:98},admission:{admission_status:'available'},
 publication_policy:{enabled:true,mode:'tiered'},work_summary:{enabled:false},details_separate:true,
 runner:{running:false},effective_run_seconds:180,
 phase_review:{host_scope:'codex:default',policy:{mode:'phase',configured:true,version:4,every_phases:2,daily_tokens:1000000,max_input_tokens:40000,max_output_tokens:4000},
  phases:{awaiting_stop:1,sealed:3,queued:2,partial:1,completed:4},historical_boundary_phases:7,source_scan_omitted:9,
  recent_batches:[{id:'pending-batch-id',state:'pending',coverage_complete:null,items:[]},
   {id:'partial-batch-id',state:'completed',coverage_complete:false,
    metrics:{submitted_cases:2,evidence_records:7,call_count:1,candidate_count:2,published_count:0,
     batch_elapsed_seconds:15.4,call_elapsed_seconds:13.132,input_tokens:11465,output_tokens:203,total_tokens:11668},
    items:[{problem_id:'source-case-id',status:'candidate',candidate_ids:['c1','c2'],reason:'Sourced convention'}]},
   {id:'complete-batch-id',state:'completed',coverage_complete:true,items:[]}]}};
const calls=[];let writeGate=null;
context.fetch=async(url,options={})=>{
 calls.push({url,options});let result;
 if(url.startsWith('/api/admin-control/details')){
  const section=new URL(url,'http://local').searchParams.get('section');
  result={project_id:'project',host_scope:'codex:default',section,versions:[latest.policy.policy_version,latest.project_budget_policy.version,latest.settings_version,1,1],
   data:section==='activity'?{recent:[],upcoming:[],deferred:[]}:section==='recovery'?{capture:{},blocked_reviews:[],history_separate:true}:{eligible:2,workload:{stages:{},pending_total:2}}};
 }else if(url.startsWith('/api/admin-control/history'))result={items:[],offset:0,next_offset:null};
 else if(url.startsWith('/api/admin-control?view=settings'))result=clone(latest);
 else if(url==='/api/admin-control/configure'){
  const payload=JSON.parse(options.body);if(writeGate)await writeGate;
  latest.settings_version++;latest.enabled=payload.enabled;
  if(payload.phase_review){latest.phase_review.policy={...latest.phase_review.policy,...payload.phase_review,version:latest.phase_review.policy.version+1};}
  latest.project_budget_policy.values={...latest.project_budget_policy.values,...payload.project_limits};
  result={request_id:payload.request_id,status:'configured',settings:clone(latest),launch:{status:'not_requested'}};
 }else throw new Error('unexpected phase route '+url);
 return {ok:true,status:200,json:async()=>result};
};
context.window.confirm=()=>true;
const textOfTree=element=>[element?.textContent||'',...(element?.children||[]).map(child=>typeof child==='string'?child:textOfTree(child))].join(' ');
const fire=(id,value)=>{const target=get(id);target.value=String(value);for(const callback of target.listeners.change||[])callback({target});};
const oldQueries=context.document.querySelectorAll;
get('phase-heading-label').dataset.i18n='phaseHeading';
context.document.querySelectorAll=selector=>selector==='[data-i18n]'?[get('phase-heading-label')]:oldQueries(selector);
function mount(value){context.current=clone(value);run('adoptAdminState(current);hydrateAdminForm(current);renderAdminState();controls()');}
function language(value){get('language').value=value;for(const callback of get('language').listeners.change||[])callback({target:get('language')});}
run("state.view='admin-control';state.session={admin_control_enabled:true};state.adminToken='admin';state.token='read';state.busy=false;");
(async()=>{
 await run('loadAdminState()');await flush();await flush();
 if(scenario==='hydrate_and_tokens'){
  assert.deepEqual(phaseIds.map(id=>get(id).value),['phase','2','1000000','40000','4000']);
  const cards=textOfTree(get('admin-status-cards'));
  assert(cards.includes('Reported project tokens today'));assert(cards.includes('1200'));
  assert(cards.includes('Estimated tokens remaining'));assert(cards.includes('978800'));
  assert(!cards.includes('Project calls remaining'));
  assert.match(get('phase-progress').textContent,/Stages waiting: 5/);
  assert.match(get('phase-progress').textContent,/completed: 4/);assert.match(get('phase-progress').textContent,/partial: 1/);
  assert.match(get('phase-progress').textContent,/unsettled reservations: 20000/);
  assert(legacyIds.every(id=>get(id).disabled));assert(phaseIds.every(id=>!get(id).disabled));
  assert.equal(get('admin-save').disabled,false);
 }else if(scenario==='tool_context_metrics'){
  const value=clone(latest);value.phase_review.recent_batches[1].metrics.tool_context={selected_observations:2,selection_limited:true};
  mount(value);language('en');let text=textOfTree(get('phase-results'));
  assert(text.includes('Selected related tool observations: 2'));assert(text.includes('not a full tool audit'));
  assert(text.includes('Some matching observations were not included'));
  language('zh-CN');text=textOfTree(get('phase-results'));
  assert(text.includes('已关联 2 条相关工具观察'));assert(text.includes('原工作历史仍可查阅'));
  mount(clone(latest));assert(!textOfTree(get('phase-results')).includes('有界选材'));
 }else if(scenario==='local_recovery_notes'){
  const value=clone(latest);value.phase_review.local_seal_pending_count=2;value.phase_review.executor_waiting_batch_count=1;
  mount(value);language('en');let text=textOfTree(get('phase-results'));
  assert(text.includes('2 delivered stages await local sealing recovery'));
  assert(text.includes('1 batches await their original executor'));
  assert.match(get('phase-progress').textContent,/completed: 4/);
  language('zh-CN');text=textOfTree(get('phase-results'));
  assert(text.includes('2 个已交付阶段等待本地封存恢复'));
  assert(text.includes('1 个批次等待原执行器'));
  mount(clone(latest));text=textOfTree(get('phase-results'));
  assert(!text.includes('等待本地封存恢复'));assert(!text.includes('批次等待原执行器'));
 }else if(scenario==='locally_deferred_language_and_old_response'){
  const base=clone(latest);
  for(const variant of ['count','phase_fallback','zero','old']){
   const value=clone(base),view=value.phase_review;
   view.waiting_phase_count=5;
   if(variant!=='old')view.phases.routine_deferred=3;
   if(variant==='count')view.locally_deferred_phase_count=3;
   if(variant==='zero')view.locally_deferred_phase_count=0;
   mount(value);language('en');
   const visible=['count','phase_fallback'].includes(variant);
   let text=textOfTree(get('phase-results'));
   assert.equal(text.includes('Pure status checks retained locally: 3'),visible,variant);
   assert.match(get('phase-progress').textContent,/Stages waiting: 5/);
   assert.match(get('phase-progress').textContent,/completed: 4/);
   assert.match(get('phase-progress').textContent,/partial: 1/);
   assert(!get('phase-progress').textContent.includes('retained locally'));
   if(visible){assert(text.includes('some material remains unreviewed'));assert(text.includes('Only the remaining material has not been submitted'));assert(text.includes('these stages are not complete'));assert(!text.includes('not been reviewed by a model'));assert(text.includes('remaining budget'));assert(text.includes('Check and continue'));}
   language('zh-CN');text=textOfTree(get('phase-results'));
   assert.equal(text.includes('纯状态检查本地暂存：3 个阶段'),visible,variant);
   assert.match(get('phase-progress').textContent,/待整理阶段：5/);
   assert.match(get('phase-progress').textContent,/已整理：4/);
   assert.match(get('phase-progress').textContent,/部分覆盖：1/);
   assert(!text.includes('Pure status checks retained locally'));
   if(visible){assert(text.includes('仍有未审核资料'));assert(text.includes('仅余下资料尚未送审，不算审核完成'));assert(!text.includes('尚未模型审核'));assert(text.includes('剩余预算'));assert(text.includes('检查并继续整理'));}
  }
 }else if(scenario==='save_payload'){
  fire('phase-every',3);fire('phase-daily',750000);fire('phase-input',48000);fire('phase-output',6000);
  assert.equal(run('state.adminDirty'),true);assert.equal(get('admin-wake').disabled,true);
  await run("saveAdminConfiguration('save')");await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);
  const payload=JSON.parse(writes[0].options.body);
  assert.deepEqual(payload.phase_review,{mode:'phase',every_phases:3,daily_tokens:750000,max_input_tokens:48000,max_output_tokens:6000});
  assert.equal(payload.expected_phase_version,4);assert.equal(payload.project_limits.max_reported_tokens_per_day,750000);
  assert.equal(payload.expected_settings_version,1);assert.equal(payload.expected_project_budget_version,1);
  assert.equal(payload.start,false);assert.equal(payload.auto_publish,true);assert.equal(payload.publication_mode,'tiered');
  assert.equal(payload.limits,undefined);assert.equal(writes[0].options.headers['X-Admin-Token'],'admin');
  assert.equal(run('state.adminPending'),null);assert.equal(run('state.adminDirty'),false);
  assert.equal(get('phase-every').value,'3');assert.equal(run('state.adminData.phase_review.policy.version'),5);
 }else if(scenario==='pending_lock'){
  fire('phase-every',3);let release;writeGate=new Promise(resolve=>{release=resolve;});
  const saving=run("saveAdminConfiguration('save')");await flush();
  const request=run('state.adminPending.payload.request_id');assert(request);
  run('controls()');assert(phaseIds.every(id=>get(id).disabled));assert.equal(get('admin-save').disabled,true);
  latest.settings_version=2;latest.phase_review.policy.every_phases=5;latest.phase_review.policy.version=5;
  await run('loadAdminState(true)');await flush();
  assert.equal(get('phase-every').value,'3');assert.equal(run('state.adminPending.payload.phase_review.every_phases'),3);
  assert.equal(run('state.adminPending.payload.request_id'),request);assert(phaseIds.every(id=>get(id).disabled));
  language('zh-CN');assert(phaseIds.every(id=>get(id).disabled));assert(get('phase-heading-label').textContent.includes('阶段'));
  release();await saving;await flush();assert.equal(run('state.adminPending'),null);
 }else if(scenario==='draft_and_legacy_lock'){
  fire('phase-every',3);latest.settings_version=2;latest.phase_review.policy.every_phases=5;latest.phase_review.policy.version=5;
  await run('loadAdminState()');await flush();assert.equal(get('phase-every').value,'3');
  assert.equal(run('state.adminStale'),true);assert.equal(get('admin-save').disabled,true);
  await run('loadAdminState(true)');await flush();assert.equal(get('phase-every').value,'5');
  assert.equal(run('state.adminStale'),false);
  fire('phase-mode','legacy');run('controls()');assert(legacyIds.every(id=>!get(id).disabled));
  fire('phase-mode','phase');run('controls()');assert(legacyIds.every(id=>get(id).disabled));
  run('state.busy=true;controls()');assert(phaseIds.every(id=>get(id).disabled));
 }else if(scenario==='validation_and_pause'){
  for(const [id,value]of [['phase-every','0'],['phase-every','21'],['phase-every','1.5'],['phase-daily','-1'],['phase-input','15999'],['phase-output','255'],['phase-output','40001']]){
   const prior=get(id).value;get(id).value=value;
   assert.throws(()=>run("adminConfiguration('save')"),/Invalid stage frequency/);get(id).value=prior;
  }
  language('zh-CN');get('phase-every').value='0';assert.throws(()=>run("adminConfiguration('save')"),/阶段频率/);
  const disabled=run("adminConfiguration('disable')");assert.equal(disabled.enabled,false);assert.equal(disabled.phase_review,undefined);
  assert.equal(disabled.project_limits.max_reported_tokens_per_day,undefined);
  assert.equal(disabled.publication_mode,'tiered');assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 }else if(scenario==='boundary_coverage_language'){
  language('en');assert(run("t('check_candidate_reference_source_blocked')").includes('source lineage requires review'));
  let text=textOfTree(get('phase-results'));
  assert(text.includes('7 stages belong to an earlier login'));assert(text.includes('9 public sources exceeded'));
  assert(text.includes('Repeated Run now cannot extend'));assert(text.includes('candidates 2'));
  assert(text.includes('2 submitted cases'));assert(text.includes('7 evidence records'));
  assert(text.includes('1 execution attempts'));assert(text.includes('0 automatically published'));
  assert(text.includes('13.132s'));assert(text.includes('not per-knowledge times'));assert(text.includes('11668'));
  const headings=get('phase-results').children.filter(node=>node.tagName==='DETAILS').map(node=>node.children[0].textContent);
  assert.match(headings[0],/Awaiting a review result/);assert(!headings[0].includes('Some sources'));
  assert.match(headings[1],/Some sources remain uncovered/);assert.match(headings[2],/Coverage complete/);
  language('zh-CN');text=textOfTree(get('phase-results'));
  assert(run("t('check_candidate_reference_source_blocked')").includes('候选来源链需要复核'));
  assert(text.includes('旧登录或治理边界的 7 个阶段'));assert(text.includes('9 条公开来源超出'));
  assert(text.includes('等待审核结果'));assert(text.includes('候选 2 条'));assert(!text.includes('Awaiting a review result'));
  assert(text.includes('本批送审 2 个案例'));assert(text.includes('7 条证据'));assert(text.includes('均不是单条知识耗时'));
  latest.project_budget_policy.values.max_batch_seconds=90;mount(latest);
  assert(textOfTree(get('phase-results')).includes('完整调用需预留 120秒'));
  language('en');assert(textOfTree(get('phase-results')).includes('A full call needs 120s'));
 }else if(scenario==='first_enable_legacy_retention'){
  const fresh=clone(latest);fresh.enabled=false;fresh.policy={policy_version:0,enabled:false,model_mode:'default',model:''};
  fresh.phase_review.policy={...fresh.phase_review.policy,configured:false,version:0,mode:'legacy'};mount(fresh);
  assert.equal(get('phase-mode').value,'phase');
  const explicit=clone(fresh);explicit.phase_review.policy.configured=true;explicit.phase_review.policy.version=1;mount(explicit);
  assert.equal(get('phase-mode').value,'legacy');
  const old=clone(fresh);old.enabled=true;old.policy.enabled=true;mount(old);assert.equal(get('phase-mode').value,'legacy');
  old.enabled=false;mount(old);assert.equal(get('phase-mode').value,'legacy','pausing an already enabled legacy config must not silently select phase');
 }else if(scenario==='foreign_usage'){
  latest.project_usage.project_id='another-project';latest.project_usage.reported_tokens=987654321;mount(latest);
  assert(!textOfTree(get('admin-status-cards')).includes('987654321'),'foreign usage must not be relabeled as this project');
  assert(!get('phase-progress').textContent.includes('987654321'));assert.equal(get('admin-save').disabled,true);
 }else throw new Error('unknown phase scenario '+scenario);
 if(scenario!=='save_payload'&&scenario!=='pending_lock')assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which("node"), "Node.js required to execute the shipped browser UI")
class PhaseBrowserUITests(unittest.TestCase):
    def scenario(self, name):
        result = subprocess.run([shutil.which("node"), "-e", HARNESS],
            input=json.dumps({"script": JAVASCRIPT, "scenario": name}),
            capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_hydration_and_token_status_replace_call_allowance_cards(self): self.scenario("hydrate_and_tokens")
    def test_local_deferral_is_separate_localized_and_compatible_with_old_responses(self): self.scenario("locally_deferred_language_and_old_response")
    def test_local_seal_and_executor_waits_are_localized_and_optional(self): self.scenario("local_recovery_notes")
    def test_related_tool_selection_is_localized_and_old_batches_remain_compatible(self): self.scenario("tool_context_metrics")
    def test_save_posts_project_token_policy_and_frozen_versions_once(self): self.scenario("save_payload")
    def test_pending_save_stays_locked_across_refresh_and_language_change(self): self.scenario("pending_lock")
    def test_stage_fields_preserve_draft_and_legacy_controls_follow_selected_mode(self): self.scenario("draft_and_legacy_lock")
    def test_invalid_budgets_are_localized_and_pause_preserves_policy(self): self.scenario("validation_and_pause")
    def test_old_boundary_source_limits_pending_coverage_and_window_are_localized(self): self.scenario("boundary_coverage_language")
    def test_first_enable_suggestion_preserves_existing_legacy_and_pause(self): self.scenario("first_enable_legacy_retention")
    def test_foreign_usage_cannot_be_displayed_as_current_project_tokens(self): self.scenario("foreign_usage")


if __name__ == "__main__":
    unittest.main()
