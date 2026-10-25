"""The shipped recovery view explains queues without performing recovery."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from tests.test_browser_reader_ui import HARNESS as READER_HARNESS


HARNESS = READER_HARNESS.split("(async()=>{")[0] + r'''
const text=element=>[element.textContent||'',...element.children.map(item=>typeof item==='object'?text(item):String(item))].join(' ');
const descendants=element=>[element,...element.children.flatMap(item=>typeof item==='object'?descendants(item):[])];
run(`state.session={admin_control_enabled:true};state.adminToken='admin';state.busy=false;state.adminDirty=false;
state.adminData={phase_review:{policy:{mode:'phase'},waiting_phase_count:0,blocked_phase_count:20,phases:{awaiting_stop:1}},
 queue:{eligible:0,eligible_unit:'stage_batch'},recovery:{capture:{},history_separate:true,
 blocked_review_summary:{failed:1,result_unknown:1,total:2,unattributed:{failed:1,result_unknown:0}},
 blocked_reviews:[{id:'failed-call',state:'failed',reason:'reviewer_process_failed',failure_class:'failed',usage_known:false,
 saved_response:{available:true,can_recheck:true,version:1},can_retry:true}]}};
renderAdminRecovery(state.adminData);`);
let content=text(get('admin-recovery'));
assert(content.includes('Selectable stage batches: 0'));
assert(content.includes('blocked stages: 20'));
assert(content.includes('1 failed attempts; 1 attempts with unknown results'));
assert(content.includes('have no confirmed host attribution'));
assert(content.includes('does not establish zero usage'));
assert(content.includes('checked locally first, without another model request'));
const buttons=descendants(get('admin-recovery')).filter(x=>x.tagName==='BUTTON');
const localIndex=buttons.findIndex(x=>x.textContent===run("t('responseRecheck')"));
const retryIndex=buttons.findIndex(x=>x.textContent==='Request a new model review instead');
assert(localIndex>=0&&retryIndex>localIndex,'local acceptance is offered before a new model request');
assert.equal(requests.length,0,'rendering recovery must not submit a model/recovery request');
run(`state.language='zh-CN';renderAdminRecovery(state.adminData)`);
content=text(get('admin-recovery'));
assert(content.includes('当前可选阶段批次：0'));
assert(content.includes('旧阻塞阶段：20'));
assert(content.includes('不代表未消耗平台额度'));
assert(content.includes('可优先本地重新验收'));
assert(content.includes('改为申请重新调用模型'));
run(`state.adminData={recovery:{capture:{},blocked_reviews:[],history_separate:true}};renderAdminRecovery(state.adminData)`);
assert(!text(get('admin-recovery')).includes('undefined'),'old responses must remain readable');
assert.equal(requests.length,0);
console.log('ok');
'''


class RecoveryBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the shipped recovery UI")
    def test_localized_failure_queue_and_local_first_recovery_render_without_requests(self):
        result = subprocess.run([shutil.which("node"), "-e", HARNESS],
                                input=json.dumps({"script": JAVASCRIPT, "scenario": "recovery_classification"}),
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
