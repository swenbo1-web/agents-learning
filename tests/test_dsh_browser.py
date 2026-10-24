"""Actual DSH browser manager with fake children and deterministic platform paths."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


MODULE = Path(__file__).resolve().parents[1] / "host_resources/dsh/host/browser.mjs"
PROBE = r'''
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {pathToFileURL} from 'node:url';
const {module,scenario}=JSON.parse(await new Promise(resolve=>{let input='';process.stdin.on('data',chunk=>input+=chunk);process.stdin.on('end',()=>resolve(input));}));
const {createBrowserManager,browserPathKey}=await import(pathToFileURL(module));
const children=[],opened=[];let receipt={python:'fake-python',runtime_directory:'/fake/runtime',home:'/fake/home',release_id:'v1'};
let preflight=null,fail=false,hang=false,malformed=false,preflights=0;
const runPython=async(_python,args)=>{
 if(args.includes('status')){preflights++;if(preflight)await preflight;}
 else if(args.includes('-c'))opened.push(args.at(-1));
 return {code:0,stdout:''};
};
const spawnProcess=(_python,args)=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdout.setEncoding=()=>{};
 child.exitCode=null;child.killed=false;child.pid=children.length+1;child.kill=()=>{child.killed=true;};children.push(child);
 if(!hang)queueMicrotask(()=>{
  if(fail){fail=false;child.emit('error',new Error('fake unavailable'));return;}
  if(malformed){child.stdout.emit('data','private-invalid-output\n');return;}
  const url='http://127.0.0.1:'+(12340+child.pid)+'/',review=args.includes('--review'),admin=args.includes('--admin-control');
  child.stdout.emit('data',JSON.stringify({project:args[args.indexOf('--workspace')+1],url,access_token:'read',read_only:!review&&!admin,
   ...(review?{review_url:url+'#token=read&review_token=review'}:{}),
   ...(admin?{admin_control_url:url+'#token=read&review_token=review&admin_token=admin'}:{})})+'\n');
 });return child;
};
const manager=createBrowserManager({loadReceipt:()=>({...receipt}),runPython,spawnProcess,canonicalWorkspace:value=>value,
 platform:scenario==='windows'?'win32':'linux',startupTimeout:scenario==='timeout'?5:10000});
const start=(options={},workspace='/audit/Foo')=>manager.manage('start',{open:false,...options},workspace);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
try{
 if(scenario==='modes'){
  const review=await start({review:true,adminControl:true,open:true});
  const read=await start({open:true});
  assert.equal(children.length,2);assert.equal(read.read_only,true);assert.equal(read.review,false);assert.equal(read.admin_control,false);
  assert.notEqual(read.pid,review.pid);assert(!opened.at(-1).includes('admin_token'));assert(!opened.at(-1).includes('review_token'));
  assert.equal((await start()).pid,read.pid);assert.equal(children.length,2);
  await manager.manage('stop',{},'/audit/Foo');assert(children.every(child=>child.killed));
 }else if(scenario==='concurrent'){
  let release;preflight=new Promise(resolve=>{release=resolve;});
  const first=start(),second=start();assert.equal(preflights,1);release();
  const [a,b]=await Promise.all([first,second]);assert.equal(a.pid,b.pid);assert.equal(children.length,1);assert.equal(b.reused,true);
  manager.close();assert(children.every(child=>child.killed));
 }else if(scenario==='stop_pending'||scenario==='close_pending'){
  let release;preflight=new Promise(resolve=>{release=resolve;});const pending=start();
  if(scenario==='stop_pending')assert.equal((await manager.manage('stop',{},'/audit/Foo')).stopped,true);else manager.close();
  release();await assert.rejects(pending,/cancelled/);assert.equal(children.length,0);
  if(scenario==='stop_pending'){preflight=null;await start();assert.equal(children.length,1);}
 }else if(scenario==='posix'||scenario==='windows'){
  const a=await start({},'/audit/Foo'),b=await start({},'/audit/foo');
  if(scenario==='posix'){
   assert.notEqual(browserPathKey('/audit/Foo','linux'),browserPathKey('/audit/foo','linux'));
   assert.notEqual(a.pid,b.pid);assert.equal((await manager.manage('status',{},'/audit/foo')).project,'/audit/foo');
   await manager.manage('stop',{},'/audit/foo');assert.equal(children[0].killed,false);assert.equal(children[1].killed,true);
  }else{assert.equal(browserPathKey('C:\\Audit\\Foo','win32'),browserPathKey('c:\\audit\\foo','win32'));assert.equal(a.pid,b.pid);}
 }else if(scenario==='installation'){
  const a=await start();receipt.release_id='v2';assert.equal((await manager.manage('status',{},'/audit/Foo')).running,false);
  assert(children[0].killed);assert.notEqual((await start()).pid,a.pid);
 }else if(scenario==='failure'){
  fail=true;await assert.rejects(start(),/failed to start/);assert(children[0].killed);
  assert.equal((await start()).running,true);assert.equal(children.length,2);
 }else if(scenario==='timeout'){
  hang=true;await assert.rejects(start(),/Timed out/);assert(children[0].killed);
  hang=false;assert.equal((await start()).running,true);
 }else if(scenario==='invalid'){
  malformed=true;await assert.rejects(start(),error=>error.message==='Invalid Agent Learning browser startup response');assert(children[0].killed);
 }else if(scenario==='late_close'){
  await start();const first=children[0];const second=await start({port:23456});first.emit('close',0);await flush();
  assert.equal((await manager.manage('status',{},'/audit/Foo')).pid,second.pid);
 }else if(scenario==='abort'){
  hang=true;const controller=new AbortController(),pending=manager.manage('start',{open:false},'/audit/Foo',controller.signal);
  await flush();controller.abort();await assert.rejects(pending,/cancelled/);assert(children[0].killed);
 }else throw new Error('unknown scenario');
 console.log('ok');
}finally{manager.close();}
'''


@unittest.skipUnless(shutil.which("node"), "Node.js required for DSH browser checks")
class DshBrowserTests(unittest.TestCase):
    def test_browser_identity_modes_concurrency_and_cleanup(self):
        for scenario in ("modes", "concurrent", "stop_pending", "close_pending", "posix", "windows",
                         "installation", "failure", "timeout", "invalid", "late_close", "abort"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", PROBE],
                    input=json.dumps({"module": str(MODULE), "scenario": scenario}), capture_output=True,
                    text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ok", result.stdout)
