"""Use the shipped browser to verify independent receipt and cost diagnostics."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_detail, control_summary
from agent_learning.browser_ui import JAVASCRIPT
from tests import test_phase_browser_ui as browser
from tests import test_phase_review as fixtures


SCENARIOS = r'''
 if(scenario==='delivery_diagnostics'){
  const value=clone(latest);
  value.queue={phase_health:{delivery_boundary_diagnostics:{checked:true,sampled:3,sample_limit:8,truncated:true,unknown_count:1,
   items:[{phase_id:'old-stage-00001',task_id:'old-task-00001',task_revision:2,delivery_version:19,reason:'missing_host_stop_after_boundary'},
    {phase_id:'new-stage',task_id:'new-task',task_revision:1,delivery_version:20,reason:'awaiting_host_stop'},
    {phase_id:'limited-stage',task_id:'task',task_revision:1,delivery_version:3,reason:'stop_lookup_limited'}]}}};
  run('resetAdminReads()');mount(value);language('en');let text=textOfTree(get('phase-results'));
  assert(text.includes('3 sampled stages'));assert(text.includes('original matching Stop receipt was not found'));
  assert(text.includes('current delivery is still waiting'));assert(text.includes('bounded check cannot confirm'));
  assert(text.includes('not all pending stages'));assert(text.includes('starts no review or retry'));
  language('zh-CN');text=textOfTree(get('phase-results'));
  assert(text.includes('原交付对应的 Stop 回执'));assert(text.includes('不代表全部待处理阶段'));
  assert(text.includes('交付版本 19'));assert(!calls.some(c=>c.options.method==='POST'));
  run('resetAdminReads()');mount(clone(latest));text=textOfTree(get('phase-results'));assert(!text.includes('交付回执：抽样'));
 }else if(scenario==='unavailable_boundary'){
  const value=clone(latest);value.phase_review.delivery_boundary_diagnostics={checked:false,reason:'diagnostics_unavailable',items:[],unknown_count:null};
  mount(value);language('en');let text=textOfTree(get('phase-results'));
  assert(text.includes('pending stages remain unverified'));assert(text.includes('Basic settings remain usable'));
  assert.equal(get('admin-save').disabled,false);
  language('zh-CN');text=textOfTree(get('phase-results'));assert(text.includes('待处理阶段仍未核实'));
 }else if(scenario==='local_boundary_states'){
  const value=clone(latest);
  value.phase_review.delivery_boundary_diagnostics={checked:true,sampled:3,sample_limit:8,truncated:false,unknown_count:0,
   items:['awaiting_local_seal','local_seal_retry','historical_boundary'].map((reason,i)=>({phase_id:'p'+i,task_id:'t',task_revision:1,delivery_version:1,reason}))};
  mount(value);language('en');let text=textOfTree(get('phase-results'));
  assert(text.includes('matching Stop receipt exists'));assert(text.includes('local sealing needs recovery'));
  assert(text.includes('earlier host or session boundary'));assert(!text.includes('not all pending stages'));
  language('zh-CN');text=textOfTree(get('phase-results'));assert(text.includes('本地封存待恢复'));
 }else if(scenario==='frozen_input_cost'){
  const value=clone(latest),m=value.phase_review.recent_batches[1].metrics;
  m.input_cost={status:'recorded',unit:'characters',instruction_chars:2200,schema_chars:400,transport_saved_chars:96,
   components:{instructions:1000,source_events:500,task_context:100,governance_target:100,related_history:100,
    related_knowledge:100,related_candidates:100,shared_context:100,packet_metadata:100}};
  mount(value);language('en');let text=textOfTree(get('phase-results'));
  assert(text.includes('Visible review input: 2200 characters'));assert(text.includes('source evidence 500'));
  assert(text.includes('Response schema: 400'));assert(text.includes('removed 96 characters'));
  assert(text.includes('not provider tokens or billing'));assert(!text.includes('2200 tokens'));
  language('zh-CN');text=textOfTree(get('phase-results'));
  assert(text.includes('可见审核输入：2200 字符'));assert(text.includes('不是平台 token 或计费量'));
  assert(!calls.some(c=>c.options.method==='POST'));
 }else if(scenario==='unknown_input_cost'){
  for(const reason of ['not_recorded','invalid_snapshot','projection_limit']){
   const value=clone(latest);value.phase_review.recent_batches[1].metrics.input_cost={status:'unknown',unit:'characters',reason};
   mount(value);language('en');let text=textOfTree(get('phase-results'));
   assert(text.includes('not recorded or cannot be verified'));assert(!text.includes('Visible review input: 0'));
   language('zh-CN');text=textOfTree(get('phase-results'));assert(text.includes('本批未记录输入构成'));
  }
  mount(clone(latest));assert(!textOfTree(get('phase-results')).includes('本批未记录输入构成'));
 }else
'''
HARNESS = browser.HARNESS.replace(" if(scenario==='hydrate_and_tokens'){", SCENARIOS + " if(scenario==='hydrate_and_tokens'){")


@unittest.skipUnless(shutil.which("node"), "Node is required for the offline browser harness")
class ReviewDiagnosticsUITests(unittest.TestCase):
    def scenario(self, name):
        result = subprocess.run([shutil.which("node"), "-e", HARNESS],
            input=json.dumps({"script": JAVASCRIPT, "scenario": name}),
            capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_delivery_receipt_sample_is_localized_read_only_and_separate(self): self.scenario("delivery_diagnostics")
    def test_local_sealing_and_historical_states_are_not_missing_stop(self): self.scenario("local_boundary_states")
    def test_failed_receipt_diagnostics_do_not_look_like_an_empty_healthy_queue(self): self.scenario("unavailable_boundary")
    def test_input_composition_is_localized_and_not_billing(self): self.scenario("frozen_input_cost")
    def test_old_or_unverifiable_costs_stay_unknown(self): self.scenario("unknown_input_cost")


class ReviewDiagnosticsLoadingTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp

    def test_basic_settings_do_not_run_boundary_scan(self):
        with patch("agent_learning.phase_boundary_diagnostics.delivery_boundary_diagnostics") as scan:
            settings = control_summary(self.r, self.p, include_health=False)
        scan.assert_not_called()
        self.assertNotIn("delivery_boundary_diagnostics", settings["phase_review"])

    def test_independently_loaded_queue_contains_its_receipt_sample(self):
        sample = {"checked": True, "sampled": 0, "sample_limit": 8, "items": [], "truncated": False}
        with patch("agent_learning.phase_boundary_diagnostics.delivery_boundary_diagnostics", return_value=sample) as scan:
            detail = control_detail(self.r, self.p, "queue")
        scan.assert_called_once_with(self.r, self.p)
        self.assertEqual(detail["data"]["phase_health"]["delivery_boundary_diagnostics"], sample)
        self.assertEqual(detail["project_id"], self.p)
        self.assertEqual(detail["host_scope"], self.r._session_scope())


if __name__ == "__main__":
    unittest.main()
