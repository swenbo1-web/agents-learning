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
context.fetch=async(url,options={})=>{
 if(url!=='/api/admin-control/candidate-reference'&&url!=='/api/admin-control/response-recheck')return originalFetch(url,options);
 calls.push({url,options});const payload=JSON.parse(options.body);
 if(payload.candidate_id){}else if(payload.job_id){}else if(url.endsWith('candidate-reference'))latest.candidate_reference_policy={enabled:payload.enabled,version:4};
 return {ok:true,status:200,json:async()=>({request_id:payload.request_id,
  action:url.endsWith('response-recheck')?'response_recheck':'candidate_reference',status:'configured',settings:clone(latest)})};
};
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
        for scenario in ("render_language", "independent_save", "local_recheck", "history_grant", "history_recheck_boundary", "accepted_activity"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "-e", HARNESS], input=json.dumps({"script": JAVASCRIPT, "scenario": scenario}),
                    capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ok", result.stdout)
