"""Run the shipped bilingual view with bounded groups and conflicting old totals."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from tests.test_project_usage_ui import BROWSER_CHECK


CHECK = BROWSER_CHECK.split("vm.runInContext(`", 1)[0] + r'''
const flatten=n=>n==null?'':typeof n==='object'?String(n.textContent||'')+' '+(n.children||[]).map(flatten).join(' '):String(n);
vm.runInContext(`
state.view='admin';$('workload-stage').value='pending_review';$('workload-view').value='groups';
state.statsData={project_id:'current',problems:{pending:1617},workload:{stages:{pending_review:2}}};renderStats();
state.listData={view:'groups',total:1,offset:0,next_offset:null,total_cases:9,sampled_cases:2,groups_truncated:true,
 summary:{stages:{pending_review:2,awaiting_evidence:38,awaiting_approval:3},history_pending_units:1518},
 items:[{group_id:'a'.repeat(32),member_count:2,summary:'<img src=x onerror=bad()>',source_host:'codex',
 stages:{pending_review:2},members:[{id:'case-one',revision:1,kind:'daily',queue_state:'pending_review',
 summary:'same failure',created:1,source_host:'codex',conversation:'root',candidates:[]}]}]};
renderList();
`,context);
assert(!flatten(get('stats')).includes('1617'));
assert(flatten(get('stats')).includes('Awaiting review'));
assert(flatten(get('list')).includes('<img src=x onerror=bad()>'));
assert(flatten(get('list')).includes('case-one'));
assert(flatten(get('workload-counts')).includes('1518'));
assert(get('workload-bound').textContent.includes('2 of 9'));
assert.equal(get('workload-tools').hidden,false);
vm.runInContext(`state.language='zh-CN';renderList();`,context);
assert(flatten(get('workload-counts')).includes('已审核待补证'));
assert(flatten(get('workload-counts')).includes('历史待审文本单元'));
assert(flatten(get('list')).includes('查看本组案例'));
vm.runInContext(`
state.language='en';renderWorkloadQueue({queue:{pending:1617,eligible:1,workload:state.listData.summary}});
`,context);
assert(!flatten(get('admin-queue-details')).includes('1617'));
assert(flatten(get('admin-queue-details')).includes('Awaiting approval'));
(async()=>{
 const calls=[];context.fetch=async(url)=>{calls.push(url);return {ok:true,json:async()=>({view:'cases',items:[],total:0,offset:0,next_offset:null,summary:{}})};};
 await vm.runInContext(`workloadGroup='b'.repeat(32);state.offset=20;loadList()`,context);
 const url=calls.at(-1);assert(url.startsWith('/api/admin-groups?'));
 assert(url.includes('view=cases'));assert(url.includes('stage=pending_review'));assert(url.includes('group_id='+ 'b'.repeat(32)));
 await get('workload-back').listeners.click();
 assert(calls.at(-1).includes('view=groups'));assert(!calls.at(-1).includes('b'.repeat(32)));
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class AdminWorkloadUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the shipped JavaScript check")
    def test_bilingual_groups_stages_and_group_drilldown(self):
        result = subprocess.run([shutil.which("node"), "-e", CHECK],
                                input=json.dumps({"script": JAVASCRIPT}), text=True, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)
