"""Execute the shared shipped UI offline; no host, network or model access."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from tests.test_phase_browser_ui import HARNESS as PHASE_HARNESS


HARNESS = PHASE_HARNESS.split("(async()=>{")[0] + r'''
latest.candidate_reference_policy={enabled:false,version:3};
const originalFetch=context.fetch;
let loseHistoryResponse=false,historyReceipt=null;
context.fetch=async(url,options={})=>{
 if(url.startsWith('/api/admin-control/receipt')){calls.push({url,options});return {ok:true,status:200,json:async()=>({found:false})};}
 if(url!=='/api/admin-control/candidate-reference'&&url!=='/api/admin-control/response-recheck'&&url!=='/api/admin-control/history-response-recheck')return originalFetch(url,options);
 calls.push({url,options});const payload=JSON.parse(options.body);
 if(payload.candidate_id){}else if(payload.job_id){}else if(url.endsWith('candidate-reference'))latest.candidate_reference_policy={enabled:payload.enabled,version:4};
 if(url.endsWith('history-response-recheck')){
  const access=context.history?.saved_response?.history_recovery?.access_reconfirmation===true;
  historyReceipt={request_id:payload.request_id,action:'history_response_recheck',status:access?'access_reconfirmed':'accepted',settings:clone(latest),
   result:{status:access?'access_reconfirmed':'accepted',result:{access_reconfirmation:access},models_started:false}};
  if(loseHistoryResponse){loseHistoryResponse=false;throw new Error('lost receipt transport');}
  return {ok:true,status:200,json:async()=>historyReceipt};
 }
 return {ok:true,status:200,json:async()=>({request_id:payload.request_id,
  action:url.endsWith('response-recheck')?'response_recheck':'candidate_reference',status:'configured',settings:clone(latest)})};
};
const historyItem=(extra={})=>({id:'history-call',saved_response:{available:true,can_recheck:false,version:2,
 history_recovery:{available:true,job_id:'frozen-job',job_version:'a'.repeat(64),ack_required:false,boundary_reasons:[],
  original_publication_authorized:true,can_allow_publication:true,...extra}}});
(async()=>{
 mount(latest);context.window.confirm=()=>true;
 if(scenario==='render_language'){
  assert.equal(get('candidate-reference-toggle').disabled,false);
  assert.match(get('candidate-reference-state').textContent,/disabled/);
  language('zh-CN');assert.match(get('candidate-reference-state').textContent,/关闭/);
  assert.match(get('candidate-reference-toggle').textContent,/开启/);
  assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 }else if(scenario==='independent_save'){
  run("state.adminDirty=true;$('admin-model').value='unsaved-draft';controls()");
  assert.equal(get('candidate-reference-toggle').disabled,false);
  await run('changeCandidateReference()');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);
  const payload=JSON.parse(writes[0].options.body);assert.equal(payload.enabled,true);assert.equal(payload.expected_version,3);
  assert.equal(payload.start,undefined);assert.equal(payload.auto_publish,undefined);
  assert.equal(get('admin-model').value,'unsaved-draft');assert.equal(run('state.adminDirty'),true);
 }else if(scenario==='local_recheck'){
  run('state.adminDirty=false');context.failed={id:'failed-call',saved_response:{can_recheck:true,version:2}};
  await run('recheckSavedResponse(failed)');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);
  assert.equal(writes[0].url,'/api/admin-control/response-recheck');
  const payload=JSON.parse(writes[0].options.body);assert.equal(payload.call_id,'failed-call');assert.equal(payload.confirmed,true);
  assert.equal(payload.action,undefined);assert.equal(payload.retry_allowed,undefined);
 }else if(scenario==='history_grant'){
  context.job={id:'frozen-history',candidate_reference_grant:{enabled:false,version:7}};
  await run('changeCandidateReference(job)');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);
  const payload=JSON.parse(writes[0].options.body);assert.equal(payload.job_id,'frozen-history');assert.equal(payload.expected_version,7);
  assert.equal(payload.max_calls,undefined);assert.equal(payload.allow_review,undefined);
 }else if(scenario==='history_recheck_boundary'){
  latest.details_separate=false;latest.recovery={capture:{},blocked_reviews:[{id:'saved-history',state:'failed',reason:'local_decision_rejected',usage_known:true,
   saved_response:{available:true,can_recheck:false,reason:'history_run_reapproval_required'}}]};mount(latest);
  const rendered=textOfTree(get('admin-recovery'));assert.match(rendered,/explicit permission handling/);
  assert(!rendered.includes('Revalidate saved response (no model call)'));
  language('zh-CN');assert.match(textOfTree(get('admin-recovery')),/批次权限/);
  assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 }else if(scenario==='history_local_recheck'){
  context.history=historyItem();run("state.adminDirty=true;$('admin-model').value='unsaved-history-draft';controls()");
  context.window.prompt=()=> 'Accept only the frozen call';
  await run('recheckHistorySavedResponse(history)');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);
  assert.equal(writes[0].url,'/api/admin-control/history-response-recheck');
  const payload=JSON.parse(writes[0].options.body);assert.equal(payload.expected_job_version,'a'.repeat(64));assert.equal(payload.expected_version,2);
  assert.equal(payload.allow_publication,false);assert.equal(payload.acknowledge_boundary,false);assert.equal(payload.confirmed,true);
  assert.equal(payload.max_calls,undefined);assert.equal(payload.allow_review,undefined);assert.equal(payload.start,undefined);
  assert.equal(get('admin-model').value,'unsaved-history-draft');assert.equal(run('state.adminDirty'),true);
  assert.match(textOfTree(get('admin-result')),/Local acceptance completed/);
  language('zh-CN');assert.match(textOfTree(get('admin-result')),/未启动模型/);
 }else if(scenario==='history_boundary_confirm'){
  context.history=historyItem({ack_required:true,boundary_reasons:['account_boundary','history_cancelled']});
  const confirmations=[];context.window.confirm=message=>{confirmations.push(message);return confirmations.length===1;};
  let prompts=0;context.window.prompt=()=>{prompts++;return 'Checked sources';};
  await run('recheckHistorySavedResponse(history)');assert.equal(prompts,0);assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
  assert.match(confirmations[1],/Login account changed/);assert.match(confirmations[1],/cancelled/);
  context.window.confirm=()=>true;context.window.prompt=()=> '   ';
  await run('recheckHistorySavedResponse(history)');assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
  language('zh-CN');context.window.prompt=()=> '已核对本次冻结来源';
  await run('recheckHistorySavedResponse(history)');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);
  assert.equal(JSON.parse(writes[0].options.body).acknowledge_boundary,true);
 }else if(scenario==='history_publication'){
  context.history=historyItem();context.window.prompt=()=> 'Checked this saved output';
  latest.details_separate=false;latest.recovery={capture:{},blocked_reviews:[context.history]};mount(latest);
  const inputs=all.filter(element=>element.type==='checkbox'&&element.parentElement?.children.some(child=>child.textContent?.includes('original batch permission')));
  assert.equal(inputs.length,1);assert.equal(inputs[0].checked,false);
  let count=0;context.window.confirm=()=> ++count===1;
  await run('recheckHistorySavedResponse(history,true)');assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
  context.window.confirm=()=>true;await run('recheckHistorySavedResponse(history,true)');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);assert.equal(JSON.parse(writes[0].options.body).allow_publication,true);
 }else if(scenario==='history_access_reconfirm'){
  context.history=historyItem({access_reconfirmation:true});context.window.prompt=()=> 'Reconfirm existing results only';
  latest.details_separate=false;latest.recovery={capture:{},blocked_reviews:[context.history]};mount(latest);
  assert.match(textOfTree(get('admin-recovery')),/Reconfirm access/);
  await run('recheckHistorySavedResponse(history,true)');await flush();
  assert.equal(JSON.parse(calls.find(call=>call.options.method==='POST').options.body).allow_publication,false);
  assert.match(textOfTree(get('admin-result')),/no new review or publication/);
 }else if(scenario==='history_recent_access'){
  context.history=historyItem({access_reconfirmation:true,needs_access_reconfirmation:true});
  latest.details_separate=false;latest.review_activity={recent:[{problem_id:'case',call_id:context.history.id,
   purpose:'history_review',history_access_required:true,local_recheck:true,state:'candidate',
   saved_response:context.history.saved_response,evidence:{}}],upcoming:[],deferred:[]};mount(latest);
  assert.match(textOfTree(get('admin-flow')),/Reconfirm access to this result/);
  latest.review_activity.recent[0].saved_response.history_recovery.needs_access_reconfirmation=false;mount(latest);
  assert(!textOfTree(get('admin-flow')).includes('Reconfirm access to this result'));
 }else if(scenario==='history_unavailable'){
  for(const reason of ['history_run_active','history_snapshot_unavailable','history_snapshot_changed']){
   context.history=historyItem({available:false,reason});latest.details_separate=false;latest.recovery={capture:{},blocked_reviews:[context.history]};mount(latest);
   const rendered=textOfTree(get('admin-recovery'));assert(!rendered.includes('Accept this batch'));
   assert(!rendered.includes(reason));await run('recheckHistorySavedResponse(history)');
  }
  language('zh-CN');assert.match(textOfTree(get('admin-recovery')),/冻结来源/);
  run("state.language='en';globalThis.historyFailure=apiFailure({error:'history_local_job_version_changed'},400)");
  assert.match(run('errorText(historyFailure)'),/batch changed/);
  run("state.language='zh-CN'");assert.match(run('errorText(historyFailure)'),/历史批次已变化/);
  assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 }else if(scenario==='history_retry'){
  context.history=historyItem();context.window.prompt=()=> 'Same frozen request';loseHistoryResponse=true;
  await assert.rejects(run('recheckHistorySavedResponse(history)'),/Retrieve or retry the same request/);
  const pending=run('state.adminPending');assert.equal(pending.uncertain,true);assert(pending.payload.request_id);
  const original=JSON.stringify(pending.payload);await run('retryAdminRequest()');await flush();
  const writes=calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,2);
  assert.equal(JSON.stringify(JSON.parse(writes[0].options.body)),original);assert.equal(writes[1].options.body,writes[0].options.body);
  assert.equal(run('state.adminPending'),null);
 }else if(scenario==='accepted_activity'){
  latest.project_usage.effective_completed_reviews=3;latest.project_usage.locally_recovered_reviews=2;
  latest.details_separate=false;latest.review_activity={recent:[{problem_id:'phase',title:'Saved stage review',purpose:'phase_review',state:'candidate',
   execution_state:'failed',local_recheck:true,evidence:{},candidates:[]}],upcoming:[],deferred:[]};mount(latest);
  assert.match(textOfTree(get('admin-flow')),/Original execution record/);
  const completed=get('admin-status-cards').children.find(card=>card.children[0]?.textContent==='Valid review results');
  assert.equal(String(completed.children[1].textContent),'3');assert.match(textOfTree(get('admin-usage-details')),/locally recovered/);
  language('zh-CN');assert.match(textOfTree(get('admin-flow')),/本地重验/);
  assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
 }
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the shipped UI")
class CandidateReferenceUITests(unittest.TestCase):
    def test_reference_controls_language_and_model_free_requests(self):
        for scenario in ("render_language", "independent_save", "local_recheck", "history_grant", "history_recheck_boundary", "accepted_activity",
                         "history_local_recheck", "history_boundary_confirm", "history_publication", "history_access_reconfirm", "history_recent_access", "history_unavailable", "history_retry"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "-e", HARNESS], input=json.dumps({"script": JAVASCRIPT, "scenario": scenario}),
                    capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ok", result.stdout)
