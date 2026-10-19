"""Stage/token review controls across the shipped project panels; no host model."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_state
from agent_learning.dsh_panel import apply_preset, snapshot
from agent_learning.panel_control import PanelControl
from agent_learning.panel_ui import HTML
from agent_learning.phase_review import configure
from agent_learning.runtime import ContractError, Runtime


ROOT = Path(__file__).resolve().parents[1]


class PhasePanelContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-panel-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.base / "home", config={}, host="dsh")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def configure_phase(self):
        configure(self.r, self.p, {"mode": "phase", "every_phases": 2, "daily_tokens": 500000}, 0)

    def test_both_backend_snapshots_expose_current_host_policy_without_writes(self):
        self.configure_phase()
        panel = PanelControl(self.r, self.p, self.workspace, None)
        panel.binding = {"workspace": str(self.workspace)}
        before = self.r.db.total_changes
        native, dsh = panel._summary(), snapshot(self.r, self.p)
        self.assertEqual(self.r.db.total_changes, before)
        for view in (native, dsh):
            self.assertEqual(view["phase_review"]["policy"]["every_phases"], 2)
            self.assertEqual(view["phase_review"]["host_scope"], self.r._session_scope())
        self.assertEqual(native["project_usage"]["budget_mode"], "tokens")

    def test_dsh_stage_mode_rejects_legacy_preset_and_can_pause_without_budget_change(self):
        self.configure_phase()
        view = snapshot(self.r, self.p)
        payload = {"admin_preset": "balanced", "panel_snapshot": view,
                   "request_id": "stage-preset", "confirm": True}
        with self.assertRaisesRegex(ContractError, "token budgets"):
            apply_preset(self.r, self.p, payload)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_control_receipts").fetchone()[0], 0)
        with patch("agent_learning.admin_control.wake_admin") as wake:
            apply_preset(self.r, self.p, {**payload, "admin_preset": "off"})
        wake.assert_not_called()
        self.assertFalse(control_state(self.r, self.p)["enabled"])
        self.assertEqual(snapshot(self.r, self.p)["phase_review"]["policy"]["daily_tokens"], 500000)

    def test_native_stage_mode_rejects_legacy_preset_before_registering_request(self):
        self.configure_phase()
        panel = PanelControl(self.r, self.p, self.workspace, None)
        with patch.object(panel, "_verify"):
            with self.assertRaisesRegex(ContractError, "token budgets"):
                panel.action("cap", "set_admin_preset", request_id="wrong-preset", preset="light",
                             expected=snapshot(self.r, self.p)["expected"], confirm=True)
        self.assertEqual(panel.requests, {})

    def test_dsh_first_enable_receipt_replays_after_default_switch_to_stage_mode(self):
        payload = {"admin_preset": "light", "panel_snapshot": snapshot(self.r, self.p),
                   "request_id": "first-enable", "confirm": True}
        with patch("agent_learning.admin_control.wake_admin", return_value={"status": "accepted"}) as wake:
            first = apply_preset(self.r, self.p, payload)
            repeated = apply_preset(self.r, self.p, payload)
        self.assertEqual(first, repeated)
        self.assertEqual(wake.call_count, 1)
        self.assertEqual(snapshot(self.r, self.p)["phase_review"]["policy"]["mode"], "phase")
        with self.assertRaisesRegex(ContractError, "reused"):
            apply_preset(self.r, self.p, {**payload, "admin_preset": "active"})


@unittest.skipUnless(shutil.which("node"), "Node.js required for shipped panel behavior")
class PhasePanelUITests(unittest.TestCase):
    def node(self, code, payload):
        result = subprocess.run([shutil.which("node"), "-e", code], input=json.dumps(payload),
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_local_deferral_is_separate_localized_and_old_response_safe(self):
        from tests.test_panel_ui import HARNESS
        body = r"""
 await boot();
 element('preset').options=['off','minimal','light','balanced','active','custom'].map(value=>({value}));
 for(const variant of ['count','phase_fallback','zero','old']){
  const staged={...snapshot,phase_review:{host_scope:'codex:default',policy:{mode:'phase',every_phases:1},
   phases:{sealed:2,queued:1,partial:1,completed:4},recent_batches:[{coverage_complete:true}]}};
  const view=staged.phase_review;
  if(variant!=='old')view.phases.routine_deferred=3;
  if(variant==='count')view.locally_deferred_phase_count=3;
  if(variant==='zero')view.locally_deferred_phase_count=0;
  const promise=element('refresh').onclick(),request=sent.filter(m=>m.method==='tools/call').at(-1);
  assert.equal(request.params.arguments.action,'status');respond(request,result(false,staged));await promise;await flush();
  const visible=['count','phase_fallback'].includes(variant);
  for(const language of ['en','zh-CN']){
   element('language').onchange({target:{value:language}});const text=element('budget').textContent;
   assert.match(text,/0 \/ 2 \/ 1 \/ 1 \/ 4/,'deferred stages must not alter the five review counts');
   assert.equal(text.includes(language==='en'?'Pure status checks retained locally: 3':'纯状态检查本地暂存：3 个阶段'),visible,variant);
   if(visible){assert(text.includes(language==='en'?'some material remains unreviewed':'仍有未审核资料'));assert(text.includes(language==='en'?'Only the remaining material has not been submitted':'仅余下资料尚未送审，不算审核完成'));assert(!text.includes(language==='en'?'not model-reviewed':'尚未模型审核'));assert(text.includes(language==='en'?'Check and continue':'检查并继续整理'));}
   assert(text.includes(language==='en'?'Latest batch coverage: Complete':'最近一批覆盖: 完整'));
  }
 }
 assert(!sent.some(m=>['ui/message','ui/update-model-context'].includes(m.method)));
 assert(!sent.some(m=>m.method==='tools/call'&&!['learning_panel_open','learning_panel_action'].includes(m.params.name)));
"""
        harness = HARNESS.replace("else throw Error('unknown scenario');", "else if(scenario==='phase_deferred'){" + body + "}else throw Error('unknown scenario');")
        script = re.search(r"<script>([\s\S]*)</script>", HTML).group(1)
        self.node(harness, {"script": script, "scenario": "phase_deferred"})

    def test_dsh_local_deferral_is_separate_localized_and_old_response_safe(self):
        self.node(r"""
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict');let factory,language='en';
const window={__ModuleLoader__:{load:value=>factory=value.factory}};
vm.runInNewContext(script.replace('exports.apply = apply;', 'exports.buildControlPanel = buildControlPanel; exports.apply = apply;'),{window,navigator:{language:'en'}});
const plugin=factory(()=>({}));
const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),useState:value=>[value,()=>{}],useCallback:fn=>fn,useEffect(){}};
const Panel=plugin.buildControlPanel(react,{current:{getSnapshot:()=>({active:language})}});
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
for(const variant of ['count','phase_fallback','zero','old']){
 const view={host_scope:'dsh:default',policy:{mode:'phase',every_phases:1},phases:{sealed:2,queued:1,partial:1,completed:4},recent_batches:[{coverage_complete:true}]};
 if(variant!=='old')view.phases.routine_deferred=3;
 if(variant==='count')view.locally_deferred_phase_count=3;
 if(variant==='zero')view.locally_deferred_phase_count=0;
 const summary={project_id:'p',counts:{},panel_snapshot:{host_scope:'dsh:default',phase_review:view}};
 const visible=['count','phase_fallback'].includes(variant);
 for(language of ['en','zh-CN']){
  const text=nodes(Panel({initialSummary:summary})).flatMap(n=>(n.children||[]).filter(v=>typeof v==='string')).join('\n');
  assert.match(text,/0 \/ 2 \/ 1 \/ 1 \/ 4/,'deferred stages must not alter the five review counts');
  assert.equal(text.includes(language==='en'?'Pure status checks retained locally: 3':'纯状态检查本地暂存：3 个阶段'),visible,variant);
  if(visible){assert(text.includes(language==='en'?'some material remains unreviewed':'仍有未审核资料'));assert(text.includes(language==='en'?'Only the remaining material has not been submitted':'仅余下资料尚未送审，不算审核完成'));assert(!text.includes(language==='en'?'not model-reviewed':'尚未模型审核'));assert(text.includes(language==='en'?'Check and continue':'检查并继续整理'));}
  assert(text.includes(language==='en'?'Latest batch coverage: Complete':'最近一批覆盖: 完整'));
 }
}
""", {"script": (ROOT / "host_resources/dsh/client/index.js").read_text(encoding="utf-8")})

    def test_antigravity_local_deferral_is_separate_localized_and_old_response_safe(self):
        html = (ROOT / "host_resources/antigravity/sidecars/control/index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>([\s\S]*)</script>", html).group(1)
        self.node(r"""
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict'),nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{id,children:[],dataset:{},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},append(...values){this.children.push(...values);},replaceChildren(){this.children=[];}});return nodes.get(id);}
const window={parent:{},addEventListener(){}},document={documentElement:{},getElementById:element,createElement:()=>({children:[],dataset:{},addEventListener(){},append(...values){this.children.push(...values);}}),querySelectorAll:()=>[]};
const exportAt='  render(); hide("waiting");';
const exposed=script.slice(0,script.indexOf(exportAt))+'\n globalThis.show=(s)=>{summary=s;capability="cap";render();};})();';
const context={window,document,navigator:{language:'en'}};vm.createContext(context);vm.runInContext(exposed,context);
for(const variant of ['count','phase_fallback','zero','old']){
 const view={host_scope:'antigravity:default',policy:{mode:'phase',every_phases:1},phases:{sealed:2,queued:1,partial:1,completed:4},recent_batches:[{coverage_complete:true}]};
 if(variant!=='old')view.phases.routine_deferred=3;
 if(variant==='count')view.locally_deferred_phase_count=3;
 if(variant==='zero')view.locally_deferred_phase_count=0;
 context.show({project_id:'p',host_scope:'antigravity:default',counts:{},phase_review:view});
 const visible=['count','phase_fallback'].includes(variant);
 for(const language of ['en','zh-CN']){
  element('language').value=language;element('language').listeners.change();const text=element('admin').textContent;
  assert.match(text,/0 \/ 2 \/ 1 \/ 1 \/ 4/,'deferred stages must not alter the five review counts');
  assert.equal(text.includes(language==='en'?'Pure status checks retained locally: 3':'纯状态检查本地暂存：3 个阶段'),visible,variant);
  if(visible){assert(text.includes(language==='en'?'some material remains unreviewed':'仍有未审核资料'));assert(text.includes(language==='en'?'Only the remaining material has not been submitted':'仅余下资料尚未送审，不算审核完成'));assert(!text.includes(language==='en'?'not model-reviewed':'尚未模型审核'));assert(text.includes(language==='en'?'Check and continue':'检查并继续整理'));}
  assert(text.includes(language==='en'?'Latest batch coverage: Complete':'最近一批覆盖: 完整'));
 }
}
""", {"script": script})

    def test_codex_card_displays_tokens_stage_progress_and_only_allows_pause(self):
        from tests.test_panel_ui import HARNESS
        body = r"""
 await boot();
 element('preset').options=['off','minimal','light','balanced','active','custom'].map(value=>({value}));
 const staged={...snapshot,admin_admission_enabled:true,
   phase_review:{host_scope:'codex:default',policy:{mode:'phase',every_phases:2},phases:{sealed:3,partial:1,completed:4},recent_batches:[{coverage_complete:false}]},
   project_usage:{...snapshot.project_usage,budget_mode:'tokens',reported_tokens:1234,token_reservations:4000,remaining_reported_tokens:994766,max_reported_tokens_per_day:1000000}};
 const promise=element('refresh').onclick(),request=sent.filter(m=>m.method==='tools/call').at(-1);
 respond(request,result(false,staged));await promise;await flush();
 assert.match(element('budget').textContent,/Project reported tokens today: 1234/);
 assert.match(element('budget').textContent,/reserved: 4000/);
 assert.match(element('budget').textContent,/Review every 2 delivered stages/);
 assert.match(element('budget').textContent,/0 \/ 3 \/ 0 \/ 1 \/ 4/);
 assert(!element('budget').textContent.includes('Project calls remaining'));
 assert(element('preset').options.filter(o=>!['off','custom'].includes(o.value)).every(o=>o.hidden&&o.disabled));
 assert.equal(element('preset').value,'custom');assert.equal(element('apply').disabled,true);
 element('preset').value='balanced';element('apply').onclick();assert.equal(element('confirmBox').hidden,true);
 element('language').onchange({target:{value:'zh-CN'}});assert.match(element('budget').textContent,/每 2 个交付阶段/);
 element('preset').value='off';element('preset').onchange();assert.equal(element('apply').disabled,false);
 element('apply').onclick();assert.match(element('confirmText').textContent,/暂停当前项目/);element('cancel').onclick();
 assert(!sent.some(m=>['ui/message','ui/update-model-context'].includes(m.method)));
"""
        harness = HARNESS.replace("else throw Error('unknown scenario');", "else if(scenario==='phase_ui'){" + body + "}else throw Error('unknown scenario');")
        script = re.search(r"<script>([\s\S]*)</script>", HTML).group(1)
        self.node(harness, {"script": script, "scenario": "phase_ui"})

    def test_dsh_client_uses_scoped_snapshot_and_preserves_only_pause_chip(self):
        self.node(r"""
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict');let factory;
const window={__ModuleLoader__:{load:value=>factory=value.factory}};
vm.runInNewContext(script.replace('exports.apply = apply;', 'exports.buildControlPanel = buildControlPanel; exports.apply = apply;'),{window,navigator:{language:'en'}});
const plugin=factory(()=>({}));
const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),useState:value=>[value,()=>{}],useCallback:fn=>fn,useEffect(){}};
const summary={project_id:'p',admin_admission_enabled:true,project_name:'P',panel_snapshot:{host_scope:'dsh:default',phase_review:{host_scope:'dsh:default',policy:{mode:'phase',every_phases:3},phases:{sealed:2},recent_batches:[{coverage_complete:true}]}},project_usage:{scope:'project',project_id:'p',budget_mode:'tokens',reported_tokens:888,token_reservations:1000,remaining_reported_tokens:999,max_reported_tokens_per_day:1000000}};
const Panel=plugin.buildControlPanel(react,null),tree=Panel({initialSummary:summary});
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
const all=nodes(tree),text=all.flatMap(n=>(n.children||[]).filter(v=>typeof v==='string')).join('\n');
const chips=all.filter(n=>n.type==='button'&&n.props.key);
assert.deepEqual(chips.map(n=>n.props.key),['off']);assert.match(text,/Reported tokens today/);assert.match(text,/888/);assert.match(text,/Review every 3 delivered stages/);assert.match(text,/Latest batch coverage: Complete/);assert(!text.includes('Project allowance left'));
""", {"script": (ROOT / "host_resources/dsh/client/index.js").read_text(encoding="utf-8")})

    def test_antigravity_panel_displays_stage_summary_without_legacy_presets(self):
        html = (ROOT / "host_resources/antigravity/sidecars/control/index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>([\s\S]*)</script>", html).group(1)
        self.node(r"""
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict'),nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{id,children:[],dataset:{},addEventListener(){},append(...values){this.children.push(...values);},replaceChildren(){this.children=[];}});return nodes.get(id);}
const window={parent:{},addEventListener(){}},document={documentElement:{},getElementById:element,createElement:()=>({children:[],dataset:{},addEventListener(){},append(...values){this.children.push(...values);}}),querySelectorAll:()=>[]};
const exportAt='  render(); hide("waiting");';
const summary={project_id:'p',host_scope:'antigravity:default',counts:{pending_review_cases:2},phase_review:{host_scope:'antigravity:default',policy:{mode:'phase',every_phases:2},phases:{partial:1},recent_batches:[{coverage_complete:false}]},project_usage:{scope:'project',project_id:'p',budget_mode:'tokens',reported_tokens:300,token_reservations:500,remaining_reported_tokens:999,max_reported_tokens_per_day:1000000}};
const exposed=script.slice(0,script.indexOf(exportAt))+'\n globalThis.show=(s)=>{summary=s;capability="cap";render();};})();';
const context={window,document,navigator:{language:'en'}};vm.createContext(context);vm.runInContext(exposed,context);context.show(summary);
assert.equal(element('presets').children.length,1);assert.equal(element('presets').children[0].textContent,'Off');assert.match(element('admin').textContent,/Project reported tokens today: 300/);assert.match(element('admin').textContent,/Review every 2 delivered stages/);assert.match(element('admin').textContent,/Partial; further work is retained/);assert(!element('admin').textContent.includes('Project calls remaining'));
""", {"script": script})
