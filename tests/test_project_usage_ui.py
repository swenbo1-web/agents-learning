"""Execute shipped UI consumers with conflicting project/global usage; no host or models."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT


BROWSER_CHECK = r'''
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.value='';this.textContent='';this.dataset={};this.listeners={};this.classList={toggle(){}};}
 addEventListener(name,fn){this.listeners[name]=fn;} append(...items){this.children.push(...items);} replaceChildren(...items){this.children=items;}
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[],addEventListener(){},documentElement:{},body:{setAttribute(){}}},navigator:{language:'en'},location:{hash:''},URLSearchParams,crypto:{randomUUID:()=> 'test-request'},setTimeout(){},clearTimeout(){},console});
vm.runInContext(script,context);
vm.runInContext(`
state.session={admin_control_enabled:true};state.adminToken='local-capability';state.view='admin-control';
state.adminData={project_id:'hefe',host_scope:'codex:default',enabled:false,policy:{policy_version:1,model_mode:'first'},project_budget_policy:{scope:'project',project_id:'hefe',version:2,values:{max_calls_per_day:15,max_calls_per_batch:2,max_batch_seconds:120,max_run_seconds:300}},budget_policy:{version:99,values:{max_calls_per_day:999}},project_usage:{scope:'project',project_id:'hefe',counted_attempts:0,completed_reviews:0,failed_attempts:0,remaining_calls:15,usage_unknown_attempts:1,reported_tokens:0,accounted_tokens:100,utc_day:'2026-10-17'},global_usage:{counted_attempts:32,remaining_calls:68},admission:{admission_status:'available',blocking_scope:null},settings_version:3,session_epoch:0,governance_epoch:0};
hydrateAdminForm(state.adminData);renderAdminState();
globalThis.payload=adminConfiguration('save');
`,context);
const flatten=n=>[n.textContent,...n.children.map(flatten)].join(' ');
const cards=flatten(get('admin-status-cards')),details=flatten(get('admin-usage-details'));
assert(cards.includes('Project review attempts today 0'));
assert(cards.includes('Project calls remaining today 15'));
assert(!cards.includes('32'));
assert(details.includes('All-project review attempts today 32'));
assert(details.includes('Unknown (some attempts did not report usage)'));
assert.equal(context.payload.expected_project_budget_version,2);
assert.equal(context.payload.project_limits.max_calls_per_day,15);
assert.equal(context.payload.expected_budget_version,undefined);
assert.equal(context.payload.limits,undefined);
vm.runInContext(`state.adminData.global_budget_policy={scope:'ALP_HOME',version:99,values:{max_calls_per_day:100}};$('admin-global-calls').value='200';globalThis.globalPayload=adminGlobalConfiguration();`,context);
assert.equal(context.globalPayload.expected_budget_version,99);
assert.equal(context.globalPayload.limits.max_calls_per_day,200);
assert.equal(context.globalPayload.project_limits,undefined);
assert.equal(context.globalPayload.expected_project_budget_version,undefined);
assert.equal(context.globalPayload.start,false);
vm.runInContext(`
state.adminData.recovery={capture:{conversations:3,work_memories:24,last_event_at:1},
 blocked_reviews:[{id:'failed-attempt',version:'a'.repeat(64),state:'failed',reason:'historical_failure_details_unavailable',usage_known:false,can_retry:true}],
 history_jobs:[{id:'old-batch',version:'b'.repeat(64),state:'paused',files_read:229,pending:0,unsupported:0,conversation_sources:0,can_reauthorize:true}]};
renderAdminRecovery(state.adminData);
globalThis.recoverySubmission=null;
sendAdminRequest=async(path,payload)=>{globalThis.recoverySubmission={path,payload};};
globalThis.window={confirm:()=>false,prompt:()=>{throw Error('cancelled action requested a reason');}};
confirmAdminRecovery('retry_call',state.adminData.recovery.blocked_reviews[0]);
`,context);
assert(flatten(get('admin-recovery')).includes('These totals do not verify this chat'));
assert(flatten(get('admin-recovery')).includes('Previous usage is incomplete or unknown; retry may use additional platform quota.'));
assert.equal(context.recoverySubmission,null);
vm.runInContext(`window.confirm=()=>true;window.prompt=()=> 'User reviewed the unknown attempt';confirmAdminRecovery('retry_call',state.adminData.recovery.blocked_reviews[0]);`,context);
assert.equal(context.recoverySubmission.path,'admin-control/recovery');
assert.equal(context.recoverySubmission.payload.action,'retry_call');
assert.equal(context.recoverySubmission.payload.confirmed,true);
assert.equal(context.recoverySubmission.payload.id,'failed-attempt');
assert.equal(context.recoverySubmission.payload.start,undefined);
vm.runInContext(`state.language='zh-CN';renderAdminRecovery(state.adminData);state.language='en';`,context);
assert(flatten(get('admin-recovery')).includes('采集覆盖与流程恢复'));
vm.runInContext(`
state.adminData.publication_policy={enabled:false};
state.adminData.review_activity={recent:[{problem_id:'specific-case',title:'Fix a failed build',category:'operation',source_host:'codex',purpose:'daily_review',state:'needs_evidence',evidence:{events:2,successes:0,failures:1,public_statements:1},missing_evidence:['Run the build after the fix'],candidate_title:'Build configuration',candidate_summary:'Use the project build configuration',applicability:'Same build tool',limitations:'Not a production acceptance',publication:{reason:'automatic_publication_disabled'}}],upcoming:[],deferred:[{problem_id:'raw-failure',title:'Raw command failed',purpose:'daily_review',state:'local_preflight',reason:'awaiting_recovery_evidence'}]};
renderAdminFlow(state.adminData);
`,context);
assert(flatten(get('admin-flow')).includes('Fix a failed build'));
assert(flatten(get('admin-flow')).includes('Run the build after the fix'));
assert(flatten(get('admin-flow')).includes('Use the project build configuration'));
assert(flatten(get('admin-flow')).includes('Automatic publication is disabled'));
assert(flatten(get('admin-flow')).includes('deferred locally without a model call'));
vm.runInContext(`
state.adminData.review_activity.recent[0].candidates=['first','second','third'].map((id,index)=>({candidate_id:id,status:index===2?'published':'candidate',title:'Title '+id,content_summary:'Summary '+id,applicability:'Applies '+id,limitations:'Limits '+id,validation_level:'reported',evidence_refs:['event-'+id]}));
state.adminData.review_activity.recent[0].candidate_summary='Legacy duplicate text';renderAdminFlow(state.adminData);
globalThis.previewRequests=[];api=async(path,params)=>{previewRequests.push({path,params});return {title:params.candidate_id,reason:'history_requires_approval'};};
`,context);
for(const id of ['first','second','third'])assert(flatten(get('admin-flow')).includes('Summary '+id));
assert(!flatten(get('admin-flow')).includes('Legacy duplicate text'));
const previews=[];function findPreviews(n){if(n.textContent==='Check publication conditions (read only)')previews.push(n);for(const c of n.children)findPreviews(c);}findPreviews(get('admin-flow'));
assert.equal(previews.length,2);
previews[1].listeners.click();
assert.equal(context.previewRequests[0].params.candidate_id,'second');
vm.runInContext(`state.busy=false;state.adminData.review_activity.recent[0].history_access_required=true;renderAdminFlow(state.adminData);`,context);
assert(!flatten(get('admin-flow')).includes('Summary first'));
vm.runInContext(`state.language='zh-CN';state.adminData.review_activity.recent[0].history_access_required=false;renderAdminFlow(state.adminData);state.language='en';`,context);
assert(flatten(get('admin-flow')).includes('本次审核生成的候选'));
vm.runInContext(`
state.adminDirty=false;state.adminPending=null;state.adminData.recovery.history_jobs[0].can_review=true;
state.adminData.recovery.history_jobs[0].resume_required=true;state.adminData.recovery.history_jobs[0].review_request={state:'superseded'};
state.adminData.recovery.history_jobs[0].counts={cases:12,reviewed_cases:0,candidates:0,published_candidates:0};
renderAdminRecovery(state.adminData);
globalThis.historySubmission=null;sendAdminRequest=async(path,payload)=>{globalThis.historySubmission={path,payload};};
$('history-review-calls-old-batch').value='3';$('history-review-seconds-old-batch').value='120';
window.confirm=()=>false;confirmHistoryReview(state.adminData.recovery.history_jobs[0]);
`,context);
assert.equal(context.historySubmission,null);
assert(flatten(get('admin-recovery')).includes('Continue this archived batch'));
assert(flatten(get('admin-recovery')).includes('inactive'));
vm.runInContext(`window.confirm=()=>true;confirmHistoryReview(state.adminData.recovery.history_jobs[0]);`,context);
assert.equal(context.historySubmission.path,'admin-control/history-review');
assert.equal(context.historySubmission.payload.max_calls,3);
assert.equal(context.historySubmission.payload.max_seconds,120);
assert.equal(context.historySubmission.payload.confirmed,true);
assert.equal(context.historySubmission.payload.start,undefined);
vm.runInContext(`
globalThis.configurationSubmission=null;sendAdminRequest=async(path,payload)=>{globalThis.configurationSubmission={path,payload};};
$('admin-auto-publish').checked=true;window.confirm=()=>false;saveAdminConfiguration('save');
`,context);
assert.equal(context.configurationSubmission,null);
vm.runInContext(`window.confirm=()=>true;saveAdminConfiguration('save');`,context);
assert.equal(context.configurationSubmission.payload.auto_publish,true);
assert.equal(context.configurationSubmission.payload.start,false);
vm.runInContext(`delete state.adminData.project_usage;delete state.adminData.project_budget_policy;renderAdminState();`,context);
assert(flatten(get('admin-readiness')).includes('Project usage unavailable'));
assert.equal(get('admin-save').disabled,true);
assert(!flatten(get('admin-status-cards')).includes('32'));
console.log('ok');
'''


DSH_CHECK = r'''
let {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict');
let factory;
vm.runInNewContext(script.replace('exports.apply = apply;','exports.buildControlPanel = buildControlPanel; exports.apply = apply;'),{window:{__ModuleLoader__:{load:x=>{factory=x.factory;}}},navigator:{language:'en'}});
const plugin=factory(()=>({}));
const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),useState:value=>[value,()=>{}],useCallback:fn=>fn,useEffect(){}};
const Panel=plugin.buildControlPanel(react,null);
const summary={project_id:'hefe',pending_problems:1617,pending_review_cases:2,admin_admission_enabled:true,max_calls_per_day:999,remaining_calls:999,project_usage:{scope:'project',project_id:'hefe',counted_attempts:0,completed_reviews:0,failed_attempts:0,remaining_calls:15,max_calls_per_day:15},global_usage:{counted_attempts:32,remaining_calls:68}};
const flatten=n=>n==null?'':typeof n==='object'?n.children.map(flatten).join(' '):String(n);
let tree=Panel({initialSummary:summary}),output=flatten(tree);
assert(output.includes('Project attempts today 0'));
assert(output.includes('Awaiting Review 2'));assert(!output.includes('1617'));
assert(output.includes('All-project attempts today: 32'));
assert(output.includes('Project allowance left 15/15'));
assert(!output.includes('999'));
delete summary.project_usage;tree=Panel({initialSummary:summary});output=flatten(tree);
assert(output.includes('Project usage unavailable'));
assert(!output.includes('999'));
const buttons=[];function walk(n){if(!n||typeof n!=='object')return;if(n.type==='button')buttons.push(n);for(const c of n.children)walk(c);}walk(tree);
for(const b of buttons.filter(b=>/^(Off|Minimal|Light|Balanced|Active)/.test(flatten(b))))assert.equal(b.props.disabled,true);
console.log('ok');
'''


class ProjectUsageUITests(unittest.TestCase):
    def check(self, script, harness):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for UI contract checks")
        result = subprocess.run([node, "-e", harness], input=json.dumps({"script": script}),
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_browser_project_form_and_unknown_tokens_are_scoped(self):
        self.check(JAVASCRIPT, BROWSER_CHECK)

    def test_dsh_project_counts_never_fall_back_to_shared_allowance(self):
        root = Path(__file__).resolve().parents[1]
        self.check((root / "host_resources/dsh/client/index.js").read_text(encoding="utf-8"), DSH_CHECK)
