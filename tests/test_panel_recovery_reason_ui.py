"""Three shipped controls render recovery categories in the selected language."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from agent_learning.panel_ui import HTML
from tests.test_panel_ui import HARNESS


ROOT = Path(__file__).resolve().parents[1]
LABELS = {
    "zh-CN": {
        "failed_call_requires_review": "旧失败调用待确认，可在审批与设置中选择恢复操作。",
        "unknown_call_requires_review": "上次调用结果待确认，请先核对后再决定是否重试。",
        "future_reason_code": "future_reason_code",
    },
    "en": {
        "failed_call_requires_review": "A previous call failed. Review recovery options in Approval & settings.",
        "unknown_call_requires_review": "A previous call outcome is unknown. Verify it before deciding whether to retry.",
        "future_reason_code": "future_reason_code",
    },
}


class PanelRecoveryReasonUITests(unittest.TestCase):
    def run_node(self, code, value):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required")
        result = subprocess.run([node, "-e", code], input=json.dumps(value, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_codex_card_translates_summary_and_debug_without_actions(self):
        scenario = r'''
if(scenario==='dispatch_reason'){
 await boot();
 const labels=__LABELS__;
 for(const [locale,values] of Object.entries(labels)){
  element('language').value=locale;element('language').onchange({target:element('language')});
  for(const [reason,expected] of Object.entries(values)){
   const s={...snapshot,reason};
   let promise=element('refresh').onclick(),request=sent.filter(m=>m.method==='tools/call').at(-1);
   respond(request,result(false,s));await promise;await flush();
   assert(element('admin').textContent.includes(expected));
   promise=element('debug').onclick();request=sent.filter(m=>m.method==='tools/call').at(-1);
   const response=result(false,s);
   response._meta.agentLearningPanel.diagnostics={version:1,project:{id:'p1'},administrator:{phase:'idle',reason}};
   respond(request,response);await promise;await flush();
   assert.equal(element('debugPanel').hidden,false);
   assert(element('debugOutput').textContent.includes(expected));
  }
 }
 assert(!sent.some(m=>['ui/message','ui/update-model-context'].includes(m.method)));
 assert(sent.filter(m=>m.method==='tools/call').every(m=>['status','debug'].includes(m.params.arguments.action)));
}else '''.replace("__LABELS__", json.dumps(LABELS, ensure_ascii=False))
        self.assertEqual(HARNESS.count("if(scenario==='bootstrap'){"), 1)
        code = HARNESS.replace("if(scenario==='bootstrap'){", scenario + "if(scenario==='bootstrap'){")
        script = re.search(r"<script>([\s\S]*)</script>", HTML).group(1)
        self.run_node(code, {"script": script, "scenario": "dispatch_reason"})

    def test_antigravity_sidecar_translates_summary_and_debug(self):
        code = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(){this.dataset={};this.hidden=false;this.children=[];this.events={};this.value='';this.textContent='';}
 addEventListener(name,fn){this.events[name]=fn;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(){this.children=[];}
}
const ids=['language','panel','notice','result','refresh','debug','debugPanel','debugTitle','debugOutput','browser','review','frequencyTitle','shared','quota','historyTitle','historyNote','history','project','workspace','stats','admin','presets'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
const document={documentElement:{},getElementById:id=>elements[id],createElement:()=>new Element(),querySelectorAll:()=>[]};
let summary={project_id:'p1',project_name:'project',counts:{active:1}},calls=[];
const window={parent:{},addEventListener(){},confirm(){throw Error('no action confirmation expected');},sidecar:{
 conversationId:'synthetic-ui',getWorkspaceUris:async()=>['file:///synthetic-project'],onWorkspaceChange(){},
 fetch:async(url,options)=>{const body=JSON.parse(options.body);calls.push([url,body]);
  assert(url==='/api/bootstrap'||url==='/api/action'&&body.action==='debug');
  const response={visible:true,capability:'fake-private',summary};
  if(body.action==='debug')response.diagnostics={version:1,project:{id:'p1'},administrator:{phase:'idle',reason:summary.reason}};
  return {ok:true,json:async()=>response};}
}};
vm.runInNewContext(input.script,{window,document,navigator:{language:'en'},crypto:globalThis.crypto,URL,console});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await flush();await flush();assert.equal(elements.panel.hidden,false);
 for(const [locale,values] of Object.entries(input.labels)){
  elements.language.value=locale;elements.language.events.change();
  for(const [reason,expected] of Object.entries(values)){
   summary={...summary,reason};await elements.refresh.events.click();await flush();
   assert(elements.admin.textContent.includes(expected));
   await elements.debug.events.click();await flush();
   assert.equal(elements.debugPanel.hidden,false);
   assert(elements.debugOutput.textContent.includes(expected));
  }
 }
 console.log('passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        html = (ROOT / "host_resources/antigravity/sidecars/control/index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>([\s\S]*)</script>", html).group(1)
        self.run_node(code, {"script": script, "labels": LABELS})

    def test_dsh_native_panel_displays_and_translates_diagnostic_reason(self):
        code = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));let factory;
const window={__ModuleLoader__:{load:item=>{factory=item.factory;}}};
const source=input.source.replace('exports.apply = apply;','exports.buildControlPanel=buildControlPanel; exports.apply = apply;');
vm.runInNewContext(source,{window,navigator:{language:'en'},fetch(){throw Error('no transport call expected');}});
const plugin=factory(()=>({}));
for(const [locale,values] of Object.entries(input.labels)){
 for(const [reason,expected] of Object.entries(values)){
  const states=[];let cursor=0;
  const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),
   useState:value=>{const i=cursor++;if(!(i in states))states[i]=value;return [states[i],value=>{states[i]=value;}];},
   useCallback:fn=>fn,useEffect(){}};
  const localeRef={current:{getSnapshot:()=>({active:locale})}};
  const Panel=plugin.buildControlPanel(react,localeRef);
  const tree=Panel({initialSummary:{ok:true,installed:true,project_id:'p1',project_name:'project',diagnostic:{phase:'idle',reason}},cwd:'synthetic-project'});
  const text=JSON.stringify(tree);assert(text.includes(expected));
  if(reason!=='future_reason_code')assert(!text.includes(reason));
 }
}
console.log('passed');
'''
        source = (ROOT / "host_resources/dsh/client/index.js").read_text(encoding="utf-8")
        self.run_node(code, {"source": source, "labels": LABELS})


if __name__ == "__main__":
    unittest.main()
