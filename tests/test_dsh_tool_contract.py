"""Exercise DSH worker tools through their real entrypoints and operation records."""
import json
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCAL = Path(os.environ.get("ALP_TEST_ROOT", ROOT / "build/local/dsh1411-tests"))
NODE = shutil.which("node")


REAL_BRIDGE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const handlers = new Map(), definitions = new Map();
const {apply} = await import(pathToFileURL(process.argv[1]).href);
apply({tools:{register(spec){definitions.set(spec.name,spec);}},effect(){},inject(){},
  on(name,handler){handlers.set(name,handler);},systemPrompt:{context(){}}});
const agent = {id:'worker-contract'};
handlers.get('session/created')({id:agent.id,header:{cwd:process.argv[2]}});
await handlers.get('agent/created')({agent});
const errors = [];
const cases = [['learning_status',{view:'summary'}],['session_context',{}],
  ['knowledge_search',{query:'synthetic tool contract'}],
  ['task_update',{goal:'Verify synthetic worker tools',new_task:true}]];
for (const [name,args] of cases) {
  const exec = {agent,name,arguments:args,callId:'contract-'+name};
  let result;
  try {
    const decision = await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));
    assert.notEqual(decision?.kind,'deny',JSON.stringify(decision));
    const value = await definitions.get(name).execute(args,exec);
    if(name==='learning_status'){
      assert.equal(value.view,'summary');
      assert.ok(JSON.stringify(value).length<=3000);
      assert.equal(Object.hasOwn(value,'history_import'),false);
    }
    result = {isError:false,content:[{type:'text',text:JSON.stringify(value)}]};
  } catch(error) {
    errors.push({name,error:String(error)});
    result = {isError:true,error:{message:String(error)}};
  }
  await handlers.get('tools/post-execute')(exec,result,async()=>({kind:'accept'}));
}
// Some host versions route a native tool through a Python expression first.
// A recovered syntax failure must still close the original operation.
const compat={agent,name:'session_context',arguments:{},callId:'syntax-recovery'};
const gate=await handlers.get('tools/pre-execute')(compat,async()=>({kind:'allow'}));
assert.notEqual(gate?.kind,'deny',JSON.stringify(gate));
const recovered=await handlers.get('tools/post-execute')(compat,
  {isError:true,error:{message:'SyntaxError: invalid syntax'}},async()=>({kind:'passthrough'}));
assert.equal(recovered.kind,'accept');
globalThis.__ALP_DSH_DISPOSE__?.();
console.log(JSON.stringify({errors}));
"""


ROTATION_BRIDGE = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
const calls = [], handlers = new Map(), definitions = new Map();
const oldHandle = 'a'.repeat(32), newHandle = 'b'.repeat(32);
const scenario = process.argv[3] || 'rotation';
let failTool = scenario === 'recovery-failure';
childProcess.spawn = (_python,argv) => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  let input = '';
  child.stdin = {write(value){input+=value;},end(){queueMicrotask(()=>{
    const payload = JSON.parse(input), i = argv.indexOf('--event');
    const event = i >= 0 ? argv[i+1] : 'tool';
    calls.push({event,payload});
    const result = event === 'tool' ? (failTool
      ? {ok:false,error:'synthetic recovery rejected'} : {ok:true,result:{bound:payload.args}})
      : {dsh_trajectory:{generation:1,token:'1'.repeat(32)},
        hookSpecificOutput:{additionalContext:'context_handle='+
          (event === 'PreToolUse' ? newHandle : oldHandle)+'; context'}};
    if (scenario === 'pre-without-handle' && event === 'SessionStart') {
      delete result.hookSpecificOutput;
    }
    child.stdout.emit('data',Buffer.from(JSON.stringify(result)));
    child.emit('close',0);
  });}};
  child.kill=()=>{};
  return child;
};
syncBuiltinESMExports();
const {apply} = await import(pathToFileURL(process.argv[1]).href);
apply({tools:{register(spec){definitions.set(spec.name,spec);}},effect(){},inject(){},
  on(name,handler){handlers.set(name,handler);},systemPrompt:{context(){}}});
const agent={id:'worker-contract'};
handlers.get('session/created')({id:agent.id,header:{cwd:process.argv[2]}});
await handlers.get('agent/created')({agent});
const rereading=scenario==='reread';
if(rereading){assert.equal(definitions.get('work_memory_read').parameters.properties.reread.type,'boolean');}
const args=rereading?{memory_id:'synthetic-memory',reread:true}:{query:'verify immutable effective arguments'};
const exec={agent,name:rereading?'work_memory_read':'knowledge_search',arguments:args,callId:'same-call'};
await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));
if (scenario === 'recovery-failure') {
  await assert.rejects(handlers.get('tools/post-execute')(exec,
    {isError:true,error:{message:'SyntaxError: invalid syntax'}},async()=>({kind:'passthrough'})),
    /synthetic recovery rejected/);
  const failedPost = calls.find(x=>x.event==='PostToolUse');
  assert.ok(failedPost,'failed compatibility recovery must complete the original operation');
  assert.equal(failedPost.payload.result.isError,true);
  assert.match(failedPost.payload.result.error.message,/synthetic recovery rejected/);
  assert.equal(failedPost.payload.arguments.context_handle,oldHandle);
  failTool=false;
  await definitions.get(exec.name).execute(args,exec);
  assert.equal(calls.at(-1).payload.args.context_handle,newHandle,
    'failed recovery must release its old per-call binding');
} else {
await definitions.get(exec.name).execute(args,exec);
await handlers.get('tools/post-execute')(exec,{isError:false},async()=>({kind:'accept'}));
const pre=calls.find(x=>x.event==='PreToolUse');
const executed=calls.find(x=>x.event==='tool');
const post=calls.find(x=>x.event==='PostToolUse');
if (scenario === 'pre-without-handle') {
  assert.equal(Object.hasOwn(pre.payload.arguments,'context_handle'),false);
  assert.equal(Object.hasOwn(executed.payload.args,'context_handle'),false,
    'the Python native session fallback must handle missing context unchanged');
} else {
  assert.equal(pre.payload.arguments.context_handle,oldHandle);
}
assert.deepEqual(executed.payload.args,pre.payload.arguments,
  'execution must use the arguments checked before the handle changed');
assert.deepEqual(post.payload.arguments,pre.payload.arguments,
  'completion must retain the actual checked arguments');
if(rereading){
 assert.equal(executed.payload.args.reread,true);
 await definitions.get('work_memory_read').execute({memory_id:'synthetic-memory',reread:false},
   {agent,name:'work_memory_read',callId:'reread-false'});
 assert.equal(calls.at(-1).payload.args.reread,false);
}
await definitions.get('learning_status').execute({}, {agent,name:'learning_status',callId:'status'});
assert.deepEqual(calls.at(-1).payload.args,{},'status has no context_handle property');
assert.deepEqual(definitions.get('learning_status').parameters.properties.view.enum,['summary','full']);
await definitions.get('learning_status').execute({view:'summary'}, {agent,name:'learning_status',callId:'status-summary'});
assert.deepEqual(calls.at(-1).payload.args,{view:'summary'},'status view must reach the Python dispatcher unchanged');
for (const handle of ['', 'invalid-explicit-handle']) {
  await definitions.get('session_context').execute({context_handle:handle},
    {agent,name:'session_context',callId:'explicit-'+handle});
  assert.equal(calls.at(-1).payload.args.context_handle,handle,
    'explicit bad handles must reach schema and identity validation unchanged');
}
}
globalThis.__ALP_DSH_DISPOSE__?.();
console.log(JSON.stringify({ok:true}));
"""


SCOPE_BRIDGE = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [modulePath,workspace]=process.argv.slice(1),calls=[],browsers=[],handlers=new Map(),definitions=new Map();
const receiptPath=join(dirname(dirname(modulePath)),'runtime-release.json');
const fixed=JSON.parse(readFileSync(receiptPath,'utf8'));
const sub=join(workspace,'src'),outside=join(dirname(workspace),'outside'),nested=join(workspace,'nested');
for(const path of [sub,outside,join(nested,'.git')])mkdirSync(path,{recursive:true});
childProcess.spawn=(_python,argv)=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdout.setEncoding=()=>{};
 child.exitCode=null;child.killed=false;child.pid=200+calls.length;let input='';
 child.kill=()=>{child.killed=true;queueMicrotask(()=>child.emit('close',0));};
 child.stdin={write:value=>input+=value,end:()=>queueMicrotask(()=>{
  const payload=input?JSON.parse(input):{},index=argv.indexOf('--event');calls.push({argv,payload});
  const result=index>=0?{dsh_trajectory:{generation:1,token:'1'.repeat(32)},
   hookSpecificOutput:{additionalContext:'context_handle='+'a'.repeat(32)+'; scope'}}:
   payload.tool?{ok:true,result:{workspace:payload.workspace,args:payload.args}}:{};
  child.stdout.emit('data',Buffer.from(JSON.stringify(result)));child.exitCode=0;child.emit('close',0);
 })};
 if(argv.includes('browser')){
  browsers.push({child,argv});queueMicrotask(()=>{
   const url='http://127.0.0.1:'+(13000+browsers.length)+'/';
   child.stdout.emit('data',JSON.stringify({project:'synthetic',url,access_token:'read',read_only:true})+'\n');
  });
 }
 return child;
};syncBuiltinESMExports();
const {apply}=await import(pathToFileURL(modulePath).href);
apply({tools:{register:spec=>definitions.set(spec.name,spec)},effect(){},inject(){},
 on:(name,handler)=>handlers.set(name,handler)});
async function session(id,cwd){const agent={id};handlers.get('session/created')({id,header:{cwd}});await handlers.get('agent/created')({agent});return agent;}
let sequence=0;
async function execute(agent,name,args){
 const exec={agent,name,arguments:args,callId:'scope-'+(++sequence)};
 const gate=await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));assert.equal(gate.kind,'allow',JSON.stringify(gate));
 return definitions.get(name).execute(args,exec);
}
async function denied(agent){
 for(const [name,args] of [['learning_status',{}],['knowledge_search',{query:'scope'}],['learning_browser',{action:'start',open:false}]]){
  const before=calls.length;
  const exec={agent,name,arguments:args,callId:'denied-'+(++sequence)};
  const gate=await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));
  assert.equal(gate.kind,'deny');assert.match(gate.reason,/trusted session.*installed workspace/);
  await assert.rejects(definitions.get(name).execute(args,exec),/trusted session.*installed workspace/);
  assert.equal(calls.length,before,'Rejected native calls must not start Python or a browser');
 }
}
try{
 const rootAgent=await session('root',workspace),subAgent=await session('sub',sub),outsideAgent=await session('outside',outside),nestedAgent=await session('nested',nested);
 assert.equal((await execute(rootAgent,'knowledge_search',{query:'root'})).workspace,workspace);
 assert.equal((await execute(subAgent,'knowledge_search',{query:'sub'})).workspace,workspace);
 await execute(rootAgent,'learning_browser',{action:'start',open:false});await execute(subAgent,'learning_browser',{action:'start',open:false});
 assert.equal(browsers.length,1);assert.equal(browsers[0].argv[browsers[0].argv.indexOf('--workspace')+1],workspace);
 for(const mode of ['observe','enforce']){
  writeFileSync(receiptPath,JSON.stringify({...fixed,hook_mode:mode}));
  for(const agent of [outsideAgent,nestedAgent,{id:'unknown'},undefined])await denied(agent);
  const before=calls.length;
  const ordinary=await handlers.get('tools/pre-execute')({agent:outsideAgent,name:'shell',arguments:{command:'synthetic'},callId:'ordinary-'+mode},async()=>({kind:'allow'}));
  assert.equal(ordinary.kind,'allow');assert.equal(calls.length,before);
 }
 handlers.get('session/created')({id:rootAgent.id,header:{cwd:outside}});await denied(rootAgent);
 writeFileSync(receiptPath,JSON.stringify({...fixed,workspaces:[workspace,nested]}));
 assert.equal((await execute(nestedAgent,'learning_status',{})).workspace,nested,'An explicitly bound nested project remains usable');
 writeFileSync(receiptPath,JSON.stringify({...fixed,workspace:nested,workspaces:[nested]}));await denied(subAgent);
 const dynamic={...fixed};delete dynamic.workspace;delete dynamic.workspaces;writeFileSync(receiptPath,JSON.stringify(dynamic));
 assert.equal((await execute(outsideAgent,'learning_status',{})).workspace,outside,'Unbound exports retain their dynamic workspace contract');
 assert.equal((await execute(nestedAgent,'learning_status',{})).workspace,nested);
 console.log(JSON.stringify({ok:true}));
}finally{globalThis.__ALP_DSH_DISPOSE__?.();}
"""


class DshToolContractTests(unittest.TestCase):
    def setUp(self):
        LOCAL.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="worker-", dir=LOCAL)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.home = self.root / "home"
        for name in ("project", "home", "tmp", "codex", "runtime-releases"):
            (self.root / name).mkdir()
        self.config = self.home / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(ROOT), "ALP_HOME": str(self.home),
                    "ALP_CONFIG": str(self.config), "CODEX_HOME": str(self.root / "codex"),
                    "ALP_RUNTIME_STORE": str(self.root / "runtime-releases")}
        self.env.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})

    def tool(self, name, args):
        payload = {"workspace": str(self.workspace), "home": str(self.home),
                   "session_id": "worker-contract", "tool": name, "args": args}
        result = subprocess.run([sys.executable, "-B", str(ROOT / "hooks/dsh_tool.py")],
                                input=json.dumps(payload), text=True, encoding="utf-8",
                                capture_output=True, cwd=self.workspace, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_python_entrypoint_obeys_each_tools_schema(self):
        cases = (("learning_status", {}), ("session_context", {}),
                 ("knowledge_search", {"query": "synthetic contract"}),
                 ("task_update", {"goal": "Synthetic contract task", "new_task": True}))
        for name, args in cases:
            with self.subTest(tool=name):
                result = self.tool(name, args)
                self.assertTrue(result["ok"], result)

    def test_python_entrypoint_preserves_explicit_invalid_args_for_rejection(self):
        for name, args in (("learning_status", {"unexpected": "keep validation"}),
                           ("learning_status", {"view": "unknown"}),
                           ("learning_status", {"view": True}),
                           ("session_context", {"context_handle": ""}),
                           ("session_context", {"context_handle": "invalid-explicit-handle"})):
            with self.subTest(tool=name, args=args):
                result = self.tool(name, args)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result.get("error"))

    def test_python_status_summary_is_explicit_and_default_status_remains_full(self):
        full = self.tool("learning_status", {})["result"]
        explicit = self.tool("learning_status", {"view": "full"})["result"]
        summary = self.tool("learning_status", {"view": "summary"})["result"]
        self.assertIn("history_import", full)
        self.assertIn("history_import", explicit)
        self.assertNotIn("view", full)
        self.assertEqual(summary["view"], "summary")
        self.assertEqual(summary["project_id"], full["project_id"])
        self.assertEqual(summary["home"], full["home"])
        self.assertFalse(summary["admin_admission_enabled"])
        self.assertNotIn("history_import", summary)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False, separators=(",", ":"))), 3000)

    def test_python_work_read_accepts_boolean_reread_and_rejects_other_types(self):
        from agent_learning.runtime import Runtime
        runtime = Runtime(self.home, config={}, host="dsh")
        try:
            project = runtime.project(self.workspace)
            payload = {"last_assistant_message": "Synthetic public observation for reread."}
            with runtime.transaction():
                event, _ = runtime._event(project, "source", "Stop", payload, outcome="reported")
                memory = runtime._work_capture_event(project, event["id"], self.workspace, raw=payload)
        finally:
            runtime.close()
        for value in (True, False):
            result = self.tool("work_memory_read", {"memory_id": memory["memory_id"], "reread": value})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["content"], payload["last_assistant_message"])
        for value in ("true", 1, [], None):
            result = self.tool("work_memory_read", {"memory_id": memory["memory_id"], "reread": value})
            self.assertFalse(result["ok"], result)
            self.assertIn("boolean", result["error"])

    def bridge(self, probe, scenario="rotation"):
        plugin = self.root / "plugin"
        shutil.copytree(ROOT / "host_resources/dsh/host", plugin / "host")
        (plugin / "runtime-release.json").write_text(json.dumps({
            "python": sys.executable, "runtime_directory": str(ROOT),
            "workspace": str(self.workspace), "home": str(self.home), "hook_mode": "observe",
        }), encoding="utf-8")
        result = subprocess.run([NODE, "--input-type=module", "-e", probe,
                                 str(plugin / "host/index.mjs"), str(self.workspace), scenario],
                                cwd=self.workspace, env=self.env, capture_output=True,
                                text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(NODE, "Node.js is required for the real DSH worker bridge")
    def test_real_worker_pre_execute_post_chain_has_no_input_conflict(self):
        result = self.bridge(REAL_BRIDGE)
        self.assertEqual(result["errors"], [])
        with closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db:
            rows = db.execute("SELECT conflict, observation FROM operations ORDER BY rowid").fetchall()
            self.assertEqual(len(rows), 5)
            # A completed knowledge tool is not independent proof of task success.
            self.assertEqual(rows, [(0, "call_completed")] * 5)
            self.assertEqual(db.execute("SELECT count(*) FROM admin_calls").fetchone()[0], 0)

    @unittest.skipUnless(NODE, "Node.js is required for DSH worker argument pinning")
    def test_bridge_pins_effective_args_and_preserves_explicit_handles(self):
        self.assertTrue(self.bridge(ROTATION_BRIDGE)["ok"])

    @unittest.skipUnless(NODE, "Node.js is required for DSH worker argument pinning")
    def test_bridge_does_not_add_a_handle_after_pre_approved_without_one(self):
        self.assertTrue(self.bridge(ROTATION_BRIDGE, "pre-without-handle")["ok"])

    @unittest.skipUnless(NODE, "Node.js is required for DSH compatibility recovery")
    def test_failed_syntax_recovery_records_completion_and_releases_binding(self):
        self.assertTrue(self.bridge(ROTATION_BRIDGE, "recovery-failure")["ok"])

    @unittest.skipUnless(NODE, "Node.js is required for DSH reread argument pinning")
    def test_bridge_declares_and_preserves_reread_through_pre_execute_and_post(self):
        self.assertTrue(self.bridge(ROTATION_BRIDGE, "reread")["ok"])

    @unittest.skipUnless(NODE, "Node.js is required for DSH native scope checks")
    def test_registered_native_tools_enforce_live_session_binding_without_blocking_other_tools(self):
        self.assertTrue(self.bridge(SCOPE_BRIDGE)["ok"])


if __name__ == "__main__":
    unittest.main()
