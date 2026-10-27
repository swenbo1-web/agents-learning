"""The shipped composer and panel must agree on summary ownership across mounts."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "host_resources" / "dsh" / "client" / "index.js"
NODE = shutil.which("node")


HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const summary=(name,cwd)=>({ok:true,installed:true,project_id:name,project_name:name,workspace:cwd,
  project_usage:{scope:'project',project_id:name,max_calls_per_day:15},
  panel_snapshot:{installation_id:'install-'+name,expected:{}}});
const A='/fixtures/project-a',B='/fixtures/project-b';
const response=(body,ok=true)=>({ok,status:ok?200:503,json:async()=>body});
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
const text=n=>!n?'':typeof n!=='object'?String(n):(n.children||[]).map(text).join(' ');
function harness(kind='composer',initialProps){
 let factory,current=null,cursor=0,dirty=true,prepared=false,rootTree=null,effects=[],props=initialProps||{sessionId:'chat-a'};
 let sessionCwd=A,timerId=0;const instances=new Map(),calls=[],pending=[],timers=new Map();
 const context={window:{__ModuleLoader__:{load:x=>{factory=x.factory;}}},navigator:{language:'en'},
   document:{addEventListener(){},removeEventListener(){}},
   fetch:(url,options)=>{assert.equal(options,undefined,'only GET is allowed');
     const wait=deferred();calls.push({url,wait});pending.push(wait);return wait.promise;},
   setTimeout:(fn,ms)=>{assert.equal(ms,15000);const id=++timerId;timers.set(id,fn);return id;},
   clearTimeout:id=>timers.delete(id)};
 const patched=source.replace('exports.apply = apply;',
  'exports.createComposerButton=createComposerButton; exports.buildControlPanel=buildControlPanel; exports.apply = apply;');
 assert.notEqual(patched,source);vm.runInNewContext(patched,context);
 const plugin=factory(()=>({}));
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((value,i)=>Object.is(value,b[i]));
 const slot=initial=>{const index=cursor++;if(!(index in current.slots))current.slots[index]=initial;return index;};
 const react={
  createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
  useState:initial=>{const instance=current,index=slot(initial);return[instance.slots[index],value=>{
    if(!instance.mounted)return;const old=instance.slots[index];
    const next=typeof value==='function'?value(old):value;
    if(!Object.is(old,next)){instance.slots[index]=next;dirty=true;}
  }];},
  useRef:value=>{const index=slot({current:value});return current.slots[index];},
  useCallback:(fn,deps)=>{const index=slot(null),old=current.slots[index];
    if(!old||!same(old.deps,deps))current.slots[index]={fn,deps};return current.slots[index].fn;},
  useEffect:(create,deps)=>{const instance=current,index=slot(null),old=instance.slots[index];
    if(!old||!same(old.deps,deps)){instance.slots[index]={deps,cleanup:old&&old.cleanup};
      effects.push(()=>{if(!instance.mounted)return;old&&old.cleanup&&old.cleanup();
        const cleanup=create();instance.slots[index].cleanup=typeof cleanup==='function'?cleanup:null;});}
  }
 };
 const Root=kind==='composer'?plugin.createComposerButton(react,null):plugin.buildControlPanel(react,null);
 const hostProps=()=>kind==='composer'?{...props,useSessions:selector=>selector({byId:{[props.sessionId]:{cwd:sessionCwd}}})}:props;
 const cleanup=instance=>{instance.mounted=false;for(const value of instance.slots){if(value&&typeof value.cleanup==='function')value.cleanup();}};
 function render(){
  dirty=false;effects=[];const visited=new Set();
  function walk(node,path){
   if(!node||typeof node!=='object')return node;
   if(typeof node.type==='function'){
    const key=path+':'+String(node.props.key??'');let instance=instances.get(key);
    if(!instance||instance.type!==node.type){if(instance)cleanup(instance);instance={type:node.type,slots:[],mounted:true};instances.set(key,instance);}
    visited.add(key);const previous=current,oldCursor=cursor;current=instance;cursor=0;
    const value=node.type(node.props);current=previous;cursor=oldCursor;return walk(value,key+'/render');
   }
   return{...node,children:(node.children||[]).map((child,i)=>walk(child,path+'/'+i))};
  }
  rootTree=walk({type:Root,props:hostProps()},'root');
  for(const[key,instance]of instances)if(!visited.has(key)){cleanup(instance);instances.delete(key);}
  prepared=true;return rootTree;
 }
 function commit(){if(!prepared||dirty)render();const tree=rootTree,currentEffects=effects;
  effects=[];prepared=false;for(const effect of currentEffects)effect();return tree;}
 async function flush(){for(let i=0;i<10;i++){await new Promise(resolve=>setImmediate(resolve));if(dirty)commit();}}
 function toolbar(){return nodes(rootTree).find(n=>n.type==='button'&&n.props['aria-expanded']!==undefined);}
 function toggle(){const button=toolbar();assert(button,'composer button must be available');button.props.onClick();commit();}
 function answer(index,body,ok=true){assert(calls[index],'request missing: '+index);calls[index].wait.resolve(response(body,ok));}
 return{calls,timers,commit,flush,toggle,answer,text:()=>text(rootTree),tree:()=>rootTree,
  renderOnly:render,scope:(cwd,sessionId)=>{sessionCwd=cwd;props={...props,sessionId};dirty=true;},
  setProps:value=>{props=value;dirty=true;},toolbar};
}
async function readyA(h){h.commit();assert.equal(h.calls.length,1);h.answer(0,summary('PROJECT_A',A));await h.flush();assert(h.toolbar());}
async function openedAThenClosed(h){await readyA(h);h.toggle();assert.equal(h.calls.length,2);
 h.answer(1,summary('PROJECT_A',A));await h.flush();assert(h.text().includes('PROJECT_A'));h.toggle();}
"""


@unittest.skipUnless(NODE, "Node.js required for shipped composer lifecycle checks")
class DshComposerScopeTests(unittest.TestCase):
    def run_client(self, scenario):
        code = HARNESS + "\n(async()=>{\n" + scenario + "\nconsole.log('ok');\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run([NODE, "-e", code, str(CLIENT)], text=True, encoding="utf-8",
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scope_change_hides_previous_summary_before_effect_and_before_new_response(self):
        self.run_client(r"""
const h=harness();await openedAThenClosed(h);h.scope(B,'chat-b');
assert.equal(h.renderOnly(),null,'old project must disappear synchronously, before passive effects');
h.commit();assert.equal(h.calls.length,3);assert.equal(h.tree(),null);
h.answer(2,summary('PROJECT_B',B));await h.flush();assert(h.toolbar());h.toggle();
assert(h.text().includes('PROJECT_B'));assert(!h.text().includes('PROJECT_A'));
""")

    def test_failed_or_uninstalled_new_project_never_reopens_old_project_controls(self):
        self.run_client(r"""
for(const failed of [false,true]){
 const h=harness();await openedAThenClosed(h);h.scope(B,'chat-b');h.commit();
 h.answer(2,failed?{ok:false,reason:'database_read_unavailable'}:{ok:true,installed:false},!failed);
 await h.flush();assert.equal(h.tree(),null,'B cannot borrow A controls after failed/uninstalled GET');
}
""")

    def test_outer_new_summary_cannot_leave_old_initial_summary_when_inner_read_fails(self):
        self.run_client(r"""
const h=harness();await openedAThenClosed(h);h.scope(B,'chat-b');h.commit();
// Old composer instances still expose A's button while B's outer read waits.
// Exercise that reopen if available, then let the outer B read finish first.
if(h.toolbar())h.toggle();
h.answer(2,summary('PROJECT_B',B));await h.flush();
if(!h.toolbar().props['aria-expanded'])h.toggle();
assert.equal(h.calls.length,4);assert(h.calls[3].url.includes('cwd='+encodeURIComponent(B)));
h.answer(3,{ok:false,reason:'query_budget_exceeded'},false);await h.flush();
assert(h.text().includes('PROJECT_B'));assert(!h.text().includes('PROJECT_A'));
assert(h.text().includes('exceeded its read budget'));
""")

    def test_rapid_a_b_a_switch_does_not_accept_previous_visit_late_reply(self):
        self.run_client(r"""
const h=harness();await readyA(h);h.scope(B,'chat-b');h.commit();
h.scope(A,'chat-a');h.commit();assert.equal(h.calls.length,3);
h.answer(2,summary('PROJECT_A_CURRENT',A));await h.flush();
h.answer(1,summary('PROJECT_B_LATE',B));await h.flush();h.toggle();
assert(h.text().includes('PROJECT_A_CURRENT'));assert(!h.text().includes('PROJECT_B_LATE'));
""")

    def test_same_workspace_different_session_cannot_reuse_previous_session_summary(self):
        self.run_client(r"""
const h=harness();await readyA(h);h.scope(A,'chat-other');assert.equal(h.renderOnly(),null);
h.commit();h.answer(1,summary('PROJECT_A_NEW_SESSION',A));await h.flush();h.toggle();
assert(h.text().includes('PROJECT_A_NEW_SESSION'));assert(!h.text().includes('PROJECT_A '));
""")

    def test_panel_rejects_initial_summary_with_a_different_declared_request_scope(self):
        self.run_client(r"""
const h=harness('panel',{cwd:B,sessionId:'chat-b',initialSummary:summary('PROJECT_A',A),
 initialScope:JSON.stringify([A,'chat-a'])});
h.commit();assert(!h.text().includes('PROJECT_A'),'a newly mounted panel must not label old data as its new scope');
h.answer(0,{ok:false,reason:'database_read_unavailable'},false);await h.flush();
assert(!h.text().includes('PROJECT_A'));
""")


if __name__ == "__main__":
    unittest.main()
