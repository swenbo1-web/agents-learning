"""Exercise the shipped DSH status UI and route with no host or model calls."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "host_resources" / "dsh" / "client" / "index.js"


HARNESS = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(process.argv[1],'utf8');
const immediate=()=>new Promise(resolve=>setImmediate(resolve));
const flush=async()=>{for(let i=0;i<6;i++)await immediate();};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const response=(body,ok=true)=>({ok,status:ok?200:503,json:async()=>body});
const summary=name=>({ok:true,installed:true,project_id:name,project_name:name,workspace:name,
 project_usage:{scope:'project',project_id:name,max_calls_per_day:15},
 panel_snapshot:{installation_id:'fixture-install',expected:{}}});
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
const text=n=>!n?'':typeof n!=='object'?String(n):(n.children||[]).map(text).join(' ');
function harness(language='en'){
 let factory,cursor=0,timerId=0,props={initialSummary:summary('confirmed'),initialScope:JSON.stringify(['scope-a','chat-a']),cwd:'scope-a',sessionId:'chat-a'};
 const states=[],pending=[],calls=[],timers=new Map();
 const context={window:{__ModuleLoader__:{load:item=>{factory=item.factory;}}},navigator:{language},
   fetch:(url,options)=>{calls.push({url,options});assert.equal(options,undefined,'status must stay GET');
     assert(pending.length,'the test must prepare a status response');return pending.shift()(url);},
   setTimeout:(fn,ms)=>{assert.equal(ms,15000);const id=++timerId;timers.set(id,fn);return id;},
   clearTimeout:id=>timers.delete(id)};
 const patched=source.replace('exports.apply = apply;', 'exports.buildControlPanel=buildControlPanel; exports.apply = apply;');
 assert.notEqual(patched,source,'the real resource export point must exist');
 vm.runInNewContext(patched,context);
 const plugin=factory(()=>({}));
 const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),
   useState:initial=>{const n=cursor++;if(!(n in states))states[n]=initial;
     return[states[n],v=>{states[n]=typeof v==='function'?v(states[n]):v;}];},
   useCallback:fn=>fn,useEffect(){}};
 const Panel=plugin.buildControlPanel(react,null);
 const render=(next)=>{if(next)props=next;cursor=0;return Panel(props);};
 const refresh=()=>{const tree=render(),button=nodes(tree).find(n=>n.type==='button'&&
   (n.children.includes('Refresh')||n.children.includes('刷新')));assert(button);return button.props.onClick();};
 return{states,pending,calls,timers,render,refresh,
   expire:()=>{assert.equal(timers.size,1);const fn=[...timers.values()][0];fn();}};
}
"""


@unittest.skipUnless(NODE, "Node.js required for the actual DSH UI resource")
class DshPanelStatusUiTests(unittest.TestCase):
    def run_client(self, scenario):
        code = HARNESS + "\n(async()=>{\n" + scenario + "\nconsole.log('ok');\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run([NODE, "-e", code, str(CLIENT)], text=True, encoding="utf-8",
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_query_budget_error_is_localized_retains_confirmed_status_and_refresh_recovers(self):
        self.run_client(r"""
for(const [language,fragment]of[['en','exceeded its read budget'],['zh-CN','本地查询预算']]){
 const h=harness(language);h.render();const confirmed=h.states[0];
 h.pending.push(async()=>response({ok:false,reason:'query_budget_exceeded',error:'PRIVATE C:\\secret\\database.db'},false));
 await h.refresh();await flush();
 assert.equal(h.states[0],confirmed,'a failed read must retain the last confirmed status');
 assert(h.states[1].includes(fragment),h.states[1]);
 assert(!text(h.render()).includes('PRIVATE'));assert(!h.states[1].includes('database.db'));
 assert.equal(h.timers.size,0);
 h.pending.push(async()=>response(summary('recovered')));await h.refresh();await flush();
 assert.equal(h.states[0].project_id,'recovered');assert.equal(h.states[1],'');
 assert(text(h.render()).includes('recovered'));
}
""")

    def test_unknown_status_reason_or_network_error_never_exposes_raw_error(self):
        self.run_client(r"""
for(const language of ['en','zh-CN'])for(const mode of ['unknown','network']){
 const h=harness(language);h.render();
 h.pending.push(async()=>{
  if(mode==='network')throw new Error('PRIVATE C:\\secret\\credentials.json');
  return response({ok:false,reason:'PRIVATE C:\\secret',error:'PRIVATE C:\\secret\\credentials.json'},false);
 });
 await h.refresh();await flush();
 assert(h.states[1].includes(language==='en'?'temporarily unavailable':'暂时无法读取'),h.states[1]);
 assert(!text(h.render()).includes('PRIVATE'));assert.equal(h.states[0].project_id,'confirmed');
}
""")

    def test_status_deadline_includes_json_body_and_late_completion_cannot_overwrite(self):
        self.run_client(r"""
for(const language of ['en','zh-CN']){
 const h=harness(language),body=deferred();h.render();
 h.pending.push(async()=>({ok:true,status:200,json:()=>body.promise}));
 const read=h.refresh();await flush();assert.equal(h.timers.size,1);
 h.expire();await read;await flush();
 assert(h.states[1].includes(language==='en'?'timed out':'超时'),h.states[1]);
 assert.equal(h.states[0].project_id,'confirmed');assert.equal(h.timers.size,0);
 h.pending.push(async()=>response(summary('after-timeout')));await h.refresh();await flush();
 assert.equal(h.states[0].project_id,'after-timeout');assert.equal(h.states[1],'');
 body.resolve(summary('late-old-body'));await flush();
 assert.equal(h.states[0].project_id,'after-timeout');assert.equal(h.states[1],'');
}
""")

    def test_out_of_order_reads_only_accept_the_latest_request(self):
        self.run_client(r"""
for(const staleFails of [false,true]){
 const h=harness(),first=deferred(),second=deferred();h.render();
 h.pending.push(()=>first.promise,()=>second.promise);
 const oldRead=h.refresh(),newRead=h.refresh();
 second.resolve(response(summary('latest-request')));await newRead;await flush();
 assert.equal(h.states[0].project_id,'latest-request');
 first.resolve(staleFails?response({ok:false,reason:'query_budget_exceeded'},false):response(summary('stale-request')));
 await oldRead;await flush();
 assert.equal(h.states[0].project_id,'latest-request');assert.equal(h.states[1],'');assert.equal(h.timers.size,0);
}
""")

    def test_previous_workspace_return_cannot_populate_the_new_scope(self):
        self.run_client(r"""
for(const staleFails of [false,true]){
 const h=harness(),first=deferred();h.render();h.pending.push(()=>first.promise);
 const oldRead=h.refresh();
 let tree=h.render({initialSummary:summary('new-initial'),cwd:'scope-b',sessionId:'chat-b'});
 assert(!text(tree).includes('confirmed'),'previous project controls must be hidden after scope change');
 h.pending.push(async()=>response(summary('current-project')));await h.refresh();await flush();
 assert(h.calls.at(-1).url.includes('cwd=scope-b'));assert(h.calls.at(-1).url.includes('sessionId=chat-b'));
 first.resolve(staleFails?response({ok:false,reason:'query_budget_exceeded'},false):response(summary('old-scope-result')));
 await oldRead;await flush();
 assert.equal(h.states[0].project_id,'current-project');assert.equal(h.states[1],'');
 tree=h.render();assert(text(tree).includes('current-project'));assert(!text(tree).includes('old-scope-result'));
 assert.equal(h.timers.size,0);
}
""")

    def test_actual_host_route_forwards_only_fixed_status_failure_reasons(self):
        with tempfile.TemporaryDirectory(prefix="dsh-status-route-") as temporary:
            root = Path(temporary).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            plugin = root / "bundle"
            shutil.copytree(ROOT / "host_resources" / "dsh" / "host", plugin / "host")
            (plugin / "runtime-release.json").write_text(json.dumps({"workspace": str(workspace),
                "runtime_directory": str(root / "runtime"), "home": str(root / "home"),
                "python": sys.executable, "release_id": "isolated-status-ui"}), encoding="utf-8")
            code = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
const [modulePath,workspace]=process.argv.slice(1),calls=[];let current;
childProcess.spawn=(_executable,args)=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();let input='';
 child.stdin={write:value=>{input+=value;},end:()=>queueMicrotask(()=>{
  const payload=JSON.parse(input);calls.push(payload);assert.equal(payload.panel_status,true);
  assert(!payload.admin_preset);assert(!args.includes('admin-drain'));
  child.stdout.emit('data',Buffer.from(current));child.emit('close',0);
 })};child.kill=()=>{};return child;
};syncBuiltinESMExports();
let route;const scope={skills:{register(){}},commands:{register(){}},effect:fn=>fn(),
 webServer:{register:value=>{if(value.path==='/agent-learning/panel-v2')route=value;}}};
const {apply}=await import(pathToFileURL(modulePath).href);
apply({tools:{register(){}},on(){},effect(){},inject(_names,callback){callback(scope);}});assert(route);
const request=async(body)=>{
 let status,value;const req={method:body?'POST':'GET',headers:{},url:'/agent-learning/panel-v2?cwd='+encodeURIComponent(workspace),
  async *[Symbol.asyncIterator](){if(body)yield Buffer.from(JSON.stringify(body));}};
 const res={writeHead:code=>{status=code;return res;},end:raw=>{value=JSON.parse(raw);}};
 await route.handler(req,res);return{status,value};
};
try{
 for(const reason of ['query_budget_exceeded','database_read_unavailable','host_scope_uninitialized','PRIVATE /secret/path']){
  current=JSON.stringify({ok:false,reason,error:'PRIVATE C:\\secret\\credentials.json'});
  const {status,value}=await request();assert.equal(status,503);
  assert.deepEqual(value,{ok:false,error:'project status unavailable',
   reason:reason.startsWith('PRIVATE')?'database_read_unavailable':reason});
  assert(!JSON.stringify(value).includes('PRIVATE'));
 }
 current='PRIVATE invalid JSON /secret/path';const failed=await request();
 assert.equal(failed.status,503);assert.equal(failed.value.reason,'database_read_unavailable');
 assert(!JSON.stringify(failed.value).includes('PRIVATE'));assert.equal(calls.length,5);
 current=JSON.stringify({ok:true,result:{data:{project_id:'project-a',panel_snapshot:{project_id:'project-a'}}}});
 const initial=await request();assert.equal(initial.status,200);assert(initial.value.installed);
 const body={action:'set_admin_preset',cwd:workspace,project_id:'project-a',
  installation_id:initial.value.panel_snapshot.installation_id,preset:'light',
  panel_snapshot:initial.value.panel_snapshot,request_id:'no-write-on-unregistered',confirm:true};
 for(const result of [{registered:false},{},{data:{project_id:'other-project'}}]){
  current=JSON.stringify({ok:true,result});const before=calls.length;
  const rejected=await request(body);assert.equal(rejected.status,409);
  assert.equal(calls.length,before+1,'only read preflight may run; no settings write');
 }
}finally{globalThis.__ALP_DSH_DISPOSE__?.();}
console.log('ok');
"""
            result = subprocess.run([NODE, "--input-type=module", "-e", code,
                                     str(plugin / "host" / "index.mjs"), str(workspace)],
                                    text=True, encoding="utf-8", capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
