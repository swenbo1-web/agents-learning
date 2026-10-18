"""Execute the shared UI: consent, migration choices and per-batch reference grants."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from tests.test_admin_browser_loading import HARNESS as LOADING_HARNESS


HARNESS = LOADING_HARNESS.split("(async()=>{")[0] + r'''
const fixture=core(),confirmations=[];context.fixture=fixture;
run("sendAdminRequest=async(path,payload)=>{globalThis.sentWrite={path,payload};return payload;};");
context.window.confirm=message=>{confirmations.push(message);return false;};
(async()=>{
 if(scenario==='first_enable'){
  fixture.enabled=false;fixture.settings_version=0;fixture.policy.policy_version=0;fixture.policy.model_mode='default';
  fixture.publication_policy={enabled:false,configured:false,mode:'verified_operation_solutions',first_enable_recommended:true};
  run('state.adminData=fixture;hydrateAdminForm(fixture)');
  assert.equal(get('admin-auto-publish').checked,true);assert.equal(get('admin-publication-mode').value,'tiered');
  await run("saveAdminConfiguration('enable')");assert.equal(context.sentWrite,undefined);assert.equal(confirmations.length,1);
  context.window.confirm=()=>true;await run("saveAdminConfiguration('enable')");
  assert.equal(context.sentWrite.payload.auto_publish,true);assert.equal(context.sentWrite.payload.publication_mode,'tiered');
 }else if(scenario==='existing_off'){
  fixture.publication_policy={enabled:false,configured:true,mode:'verified_operation_solutions',first_enable_recommended:false};
  run('state.adminData=fixture;hydrateAdminForm(fixture)');
  assert.equal(get('admin-auto-publish').checked,false);assert.equal(get('admin-publication-mode').value,'verified_operation_solutions');
  await run("saveAdminConfiguration('save')");assert.equal(confirmations.length,0);assert.equal(context.sentWrite.payload.auto_publish,false);
 }else if(scenario==='history_grant'){
  fixture.publication_policy={enabled:true,configured:true,mode:'tiered'};
  run("state.adminData=fixture;state.adminDirty=false;addHistoryReviewControls(document.createElement('article'),{id:'job',can_review:true});");
  const check=all.find(x=>x.id==='history-review-references-job');assert(check);assert.equal(check.checked,true);
  // The DOM stub normally only indexes static nodes. Register dynamic inputs.
  for(const element of all)if(element.id.startsWith('history-review-'))elements.set(element.id,element);
  await run("confirmHistoryReview({id:'job',version:'frozen'})");assert.equal(context.sentWrite,undefined);
  assert(confirmations[0].includes('sourced references'));
  context.window.confirm=()=>true;await run("confirmHistoryReview({id:'job',version:'frozen'})");
  assert.equal(context.sentWrite.payload.auto_reference,true);assert.equal(context.sentWrite.payload.max_calls,5);
  check.checked=false;await run("confirmHistoryReview({id:'job',version:'frozen'})");assert.equal(context.sentWrite.payload.auto_reference,false);
 }else if(scenario==='knowledge_warning'){
  run("knowledgeNotice(document.getElementById('warning'),{knowledge_tier:'source_reference',pending_counterexample_count:1})");
  assert(get('warning').children.some(x=>x.textContent.includes('not verification')));
  assert(get('warning').children.some(x=>x.textContent.includes('suspended')));
  run("state.language='zh-CN';document.getElementById('warning').replaceChildren();knowledgeNotice(document.getElementById('warning'),{knowledge_tier:'source_reference',pending_counterexample_count:1})");
  assert(get('warning').children.some(x=>x.textContent.includes('尚未')||x.textContent.includes('不代表当前')));
  assert(get('warning').children.some(x=>x.textContent.includes('暂停自动注入')));
 }
 assert.equal(calls.filter(x=>x.options.method==='POST').length,0);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class TieredPublicationUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the shipped UI")
    def test_publication_choices_and_history_consent(self):
        for scenario in ("first_enable", "existing_off", "history_grant", "knowledge_warning"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "-e", HARNESS],
                    input=json.dumps({"script": JAVASCRIPT, "scenario": scenario}), capture_output=True,
                    text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
