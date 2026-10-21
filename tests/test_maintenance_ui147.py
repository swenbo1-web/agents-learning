"""Exercise shipped maintenance previews and summary strategy text without a host."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import JAVASCRIPT
from agent_learning.panel_ui import DIRECT_HTML, HTML, local_panel_html
from tests.test_browser_reader_ui import HARNESS as BROWSER_HARNESS
from tests.test_panel_ui import HARNESS as PANEL_HARNESS


PANEL_SCENARIO = r'''
 const local=script.includes("const launchMode='local'"),direct=script.includes("const launchMode='direct'");
 if(local){http[0].resolve({ok:true,json:async()=>directResult()._meta.agentLearningPanel});await flush();}
 else if(direct){const init=sent.find(m=>m.method==='ui/initialize');respond(init,{protocolVersion:'2026-01-26',hostCapabilities:{serverTools:{}}});await flush();respond(sent.filter(m=>m.method==='tools/call').at(-1),directResult());await flush();}
 else await boot();
 const preview={status:'preview',verified:false,retention_evaluated:false,migration_count:null,count:2,legacy_count:1,
  backup_root:'/external/backups',truncated:true,deletion_supported:false,entries:[
   {name:'legacy-one',version:'1.4.4',location:'legacy',retained:false,migration_allowed:true,needs_review:true},
   {name:'<script>untrusted name</script>',version:'1.4.6',location:'external',retained:false,migration_allowed:false,needs_review:true}]};
 async function show(backups){const promise=element('debug').onclick();const value=direct||local?directResult(false):result(false);
  value._meta.agentLearningPanel.diagnostics={version:1,project:{id:'p1'},backups};
  if(local){const req=http.at(-1);assert.equal(req.body.action,'debug');req.resolve({ok:true,json:async()=>value._meta.agentLearningPanel});}
  else{const req=sent.filter(m=>m.method==='tools/call').at(-1);assert.equal(req.params.arguments.action,'debug');respond(req,value);}
  await promise;await flush();}
 await show(preview);
 for(const lang of ['en','zh-CN']){element('language').onchange({target:{value:lang}});const visible=element('debugOutput').textContent;
  assert.equal(element('debugPanel').hidden,false);assert(visible.includes('/external/backups'));assert(visible.includes('legacy-one'));
  assert(visible.includes('<script>untrusted name</script>'));
  assert(visible.includes(lang==='en'?'Backups on this page: 2':'本页备份数量: 2'));
  assert(visible.includes(lang==='en'?'Preview only; no migration or deletion':'仅预览，不迁移或删除'));
  assert(visible.includes(lang==='en'?'Metadata preview only':'仅元数据预览'));
  assert(visible.includes(lang==='en'?'This list is incomplete':'列表未列全'));
  assert(!visible.includes(lang==='en'?'Verified for migration':'已核验可迁移'));}
 await show({status:'unavailable'});assert(!element('debugOutput').textContent.includes('legacy-one'));
 await show(undefined);assert(!element('debugOutput').textContent.includes('升级备份预览'));
 assert(!sent.some(m=>['ui/message','ui/update-model-context'].includes(m.method)));
 assert(!http.some(r=>r.url==='/api/action'&&r.body.action!=='debug'));
 assert(!sent.some(m=>m.method==='tools/call'&&m.params.name!=='learning_panel_open'&&m.params.arguments.action!=='debug'));
'''


AG_HARNESS = r'''
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const {runInNewContext}=require('node:vm'),assert=require('node:assert/strict');
class Element{constructor(){this.dataset={};this.hidden=false;this.children=[];this.events={};this.value='';this.textContent='';}
 addEventListener(name,fn){this.events[name]=fn;}append(...children){this.children.push(...children);}replaceChildren(){this.children=[];}}
const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
let changed;const requests=[],flush=()=>new Promise(resolve=>setImmediate(resolve));
const window={parent:{},addEventListener(){},confirm:()=>{throw Error('preview must not ask for a mutation');},sidecar:{conversationId:'chat',
 getWorkspaceUris:async()=>[],onWorkspaceChange:fn=>{changed=fn;},fetch:(url,options)=>new Promise(resolve=>requests.push({url,body:JSON.parse(options.body),resolve}))}};
runInNewContext(script,{window,document:{documentElement:{},getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[]},navigator:{language:'en'},crypto:globalThis.crypto,console});
(async()=>{await flush();changed(['file:///project']);requests[0].resolve({ok:true,json:async()=>({visible:true,capability:'cap',summary:{project_id:'p1',counts:{}}})});await flush();
 get('debug').events.click();assert.equal(requests[1].body.action,'debug');assert.equal(requests[1].body.confirm,undefined);
 requests[1].resolve({ok:true,json:async()=>({visible:true,summary:{project_id:'p1',counts:{}},diagnostics:{version:1,project:{id:'p1'},backups:{status:'preview',verified:false,count:1,legacy_count:1,backup_root:'/external/backups',scan_limited:true,deletion_supported:false,
  entries:[{name:'legacy-one',version:'1.4.4',location:'legacy',needs_review:true,migration_allowed:false,retained:false}]}}})});await flush();
 for(const language of ['en','zh-CN']){get('language').value=language;get('language').events.change();const text=get('debugOutput').textContent;
  assert(text.includes('/external/backups'));assert(text.includes('legacy-one'));assert(text.includes(language==='en'?'Backups on this page: 1':'本页备份数量: 1'));
  assert(text.includes(language==='en'?'Preview only; no migration or deletion':'仅预览，不迁移或删除'));assert(text.includes(language==='en'?'Metadata preview only':'仅元数据预览'));
  assert(text.includes(language==='en'?'This list is incomplete':'列表未列全'));}
 assert.equal(requests.length,2);changed([]);assert.equal(get('debugPanel').hidden,true);assert.equal(get('debugOutput').textContent,'');console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which("node"), "Node.js required for shipped UI behavior")
class MaintenanceUI147Tests(unittest.TestCase):
    def node(self, harness, script, scenario="maintenance147"):
        result = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"script": script, "scenario": scenario}), capture_output=True,
            text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def panel(self, html):
        harness = PANEL_HARNESS.replace("if(scenario==='bootstrap'){",
            "if(scenario==='maintenance147'){" + PANEL_SCENARIO + "}else if(scenario==='bootstrap'){")
        self.node(harness, re.search(r"<script>([\s\S]*)</script>", html).group(1))

    def test_codex_conversation_card_displays_read_only_bilingual_preview(self):
        self.panel(HTML)

    def test_codex_native_menu_displays_preview_without_chat_messages(self):
        self.panel(DIRECT_HTML)

    def test_shared_local_console_displays_preview_without_chat_messages(self):
        self.panel(local_panel_html())

    def test_antigravity_preview_and_project_switch(self):
        path = Path(__file__).resolve().parents[1] / "host_resources/antigravity/sidecars/control/index.html"
        script = re.findall(r"<script>([\s\S]*?)</script>", path.read_text(encoding="utf-8"))[-1]
        self.node(AG_HARNESS, script)

    def test_summary_strategy_is_localized_read_only_and_old_runtime_safe(self):
        harness = BROWSER_HARNESS.split("(async()=>{")[0] + r'''
run("state.session={admin_control_enabled:true};state.adminToken='admin';state.view='admin-control'");
for(const language of ['en','zh-CN']){
 run(`state.language=${JSON.stringify(language)}`);
 run("renderSummaryStrategy({work_summary:{enabled:true,strategy:'balanced'}})");
 assert(get('admin-summary-strategy').textContent.includes(language==='en'?'reuse complete public records directly':'完整公开记录直接复用'));
 assert(get('admin-summary-strategy').textContent.includes(language==='en'?'Stage review has priority':'阶段资料优先阶段审核'));
 run("renderSummaryStrategy({work_summary:{enabled:true,strategy:'all_windows'}})");
 assert(get('admin-summary-strategy').textContent.includes(language==='en'?'All-window summary strategy':'全部窗口摘要策略'));
 for(const value of ['{}',"{work_summary:{strategy:'unknown'}}"]){run(`renderSummaryStrategy(${value})`);assert(get('admin-summary-strategy').textContent.includes(language==='en'?'did not report':'未报告'));}
}
assert.equal(requests.length,0);assert(!run('ADMIN_FIELDS').includes('admin-summary-strategy'));console.log('ok');
'''
        self.node(harness, JAVASCRIPT)


if __name__ == "__main__":
    unittest.main()
