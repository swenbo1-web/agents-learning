"""The shipped DSH settings card must not offer controls without a project."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "host_resources/dsh/client/index.js"
NODE = shutil.which("node")


HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const deferred=()=>{let resolve;const promise=new Promise(yes=>{resolve=yes;});return{promise,resolve};};
const response=(body,ok=true)=>({ok,status:ok?200:503,json:async()=>body});
const summary=name=>({ok:true,installed:true,project_id:name,project_name:name,workspace:'/fixtures/'+name,
  active_knowledge:7,admin_admission_enabled:true,
  project_usage:{scope:'project',project_id:name,max_calls_per_day:15},
  panel_snapshot:{installation_id:'install-'+name,expected:{}}});
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
const text=n=>!n?'':typeof n!=='object'?String(n):(n.children||[]).map(text).join(' ');
function harness(language='en',initialProps={},kind='panel'){
 let factory,current=null,cursor=0,dirty=true,rootTree=null,effects=[],props=initialProps,timerId=0;
 const instances=new Map(),calls=[],posts=[],timers=new Map();
 const context={window:{__ModuleLoader__:{load:value=>{factory=value.factory;}},confirm:()=>true},
  navigator:{language},fetch:(url,options)=>{const wait=deferred(),call={url,options,wait};
    if(options)posts.push(call);else calls.push(call);return wait.promise;},
  setTimeout:(fn,ms)=>{assert.equal(ms,15000);const id=++timerId;timers.set(id,fn);return id;},
  clearTimeout:id=>timers.delete(id)};
 const patched=source.replace('exports.apply = apply;',
  'exports.buildControlPanel=buildControlPanel; exports.createSettingsCard=createSettingsCard; exports.apply = apply;');
 assert.notEqual(patched,source);vm.runInNewContext(patched,context);
 const plugin=factory(()=>({})),same=(a,b)=>a&&b&&a.length===b.length&&a.every((x,i)=>Object.is(x,b[i]));
 const slot=initial=>{const index=cursor++;if(!(index in current.slots))current.slots[index]=initial;return index;};
 const react={
  createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
  useState:initial=>{const instance=current,index=slot(initial);return[instance.slots[index],value=>{
    const old=instance.slots[index],next=typeof value==='function'?value(old):value;
    if(!Object.is(old,next)){instance.slots[index]=next;dirty=true;}
  }];},
  useCallback:(fn,deps)=>{const index=slot(null),old=current.slots[index];
    if(!old||!same(old.deps,deps))current.slots[index]={fn,deps};return current.slots[index].fn;},
  useEffect:(create,deps)=>{const instance=current,index=slot(null),old=instance.slots[index];
    if(!old||!same(old.deps,deps)){instance.slots[index]={deps};effects.push(()=>{
      old&&old.cleanup&&old.cleanup();const cleanup=create();instance.slots[index].cleanup=cleanup;});}}
 };
 const Root=kind==='settings'?plugin.createSettingsCard(react,null):plugin.buildControlPanel(react,null);
 function render(){
  dirty=false;effects=[];
  function walk(node,path){
   if(!node||typeof node!=='object')return node;
   if(typeof node.type==='function'){
    let instance=instances.get(path);if(!instance){instance={slots:[]};instances.set(path,instance);}
    const previous=current,oldCursor=cursor;current=instance;cursor=0;
    const value=node.type(node.props);current=previous;cursor=oldCursor;return walk(value,path+'/render');
   }
   return{...node,children:(node.children||[]).map((child,i)=>walk(child,path+'/'+i))};
  }
  rootTree=walk({type:Root,props},'root');return rootTree;
 }
 function commit(){render();const pending=effects;effects=[];for(const effect of pending)effect();return rootTree;}
 async function flush(){for(let i=0;i<8;i++){await new Promise(resolve=>setImmediate(resolve));if(dirty)commit();}}
 function button(fragment){return nodes(rootTree).find(n=>n.type==='button'&&text(n).includes(fragment));}
 function refresh(){const b=button(language.startsWith('en')?'Refresh':'刷新');assert(b);b.props.onClick();}
 function answer(index,body,ok=true){assert(calls[index]);calls[index].wait.resolve(response(body,ok));}
 return{calls,posts,timers,commit,flush,button,refresh,answer,text:()=>text(rootTree),tree:()=>rootTree,
  setProps:value=>{props=value;dirty=true;}};
}
const unbound=language=>language.startsWith('en')?'Open the knowledge panel from a conversation in an installed project':'请从已安装项目的对话打开知识库';
function onlyRefresh(h,language){
 const buttons=nodes(h.tree()).filter(n=>n.type==='button');
 assert.equal(buttons.length,1,'unbound/unknown project must only show Refresh');
 assert(text(buttons[0]).includes(language.startsWith('en')?'Refresh':'刷新'));
 assert(!h.text().includes(language.startsWith('en')?'Published Knowledge':'正式知识'));
 assert(!h.text().includes(language.startsWith('en')?'Disabled':'已关闭'));
 assert.equal(h.posts.length,0);
}
"""


@unittest.skipUnless(NODE, "Node.js required for the shipped DSH panel")
class DshUnboundPanelTests(unittest.TestCase):
    def run_client(self, scenario):
        code = HARNESS + "\n(async()=>{\n" + scenario + "\nconsole.log('ok');\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run([NODE, "-e", code, str(CLIENT)], text=True, encoding="utf-8",
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_settings_cold_open_loading_then_localized_unbound_state(self):
        self.run_client(r"""
for(const language of ['en','zh-CN']){
 const h=harness(language,{},'settings');h.commit();assert.equal(h.calls.length,1);
 assert(h.text().includes(language==='en'?'Loading status':'正在读取状态'));
 onlyRefresh(h,language);assert(!h.text().includes(unbound(language)));
 h.answer(0,{ok:true,installed:false,reason:'workspace_required'});await h.flush();
 assert(h.text().includes(unbound(language)),h.text());onlyRefresh(h,language);
 assert(!h.text().includes(language==='en'?'Loading status':'正在读取状态'));
 assert.equal(h.timers.size,0);
}
""")

    def test_uninstalled_residual_identity_never_displays_project_or_actions(self):
        self.run_client(r"""
for(const language of ['en','zh-CN'])for(const invalid of [
 {...summary('STALE_PROJECT'),installed:false},
 {...summary('STALE_PROJECT'),installed:true,ok:false},
 {...summary('STALE_PROJECT'),installed:true,project_id:''},
 {...summary('STALE_PROJECT'),project_id:'   '},
 {...summary('STALE_PROJECT'),project_id:123}
]){
 const h=harness(language,{cwd:'scope-a',sessionId:'chat-a',initialSummary:invalid,
  initialScope:JSON.stringify(['scope-a','chat-a'])});h.commit();
 assert(h.text().includes(unbound(language)));assert(!h.text().includes('STALE_PROJECT'));
 onlyRefresh(h,language);
 h.answer(0,{ok:true,installed:false});await h.flush();onlyRefresh(h,language);
}
""")

    def test_unbound_read_failure_is_distinct_and_recovers_on_refresh(self):
        self.run_client(r"""
for(const language of ['en','zh-CN']){
 const h=harness(language,{},'settings');h.commit();
 h.answer(0,{ok:false,reason:'database_read_unavailable',error:'PRIVATE'},false);await h.flush();
 assert(h.text().includes(language==='en'?'temporarily unavailable':'暂时无法读取'));
 assert(!h.text().includes(unbound(language)));assert(!h.text().includes('PRIVATE'));
 assert(!h.text().includes(language==='en'?'Loading status':'正在读取状态'));onlyRefresh(h,language);
 h.refresh();h.answer(1,{ok:true,installed:false});await h.flush();
 assert(h.text().includes(unbound(language)));assert(!h.text().includes(language==='en'?'temporarily unavailable':'暂时无法读取'));
 onlyRefresh(h,language);
}
""")

    def test_bound_scope_to_unbound_hides_old_controls_and_ignores_late_reply(self):
        self.run_client(r"""
const language='en',h=harness(language,{cwd:'scope-a',sessionId:'chat-a'});h.commit();
h.answer(0,summary('PROJECT_A'));await h.flush();assert(h.button('Open Review'));
h.refresh();assert.equal(h.calls.length,2);
h.setProps({cwd:'scope-b',sessionId:'chat-b'});h.commit();onlyRefresh(h,language);
assert(!h.text().includes('PROJECT_A'));
h.answer(2,{ok:true,installed:false});await h.flush();
h.answer(1,summary('LATE_A'));await h.flush();
assert(h.text().includes(unbound(language)));assert(!h.text().includes('LATE_A'));onlyRefresh(h,language);
""")

    def test_binding_recovery_restores_real_controls_and_keeps_confirmed_read_on_failure(self):
        self.run_client(r"""
const h=harness();h.commit();h.answer(0,{ok:true,installed:false});await h.flush();onlyRefresh(h,'en');
h.setProps({cwd:'scope-a',sessionId:'chat-a'});h.commit();
h.answer(1,summary('PROJECT_A'));await h.flush();assert(h.text().includes('PROJECT_A'));
assert(h.text().includes('Published Knowledge'));assert(h.button('Open Review'));
h.refresh();h.answer(2,{ok:false,reason:'query_budget_exceeded'},false);await h.flush();
assert(h.text().includes('PROJECT_A'));assert(h.text().includes('exceeded its read budget'));
h.button('Open Visual Knowledge').props.onClick();assert.equal(h.posts.length,1);
const payload=JSON.parse(h.posts[0].options.body);
assert.equal(payload.action,'open_browser');assert.equal(payload.project_id,'PROJECT_A');
assert.equal(payload.installation_id,'install-PROJECT_A');assert.equal(payload.cwd,'scope-a');
assert.equal(payload.sessionId,'chat-a');h.posts[0].wait.resolve(response({ok:true}));await h.flush();
h.answer(3,summary('PROJECT_A'));await h.flush();assert.equal(h.timers.size,0);
""")

    def test_action_guard_rejects_installed_false_even_with_a_retained_button(self):
        self.run_client(r"""
const data=summary('PROJECT_A'),h=harness('en',{initialSummary:data,initialScope:JSON.stringify(['',''])});
h.commit();const retained=h.button('Open Visual Knowledge');assert(retained);
data.installed=false;retained.props.onClick();
assert.equal(h.posts.length,0,'an uninstalled project ID must not authorize a panel action');
h.answer(0,{ok:true,installed:false});await h.flush();onlyRefresh(h,'en');
""")

    def test_previous_scope_error_and_retained_action_do_not_follow_scope_switch(self):
        self.run_client(r"""
const h=harness('en',{cwd:'scope-a',sessionId:'chat-a'});h.commit();
h.answer(0,summary('PROJECT_A'));await h.flush();const retained=h.button('Open Visual Knowledge');
h.refresh();h.answer(1,{ok:false,reason:'query_budget_exceeded'},false);await h.flush();
assert(h.text().includes('exceeded its read budget'));
h.setProps({cwd:'scope-b',sessionId:'chat-b'});h.commit();
assert(!h.text().includes('exceeded its read budget'));assert(h.text().includes('Loading status'));
retained.props.onClick();assert.equal(h.posts.length,0,'old scope button must not start a write');
h.answer(2,summary('PROJECT_B'));await h.flush();assert(h.button('Open Review'));
assert(!h.text().includes('exceeded its read budget'));assert(!h.text().includes('PROJECT_A'));
""")

    def test_identical_errors_in_new_scope_repaint_instead_of_remaining_loading(self):
        self.run_client(r"""
for(const language of ['en','zh-CN']){
 const h=harness(language,{cwd:'scope-a',sessionId:'chat-a'});h.commit();
 const failure={ok:false,reason:'query_budget_exceeded'};
 h.answer(0,failure,false);await h.flush();
 const fragment=language==='en'?'exceeded its read budget':'本地查询预算';assert(h.text().includes(fragment));
 h.setProps({cwd:'scope-b',sessionId:'chat-b'});h.commit();
 assert(h.text().includes(language==='en'?'Loading status':'正在读取状态'));
 h.answer(1,failure,false);await h.flush();assert(h.text().includes(fragment),h.text());onlyRefresh(h,language);
 assert(!h.text().includes(language==='en'?'Loading status':'正在读取状态'));
}
""")


if __name__ == "__main__":
    unittest.main()
