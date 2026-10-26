"""Exported JS -> real isolated Python Hook -> lost ack -> real SQLite receipt."""
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

from agent_learning.install import install
from agent_learning.phase_review import configure
from agent_learning.runtime import Runtime


NODE = shutil.which("node")
PROBE = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {readFileSync} from 'node:fs';
import {dirname,join,resolve} from 'node:path';
const bridge=process.argv[1],workspace=process.argv[2];
const receipt=JSON.parse(readFileSync(join(dirname(dirname(bridge)),'runtime-release.json'),'utf8'));
const actualSpawn=childProcess.spawn, stops=[];
let dropped=false, processes=0;
childProcess.spawn=(command,args,options)=>{
  assert.equal(resolve(command),resolve(receipt.python));
  const script=resolve(args[1]);
  assert.ok([join(receipt.runtime_directory,'hooks','dsh_dispatch.py'),join(receipt.runtime_directory,'hooks','dsh_tool.py')].map(value=>resolve(value)).includes(script),
    'only the isolated fixed runtime hooks may be executed; never model CLIs');
  assert.equal(options.env.ALP_HOME,process.env.ALP_HOME);
  assert.equal(options.env.ALP_CONFIG,process.env.ALP_CONFIG);
  processes++;
  const real=actualSpawn(command,args,options),proxy=new EventEmitter();
  proxy.stdout=new EventEmitter();proxy.stderr=new EventEmitter();proxy.stdin=real.stdin;proxy.kill=()=>real.kill();
  let stdout='';real.stdout.on('data',chunk=>{stdout+=chunk.toString('utf8');});
  real.stderr.on('data',chunk=>proxy.stderr.emit('data',chunk));real.on('error',e=>proxy.emit('error',e));
  real.on('close',code=>{
    const ix=args.indexOf('--event'),kind=ix<0?null:args[ix+1];
    if(kind==='Stop'&&code===0){
      const data=JSON.parse(stdout.trim());
      assert.match(data.dsh_capture_boundary,/^[a-f0-9]{64}$/);
      assert.match(data.dsh_capture_ack,/^[a-f0-9-]{36}$/);
      stops.push(data);
      if(!dropped){dropped=true;proxy.emit('close',1);return;}
    }
    proxy.stdout.emit('data',Buffer.from(stdout));proxy.emit('close',code);
  });
  return proxy;
};
syncBuiltinESMExports();
const handlers=new Map(),tools=new Map();
const {apply}=await import(pathToFileURL(bridge).href);
apply({tools:{register(s){tools.set(s.name,s);}},effect(){},inject(){},on(name,cb){handlers.set(name,cb);},systemPrompt:{context(){}}});
const owner={id:'native-e2e',header:{cwd:workspace}};
handlers.get('session/created')(owner);
await handlers.get('agent/created')({agent:{id:owner.id}});
async function step(turn){return handlers.get('agent/pre-step')({agent:{id:owner.id},turn,step:1,signal:new AbortController().signal},async()=>({kind:'enter',messages:[
  {id:'request-'+turn,role:'user',source:{kind:'user'},content:[{type:'text',text:'Verify capture receipt integration '+turn}]}]}));}
await step(1);
async function call(name,args,id){
  const exec={agent:{id:owner.id},name,arguments:args,callId:id,signal:new AbortController().signal};
  const allowed=await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));assert.notEqual(allowed?.kind,'deny');
  const result=await tools.get(name).execute(args,exec);
  await handlers.get('tools/post-execute')(exec,{isError:false,output:JSON.stringify(result)},async()=>({kind:'accept'}));
  return result;
}
const current=await call('session_context',{},'context');
assert.equal(current.context_bound,true);
const task=current.task;
await call('task_update',{goal:task.goal,status:'delivered',expected_task_id:task.id,expected_write_version:task.write_version},'delivery');
await handlers.get('session/event')(owner,{type:'assistant/message',data:{turn:1,step:1,message:{role:'assistant',source:{kind:'model'},content:[
  {type:'reasoning',text:'PRIVATE_THINKING_MUST_NOT_PERSIST'},
  {type:'text',text:'Implemented the synthetic fixture. Awaiting user acceptance.'}]}}});
const lost=await handlers.get('session/event')(owner,{type:'turn/end',data:{turn:1,reason:{kind:'completed'}}});
assert.equal(lost.dsh_event_unconfirmed,true);
assert.equal(stops.length,1);
await step(2); // Flush the original frozen event before the next user boundary.
assert.equal(stops.length,2);
assert.deepEqual(stops[1],stops[0],'the actual Python receiver returns the original complete receipt');
console.log(JSON.stringify({ok:true,python_processes:processes,stop_attempts:stops.length,ack_equal:true}));
"""


@unittest.skipUnless(NODE, "Node.js is required for the exported bridge integration")
class DshCaptureE2ETests(unittest.TestCase):
    def test_real_fixed_runtime_ack_loss_replays_once_without_duplicate_stage(self):
        # The acceptance runner sets ALP_TEST_ROOT. CI uses its isolated temp
        # directory, keeping exports outside the source without bypassing the
        # installer's overlap guard.
        parent = Path(os.environ.get("ALP_TEST_ROOT", tempfile.gettempdir()))
        local = parent / "dsh-e2e"
        local.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="native-", dir=local) as directory:
            root = Path(directory)
            for name in ("workspace", "home", "tmp", "codex", "runtime-releases", "exports"):
                (root / name).mkdir()
            workspace, home = root / "workspace", root / "home"
            config = {"admin": {"enabled": False}, "scheduler": {"enabled": False},
                      "software_updates": {"enabled": False}, "hindsight": {"enabled": False}}
            config_path = home / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = Runtime(home, config=config, host="dsh")
            try:
                project = runtime.project(workspace)
                conversation = runtime.host_conversation("native-e2e")
                configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
                runtime.task_update(project, conversation, "Verify capture receipt integration", new_task=True)
            finally:
                runtime.close()
            target = root / "exports" / "dsh"
            install(target, runtime_store=root / "runtime-releases", host="dsh",
                    workspace=workspace, home=home, hook_mode="observe")
            env = {key: value for key, value in os.environ.items() if not key.startswith("ALP_")}
            env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       ALP_HOME=str(home), ALP_CONFIG=str(config_path), ALP_RUNTIME_STORE=str(root / "runtime-releases"),
                       CODEX_HOME=str(root / "codex"), TMP=str(root / "tmp"), TEMP=str(root / "tmp"),
                       TMPDIR=str(root / "tmp"), DSH_HOME=str(root / "dsh-profile"))
            result = subprocess.run([NODE, "--input-type=module", "-e", PROBE,
                str(target / "host/index.mjs"), str(workspace)], cwd=workspace, env=env,
                capture_output=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            observed = json.loads(result.stdout.decode("utf-8"))
            self.assertTrue(observed["ok"])
            self.assertEqual(observed["stop_attempts"], 2)
            with closing(sqlite3.connect(home / "runtime.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE type='Stop'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM dsh_receipts WHERE kind='Stop' AND state='complete'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)
                self.assertFalse(db.execute("SELECT 1 FROM operations WHERE conflict<>0").fetchone())
                self.assertFalse(db.execute("SELECT 1 FROM events WHERE payload LIKE '%PRIVATE_THINKING_MUST_NOT_PERSIST%'").fetchone())


if __name__ == "__main__":
    unittest.main()
