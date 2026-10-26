"""SDK-shaped lost capture delivery, with every subprocess simulated."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


NODE = shutil.which("node")
BRIDGE = Path(__file__).resolve().parents[1] / "host_resources/dsh/host/index.mjs"
PROBE = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {readFileSync,writeFileSync} from 'node:fs';
import {dirname,join} from 'node:path';
const scenario=process.argv[2], workspace=process.argv[3];
const calls=[], receipts=new Map(), firstBodies=new Map(), handlers=new Map(), routes=new Map();
let version=1, failed=false, writes=0, clock=Date.now();
const proof=()=>version.toString(16).padStart(64,'0');
const originalNow=Date.now;
Date.now=()=>clock;
childProcess.spawn=(_command,argv,options)=>{
  const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();
  let input='';child.stdin=Object.assign(new EventEmitter(),{write(s){input+=s;},end(){queueMicrotask(()=>{
    const data=JSON.parse(input), ix=argv.indexOf('--event');
    if(ix<0){
      const result=data.panel_status?{ok:true,result:{data:{project_id:'synthetic',counts:{},work_memory:{}}}}:{running:false};
      child.stdout.emit('data',Buffer.from(JSON.stringify(result)));child.emit('close',0);return;
    }
    const event=argv[ix+1];calls.push({event,data,cwd:options.cwd});
    if(scenario==='deadline'&&data.dsh_capture_replay)return; // Even kill() never emits close.
    let result={dsh_trajectory:{generation:1,token:'1'.repeat(32)}};
    if(scenario!=='legacy')Object.assign(result,{dsh_capture_boundary:proof(),dsh_capture_write_version:version});
    if(event==='SessionStart'||event==='UserPromptSubmit')result.hookSpecificOutput={additionalContext:'context_handle=synthetic; matched'};
    if(data.dsh_capture_id){
      const normalized={...data};delete normalized.dsh_capture_replay;
      const body=JSON.stringify(normalized),saved=firstBodies.get(data.dsh_capture_id);
      if(saved)assert.equal(body,saved,'replay must keep original source and request metadata');
      else firstBodies.set(data.dsh_capture_id,body);
      const old=receipts.get(data.dsh_capture_id);
      const target=event==='Stop'||event==='PostToolUse'||event==='Interrupt';
      const shouldFail=target&&(scenario==='always-fail'||scenario==='capacity'||scenario==='oversized'||scenario==='ttl'||scenario==='dispose'||scenario==='installation'||scenario==='one-at-a-time'||scenario==='legacy'||!failed);
      if(old){result=old;}
      else if(shouldFail){
        failed=true;
        if(scenario==='drop-ack'){writes++;result.dsh_capture_ack=data.dsh_capture_id;receipts.set(data.dsh_capture_id,result);}
        if(scenario==='stdin-error'){child.stdin.emit('error',new Error('synthetic EPIPE'));return;}
        child.emit('close',1);return;
      }else if(data.dsh_capture_replay&&event==='Stop'&&data.dsh_capture_expected!==proof()){
        result={dsh_event_unconfirmed:true,dsh_capture_rejected:true,systemMessage:'capture_boundary_changed'};
      }else{
        writes++;result.dsh_capture_ack=data.dsh_capture_id;receipts.set(data.dsh_capture_id,result);
      }
    }
    child.stdout.emit('data',Buffer.from(JSON.stringify(result)));child.emit('close',0);
  });}});child.kill=()=>{};return child;
};
syncBuiltinESMExports();
const {apply}=await import(pathToFileURL(process.argv[1]).href);
function install(){apply({tools:{register(){}},effect(){},
  inject(names,cb){if(names.includes('webServer'))cb({webServer:{register(route){routes.set(route.path,route);return()=>{};}},effect(fn){return fn();}});},
  on(name,cb){handlers.set(name,cb);},systemPrompt:{context(){}}});}
install();
const session=(id)=>{const s={id,header:{cwd:workspace}};handlers.get('session/created')(s);return s;};
const owner=session('one'),other=session('two');
const step=async(s,turn)=>handlers.get('agent/pre-step')({agent:{id:s.id},turn,step:1,messages:[],signal:{aborted:false}},async()=>({kind:'enter',messages:[{id:'u'+turn,role:'user',source:{kind:'user'},content:[{type:'text',text:'request '+turn}]}]}));
const emit=(s,type,data)=>handlers.get('session/event')(s,{type,data});
const end=async(s,turn)=>{await emit(s,'assistant/message',{turn,step:1,message:{role:'assistant',source:{kind:'model'},content:[{type:'reasoning',text:'PRIVATE_THINKING'},{type:'text',text:'PUBLIC_RESULT'}]}});return emit(s,'turn/end',{turn,reason:{kind:'completed'}});};
const status=async()=>{
  let body;
  await routes.get('/agent-learning/panel-v2').handler({method:'GET',url:'/agent-learning/panel-v2?cwd='+encodeURIComponent(workspace),headers:{}},{writeHead(){},end(s){body=JSON.parse(s);}});
  const result=body.capture_transport;
  assert.deepEqual(Object.keys(result).sort(),['pending_events','persistence','retryable_events','unresolved_events']);
  assert.equal(result.persistence,'bridge_memory_only');
  assert.ok(!JSON.stringify(body).includes('PUBLIC_RESULT'));
  assert.ok(!JSON.stringify(body).includes('PRIVATE_THINKING'));
  return result;
};
const stops=()=>calls.filter(c=>c.event==='Stop');
await step(owner,1);
if(['before-write','drop-ack','task-changed','legacy','always-fail','ttl','dispose','installation','deadline','stdin-error'].includes(scenario)){
  const first=await end(owner,1);assert.equal(first.dsh_event_unconfirmed,true);
  if(scenario==='task-changed')version=2;
  if(scenario==='ttl')clock+=16*60*1000;
  if(scenario==='dispose')handlers.get('session/disposed')(owner);
  if(scenario==='installation'){
    const file=join(dirname(dirname(process.argv[1])),'runtime-release.json');
    const data=JSON.parse(readFileSync(file,'utf8'));data.version='changed';writeFileSync(file,JSON.stringify(data));
  }
  const next=scenario==='dispose'?session('one'):owner;
  if(scenario==='always-fail'){
    for(let i=0;i<8;i++)await emit(owner,'turn/end',{turn:1,reason:{kind:'completed'}});
    assert.equal(stops().length,3,'one initial attempt and only two capture retries');
    const s=await status();assert.equal(s.pending_events,1);assert.equal(s.retryable_events,0);assert.equal(s.unresolved_events,1);
  }else{
    const start=performance.now();await step(next,2);
    const expected=['legacy','ttl','dispose','installation'].includes(scenario)?1:2;
    assert.equal(stops().length,expected);
    if(['before-write','drop-ack','stdin-error'].includes(scenario)){assert.equal(writes,1);assert.equal((await status()).pending_events,0);}
    if(scenario==='task-changed'){assert.equal(writes,0);assert.equal((await status()).unresolved_events,1);}
    if(scenario==='deadline'){
      assert.ok(performance.now()-start>=1400);assert.ok(performance.now()-start<4500,'capture timeout must not await close forever');
      assert.equal(writes,0);assert.equal((await status()).pending_events,1);
    }
  }
}else if(scenario==='post'||scenario==='task-update-post'){
  const exec={agent:{id:owner.id},callId:'read-1',name:scenario==='post'?'read':'task_update',arguments:{path:'README.md'},signal:{aborted:false}};
  let businessCalls=0;
  await handlers.get('tools/pre-execute')(exec,async()=>{businessCalls++;return {kind:'allow'};});
  if(scenario==='task-update-post')version=2;
  await handlers.get('tools/post-execute')(exec,{isError:false,content:'read result'},async()=>({kind:'accept'}));
  if(scenario==='post')await step(owner,2);
  const posts=calls.filter(c=>c.event==='PostToolUse');assert.equal(posts.length,2);assert.equal(writes,1);assert.equal(businessCalls,1);
  assert.equal(posts[0].data.callId,posts[1].data.callId);assert.equal(posts[0].data.dsh_capture_id,posts[1].data.dsh_capture_id);
  if(scenario==='task-update-post'){
    await end(owner,1);assert.equal(stops().at(-1).data.dsh_capture_expected,proof());assert.equal(writes,2);
  }
}else if(scenario==='isolation'){
  await end(owner,1);await step(other,1);assert.equal(stops().length,1);
  await step(owner,2);assert.equal(stops().length,2);assert.equal(writes,1);
}else if(scenario==='one-at-a-time'){
  await end(owner,1);await end(owner,2);assert.equal(stops().length,2);
  await step(owner,3);assert.equal(stops().length,3,'a safe boundary retries at most one queued event');
}else if(scenario==='capacity'){
  for(let n=1;n<=70;n++)await end(owner,n);
  const s=await status();assert.equal(s.pending_events,64);assert.equal(s.unresolved_events,6);
}else if(scenario==='oversized'){
  const exec={agent:{id:owner.id},callId:'big',name:'read',arguments:{},signal:{aborted:false}};
  await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));
  await handlers.get('tools/post-execute')(exec,{content:'z'.repeat(300000)},async()=>({kind:'accept'}));
  const s=await status();assert.equal(s.pending_events,0);assert.equal(s.unresolved_events,1);
}else if(scenario==='unserializable'){
  const exec={agent:{id:owner.id},callId:'bad',name:'read',arguments:{},signal:{aborted:false}};
  await handlers.get('tools/pre-execute')(exec,async()=>({kind:'allow'}));
  let next=0;await handlers.get('tools/post-execute')(exec,{content:1n},async()=>{next++;return {kind:'accept'};});
  assert.equal(next,1);assert.equal((await status()).unresolved_events,1);
}else throw new Error('unknown scenario');
Date.now=originalNow;
console.log(JSON.stringify({scenario,ok:true,calls:calls.length,writes}));
"""


@unittest.skipUnless(NODE, "Node.js is required for the native bridge probe")
class DshCaptureRetryTests(unittest.TestCase):
    def setUp(self):
        local = Path(os.environ.get("ALP_TEST_ROOT", Path(__file__).resolve().parents[1] / "build/local/opt1431/dsh"))
        (local / "tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="capture-native-", dir=local / "tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.plugin = self.root / "plugin"
        (self.plugin / "host").mkdir(parents=True)
        shutil.copytree(BRIDGE.parent, self.plugin / "host", dirs_exist_ok=True)
        (self.plugin / "runtime-release.json").write_text(json.dumps({
            "python": sys.executable, "runtime_directory": str(self.root / "runtime"),
            "workspace": str(self.workspace), "home": str(self.root / "home"), "hook_mode": "observe",
        }), encoding="utf-8")

    def probe(self, scenario):
        result = subprocess.run([NODE, "--input-type=module", "-e", PROBE,
            str(self.plugin / "host/index.mjs"), scenario, str(self.workspace)],
            cwd=self.workspace, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertTrue(json.loads(result.stdout.decode("utf-8"))["ok"])

    def test_failed_before_write_is_retried_before_next_prompt(self): self.probe("before-write")
    def test_lost_ack_is_retried_with_one_persisted_event(self): self.probe("drop-ack")
    def test_changed_task_rejects_original_envelope_without_resigning(self): self.probe("task-changed")
    def test_post_capture_retry_never_runs_business_tool_again(self): self.probe("post")
    def test_task_update_post_ack_recovery_precedes_terminal_proof(self): self.probe("task-update-post")
    def test_other_session_cannot_flush_pending_capture(self): self.probe("isolation")
    def test_attempt_budget_is_finite_and_visible(self): self.probe("always-fail")
    def test_only_one_record_is_retried_per_safe_boundary(self): self.probe("one-at-a-time")
    def test_capacity_loss_is_visible_without_unbounded_memory(self): self.probe("capacity")
    def test_large_unretained_failure_is_visible(self): self.probe("oversized")
    def test_expired_capture_is_not_replayed(self): self.probe("ttl")
    def test_disposed_session_does_not_replay_into_new_session(self): self.probe("dispose")
    def test_changed_installation_does_not_replay_old_envelope(self): self.probe("installation")
    def test_legacy_runtime_without_proof_is_never_automatically_replayed(self): self.probe("legacy")
    def test_bad_transport_value_keeps_business_callback_available(self): self.probe("unserializable")
    def test_capture_retry_deadline_does_not_wait_for_process_close(self): self.probe("deadline")
    def test_stdin_pipe_failure_is_observed_and_capture_can_retry(self): self.probe("stdin-error")


if __name__ == "__main__":
    unittest.main()
