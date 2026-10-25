"""DSH installation boundaries and frozen visual control requests."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_status, request_receipt
from agent_learning.admin_usage import configure_project_budget
from agent_learning.dsh_panel import apply_preset, bound_project, snapshot
from agent_learning.runtime import ContractError, Runtime


class DshPanelContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "parent"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={}, host="dsh")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def payload(self, preset="light"):
        return {"admin_preset": preset, "panel_snapshot": snapshot(self.r, self.p),
                "request_id": "displayed-request", "confirm": True}

    def test_subdirectory_allowed_but_nested_checkout_and_alias_rejected(self):
        sub = self.workspace / "src"
        sub.mkdir()
        self.assertEqual(bound_project(self.r, sub, self.workspace), self.p)
        (sub / ".git").mkdir()
        with self.assertRaises(ContractError):
            bound_project(self.r, sub, self.workspace)
        (sub / ".git").rmdir()
        self.r.config["project_aliases"][str(sub)] = "another-project"
        with self.assertRaises(ContractError):
            bound_project(self.r, sub, self.workspace)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_stale_displayed_versions_cannot_overwrite_new_budget(self):
        body = self.payload()
        configure_project_budget(self.r, self.p, {"max_calls_per_day": 300}, expected_version=0,
                                 expected_epoch=self.r.session_epoch())
        with self.assertRaisesRegex(ContractError, "refresh"):
            apply_preset(self.r, self.p, body)
        self.assertEqual(control_status(self.r, self.p)["project_usage"]["max_calls_per_day"], 300)
        self.assertFalse(request_receipt(self.r, self.p, body["request_id"])["found"])

    def test_retry_same_visual_request_replays_durable_receipt_once(self):
        body = self.payload()
        with patch("agent_learning.admin_control.wake_admin", return_value={"status": "accepted"}) as wake:
            first = apply_preset(self.r, self.p, body)
            second = apply_preset(self.r, self.p, body)
        self.assertEqual(first, second)
        self.assertEqual(wake.call_count, 1)
        self.assertEqual(self.r.admin_policy(self.p)["model_mode"], "first")
        self.assertTrue(request_receipt(self.r, self.p, body["request_id"])["found"])
        with self.assertRaisesRegex(ContractError, "reused"):
            apply_preset(self.r, self.p, dict(body, admin_preset="active"))

    def test_missing_confirmation_snapshot_or_wrong_project_fails_closed(self):
        body = self.payload()
        for update in ({"confirm": False}, {"panel_snapshot": None}, {"panel_snapshot": {
                **body["panel_snapshot"], "project_id": "wrong"}}, {"request_id": None}):
            with self.subTest(update=update), self.assertRaises(ContractError):
                apply_preset(self.r, self.p, {**body, **update})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_control_receipts").fetchone()[0], 0)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_shipped_host_path_matcher_rejects_unbound_and_nested_checkouts(self):
        path = Path(__file__).resolve().parents[1] / "host_resources/dsh/host/index.mjs"
        source = path.read_text(encoding="utf-8")
        functions = source[source.index("function loadAllowedWorkspaces"):source.index("function extractTextFromBlocks")]
        sub = self.workspace / "src"
        sub.mkdir()
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        code = """
const {realpathSync,statSync,existsSync}=require('node:fs');
const {relative,isAbsolute,sep,join,dirname}=require('node:path');
const assert=require('node:assert/strict');
const value=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
eval(value.functions+';globalThis.match=matchInstalledWorkspace');
assert.equal(match(value.sub,{workspace:value.parent}),realpathSync.native(value.parent));
assert.equal(match(value.nested,{workspace:value.parent}),null);
assert.equal(match(value.nested,{workspace:value.nested}),realpathSync.native(value.nested));
assert.equal(match(value.sub,{}),null);
assert.equal(match('',{workspace:value.parent}),null);
assert.equal(match('relative',{workspace:value.parent}),null);
console.log('ok');
"""
        result = subprocess.run([shutil.which("node"), "-e", code], text=True, encoding="utf-8",
            input=json.dumps({"functions": functions, "parent": str(self.workspace), "sub": str(sub),
                              "nested": str(nested)}), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_client_retries_uncertain_settings_with_same_id_and_frozen_snapshot(self):
        client = Path(__file__).resolve().parents[1] / "host_resources/dsh/client/index.js"
        code = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let factory,posts=[],confirms=0,turn=0;
const summary={ok:true,installed:true,project_id:'p',project_name:'p',workspace:'p',
  project_usage:{scope:'project',project_id:'p',max_calls_per_day:15},
  panel_snapshot:{project_id:'p',host_scope:'dsh:default',installation_id:'install-a',
    expected:{expected_policy_version:0,expected_project_budget_version:0,expected_settings_version:0,
      expected_session_epoch:0,expected_governance_epoch:0},model_mode:'default',model:'',project_limits:{}}};
const response=value=>({ok:true,status:200,json:async()=>value});
const fetch=async(_url,options)=>{
 if(!options)return response({...summary,panel_snapshot:{...summary.panel_snapshot,expected:{...summary.panel_snapshot.expected,expected_policy_version:99}}});
 const value=JSON.parse(options.body);posts.push(value);
 if(value.action==='admin_receipt')return response({ok:true,found:false});
 if(++turn===1)throw new Error('synthetic interrupted local response');
 return response({ok:true});
};
const window={confirm:()=>{confirms++;return true;},crypto:{randomUUID:()=> 'one-request'},
 __ModuleLoader__:{load:item=>{factory=item.factory;}}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8').replace('exports.apply = apply;',
 'exports.buildControlPanel = buildControlPanel; exports.apply = apply;'),{window,fetch,navigator:{language:'en'}});
const plugin=factory(()=>({})),states=[];let cursor=0;
const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),
 useState:value=>{const n=cursor++;if(!(n in states))states[n]=value;return[states[n],v=>{states[n]=v;}];},
 useCallback:fn=>fn,useEffect(){}};
const Panel=plugin.buildControlPanel(react,null),render=()=>{cursor=0;return Panel({initialSummary:summary,cwd:'bound',sessionId:'session'});};
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
const button=tree=>nodes(tree).find(n=>n.type==='button'&&n.props.key==='balanced');
const flush=()=>new Promise(resolve=>setTimeout(resolve,10));
(async()=>{
 button(render()).props.onClick();await flush();await flush();
 assert.equal(posts.length,2);assert.equal(posts[1].action,'admin_receipt');
 button(render()).props.onClick();await flush();
 const attempts=posts.filter(p=>p.action==='set_admin_preset');assert.equal(attempts.length,2);
 assert.deepEqual(attempts[1],attempts[0]);assert.equal(attempts[0].request_id,'one-request');
 assert.equal(attempts[0].panel_snapshot.expected.expected_policy_version,0);
 assert.equal(attempts[0].installation_id,'install-a');assert.equal(attempts[0].confirm,true);assert.equal(confirms,1);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", code, str(client)], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_client_reports_only_confirmed_browser_stops(self):
        client = Path(__file__).resolve().parents[1] / "host_resources/dsh/client/index.js"
        code = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let factory,receipt;
const summary={ok:true,installed:true,project_id:'p',browser_running:true,panel_snapshot:{installation_id:'install-a'}};
const response=value=>({ok:true,status:200,json:async()=>value});
const fetch=async(_url,options)=>response(options?{ok:true,...receipt}:summary);
const window={__ModuleLoader__:{load:item=>{factory=item.factory;}}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8').replace('exports.apply = apply;',
 'exports.buildControlPanel = buildControlPanel; exports.apply = apply;'),{window,fetch,navigator:{language:'en'}});
const plugin=factory(()=>({})),states=[];let cursor=0;
const react={createElement:(type,props,...children)=>({type,props,children:children.flat(Infinity)}),
 useState:value=>{const n=cursor++;if(!(n in states))states[n]=value;return[states[n],v=>{states[n]=v;}];},
 useCallback:fn=>fn,useEffect(){}};
const Panel=plugin.buildControlPanel(react,null),render=()=>{cursor=0;return Panel({initialSummary:summary,cwd:'bound',sessionId:'session'});};
const nodes=n=>!n||typeof n!=='object'?[]:[n,...(n.children||[]).flatMap(nodes)];
const stop=tree=>nodes(tree).find(n=>n.type==='button'&&nodes(n).some(item=>item.type==='span'&&item.children.includes('Stop Background Browser Server')));
const flush=()=>new Promise(resolve=>setTimeout(resolve,10));
(async()=>{
 for(const [value,note] of [
  [{running:false,stopped:true},'Stopped background browser server'],
  [{running:false,stopped:false},'No knowledge browser server is running for this project'],
  [{running:true,stopped:true},'Stop result is unconfirmed; refresh the status'],
  [{},'Stop result is unconfirmed; refresh the status']]){
  receipt=value;stop(render()).props.onClick();await flush();assert.equal(states[1],note);
 }
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", code, str(client)], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_actual_host_route_forwards_frozen_request_and_rejects_changed_installation(self):
        plugin = self.root / "bundle"
        (plugin / "host").mkdir(parents=True)
        host_source = Path(__file__).resolve().parents[1] / "host_resources/dsh/host/index.mjs"
        shutil.copytree(host_source.parent, plugin / "host", dirs_exist_ok=True)
        receipt = {"workspace": str(self.workspace), "runtime_directory": str(self.root / "runtime"),
                   "home": str(self.root / "home"), "python": sys.executable, "release_id": "isolated-v1"}
        (plugin / "runtime-release.json").write_text(json.dumps(receipt), encoding="utf-8")
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        code = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
const [modulePath,workspace,nested]=process.argv.slice(1),calls=[];
const view={project_id:'project-a',host_scope:'dsh:default',expected:{expected_policy_version:7,
 expected_project_budget_version:8,expected_settings_version:9,expected_session_epoch:0,expected_governance_epoch:0},
 model_mode:'first',model:'',project_limits:{}};
childProcess.spawn=(_executable,args)=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();let input='';
 child.stdin={write:value=>{input+=value;},end:()=>queueMicrotask(()=>{
  const payload=input?JSON.parse(input):{};calls.push(payload);
  const result=payload.panel_status?{ok:true,result:{data:{project_id:'project-a',panel_snapshot:view}}}:
    payload.admin_preset?{ok:true,result:{preset:payload.admin_preset}}:{running:false};
  child.stdout.emit('data',Buffer.from(JSON.stringify(result)));child.emit('close',0);
 })};child.kill=()=>{};return child;
};syncBuiltinESMExports();
let route;
const scope={skills:{register(){}},commands:{register(){}},effect:fn=>fn(),
 webServer:{register:value=>{if(value.path==='/agent-learning/panel-v2')route=value;}}};
const {apply}=await import(pathToFileURL(modulePath).href);
apply({tools:{register(){}},on(){},effect(){},inject(_names,callback){callback(scope);}});
assert(route);
const request=async(method,cwd,body)=>{
 let status,value;
 const req={method,url:'/agent-learning/panel-v2?cwd='+encodeURIComponent(cwd||''),headers:{},
  async *[Symbol.asyncIterator](){if(body)yield Buffer.from(JSON.stringify(body));}};
 const res={writeHead:code=>{status=code;return res;},end:raw=>{value=raw?JSON.parse(raw):null;}};
 await route.handler(req,res);return{status,value};
};
let result=await request('GET',nested);assert.equal(result.value.installed,false);assert.equal(calls.length,0);
result=await request('GET',workspace);assert.equal(result.value.installed,true);
const frozen=result.value.panel_snapshot;
assert.equal(frozen.expected.expected_policy_version,7);
result=await request('POST',workspace,{action:'set_admin_preset',cwd:workspace,project_id:'project-a',
 installation_id:frozen.installation_id,preset:'light',panel_snapshot:frozen,request_id:'stable-request',confirm:true});
assert.equal(result.status,200);
const applied=calls.find(row=>row.admin_preset);assert(applied);
assert.equal(applied.request_id,'stable-request');assert.deepEqual(applied.panel_snapshot,frozen);
assert.equal(applied.workspace,workspace);assert.equal(applied.panel_bound_workspace,workspace);
const before=calls.length;
result=await request('POST',workspace,{action:'set_admin_preset',cwd:workspace,project_id:'project-a',
 installation_id:'old-installation',preset:'active'});
assert.equal(result.status,409);assert.equal(calls.length,before);
result=await request('POST','',{action:'set_admin_preset',project_id:'project-a'});
assert.equal(result.status,403);assert.equal(calls.length,before);
console.log('ok');
"""
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code,
                                 str(plugin / "host/index.mjs"), str(self.workspace), str(nested)],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_host_route_opens_exact_browser_mode_and_disposes_shared_starts(self):
        plugin = self.root / "browser-bundle"
        host_source = Path(__file__).resolve().parents[1] / "host_resources/dsh/host"
        shutil.copytree(host_source, plugin / "host")
        (plugin / "runtime-release.json").write_text(json.dumps({"workspace": str(self.workspace),
            "runtime_directory": str(self.root / "runtime"), "home": str(self.root / "home"),
            "python": sys.executable, "release_id": "isolated-v1"}), encoding="utf-8")
        code = r'''
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
const [modulePath,workspace]=process.argv.slice(1),browsers=[],opened=[],tools=new Map(),commands=new Map();
const view={project_id:'project-a',host_scope:'dsh:default',expected:{}};
childProcess.spawn=(_exe,args)=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();
 child.stdout.setEncoding=()=>{};child.exitCode=null;child.killed=false;let input='';
 child.kill=()=>{child.killed=true;queueMicrotask(()=>child.emit('close',0));};
 child.stdin={write:value=>{input+=value;},end:()=>queueMicrotask(()=>{
  const payload=input?JSON.parse(input):{};
  if(args.includes('-c'))opened.push(args.at(-1));
  const data=payload.panel_status?{ok:true,result:{data:{project_id:'project-a',panel_snapshot:view}}}:{};
  child.stdout.emit('data',Buffer.from(JSON.stringify(data)));child.exitCode=0;child.emit('close',0);
 })};
 if(args.includes('browser')){
  child.pid=400+browsers.length;browsers.push(child);queueMicrotask(()=>{
   const url='http://127.0.0.1:'+(12340+browsers.length)+'/',review=args.includes('--review'),admin=args.includes('--admin-control');
   child.stdout.emit('data',JSON.stringify({project:'project-a',url,access_token:'read',read_only:!review&&!admin,
    ...(review?{review_url:url+'#token=read&review_token=review'}:{}),
    ...(admin?{admin_control_url:url+'#token=read&review_token=review&admin_token=admin'}:{})})+'\n');
  });
 }
 return child;
};syncBuiltinESMExports();
let route;
const scope={skills:{register(){}},commands:{register:spec=>{commands.set(spec.name,spec);}},effect:fn=>fn(),
 webServer:{register:value=>{if(value.path==='/agent-learning/panel-v2')route=value;}}};
const {apply}=await import(pathToFileURL(modulePath).href);
apply({tools:{register:spec=>{tools.set(spec.name,spec);}},on(){},effect(){},inject(_names,callback){callback(scope);}});
const request=async(method,body)=>{
 let status,value;
 const req={method,url:'/agent-learning/panel-v2?cwd='+encodeURIComponent(workspace),headers:{},
  async *[Symbol.asyncIterator](){if(body)yield Buffer.from(JSON.stringify(body));}};
 const res={writeHead:code=>{status=code;return res;},end:raw=>{value=JSON.parse(raw);}};
 await route.handler(req,res);assert.equal(status,200,JSON.stringify(value));return value;
};
try{
 const initial=await request('GET'),binding={cwd:workspace,project_id:'project-a',installation_id:initial.panel_snapshot.installation_id};
 assert.equal((await request('POST',{...binding,action:'open_review'})).mode,'review');
 assert(opened.at(-1).includes('admin_token'));
 const modelBrowser=tools.get('learning_browser');assert(modelBrowser);
 const modelStatus=await modelBrowser.execute({action:'status'},{});
 assert.equal(modelStatus.running,false);assert(!JSON.stringify(modelStatus).includes('admin_token'));assert(!JSON.stringify(modelStatus).includes('review_token'));
 assert.equal((await modelBrowser.execute({action:'stop'},{})).stopped,false);assert.equal(browsers[0].killed,false);
 const slash=commands.get('learning-browser');assert(slash);
 const slashStatus=await slash.handler({rawInput:'status'});
 assert(!slashStatus.text.includes('admin_token'));assert(!slashStatus.text.includes('review_token'));assert(slashStatus.text.includes('只读知识库浏览器未运行'));
 await assert.rejects(modelBrowser.execute({action:'start',adminControl:true},{}),/read-only browsers only/);
 const reads=await Promise.all([request('POST',{...binding,action:'open_browser'}),request('POST',{...binding,action:'open_browser'})]);
 assert(reads.every(item=>item.mode==='readonly'));assert.equal(browsers.length,2);
 assert(!opened.at(-1).includes('admin_token'));assert(!opened.at(-1).includes('review_token'));
 const active=await modelBrowser.execute({action:'status'},{});assert.equal(active.read_only,true);assert.equal(active.pid,browsers[1].pid);
 assert(!JSON.stringify(active).includes('admin_token'));assert(!JSON.stringify(active).includes('review_token'));
 await modelBrowser.execute({action:'stop'},{});assert.equal(browsers[0].killed,false);assert.equal(browsers[1].killed,true);
 assert.equal((await request('GET')).browser_running,true);
 await request('POST',{...binding,action:'stop_browser'});assert(browsers.every(child=>child.killed));
}finally{globalThis.__ALP_DSH_DISPOSE__();}
assert(browsers.every(child=>child.killed));console.log('ok');
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code,
                                 str(plugin / "host/index.mjs"), str(self.workspace)], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_host_route_shares_project_browsers_across_subdirectories_and_isolates_other_roots(self):
        plugin = self.root / "root-browser-bundle"
        host_source = Path(__file__).resolve().parents[1] / "host_resources/dsh/host"
        shutil.copytree(host_source, plugin / "host")
        sub = self.workspace / "src"
        sub.mkdir()
        other = self.root / "other-project"
        other.mkdir()
        nested = self.workspace / "nested-checkout"
        nested.mkdir()
        (nested / ".git").mkdir()
        (plugin / "runtime-release.json").write_text(json.dumps({"workspace": str(self.workspace),
            "workspaces": [str(self.workspace), str(other)], "runtime_directory": str(self.root / "runtime"),
            "home": str(self.root / "home"), "python": sys.executable, "release_id": "isolated-v1"}), encoding="utf-8")
        code = r'''
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';
const [modulePath,workspace,sub,other,nested]=process.argv.slice(1),browsers=[],checks=[];
const project=cwd=>cwd===other?'project-b':'project-a';
childProcess.spawn=(_exe,args)=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();
 child.stdout.setEncoding=()=>{};child.exitCode=null;child.killed=false;let input='';
 child.kill=()=>{child.killed=true;queueMicrotask(()=>child.emit('close',0));};
 child.stdin={write:value=>{input+=value;},end:()=>queueMicrotask(()=>{
  const payload=input?JSON.parse(input):{};if(payload.panel_status)checks.push(payload);
  const identity=project(payload.workspace),valid=!payload.panel_project_id||payload.panel_project_id===identity;
  const data=payload.panel_status?{ok:valid,result:{data:{project_id:identity,panel_snapshot:{project_id:identity,host_scope:'dsh:default',expected:{}}}}}:{};
  child.stdout.emit('data',Buffer.from(JSON.stringify(data)));child.exitCode=0;child.emit('close',0);
 })};
 if(args.includes('browser')){
  child.workspace=args[args.indexOf('--workspace')+1];child.pid=400+browsers.length;browsers.push(child);queueMicrotask(()=>{
   const url='http://127.0.0.1:'+(12340+child.pid)+'/',review=args.includes('--review'),admin=args.includes('--admin-control');
   child.stdout.emit('data',JSON.stringify({project:project(child.workspace),url,access_token:'read',read_only:!review&&!admin,
    ...(review?{review_url:url+'#token=read&review_token=review'}:{}),
    ...(admin?{admin_control_url:url+'#token=read&review_token=review&admin_token=admin'}:{})})+'\n');
  });
 }
 return child;
};syncBuiltinESMExports();
let route;
const scope={skills:{register(){}},commands:{register(){}},effect:fn=>fn(),
 webServer:{register:value=>{if(value.path==='/agent-learning/panel-v2')route=value;}}};
const {apply}=await import(pathToFileURL(modulePath).href);
apply({tools:{register(){}},on(){},effect(){},inject(_names,callback){callback(scope);}});
const request=async(method,cwd,body)=>{
 let status,value;
 const req={method,url:'/agent-learning/panel-v2?cwd='+encodeURIComponent(cwd),headers:{},
  async *[Symbol.asyncIterator](){if(body)yield Buffer.from(JSON.stringify(body));}};
 const res={writeHead:code=>{status=code;return res;},end:raw=>{value=JSON.parse(raw);}};
 await route.handler(req,res);return{status,value};
};
const post=async(cwd,view,action,extra={})=>request('POST',cwd,{cwd,project_id:view.project_id,
 installation_id:view.panel_snapshot.installation_id,action,...extra});
try{
 const rootView=(await request('GET',workspace)).value,subView=(await request('GET',sub)).value,otherView=(await request('GET',other)).value;
 assert(rootView.installed&&subView.installed&&otherView.installed);
 assert.equal(rootView.project_id,subView.project_id);assert.notEqual(rootView.project_id,otherView.project_id);
 assert.equal((await post(workspace,rootView,'open_review')).status,200);
 assert.equal((await request('GET',sub)).value.browser_running,true);
 assert.equal((await post(sub,subView,'open_review')).status,200);assert.equal(browsers.length,1);
 await post(other,otherView,'open_review');await post(sub,subView,'open_browser');
 assert.deepEqual(browsers.map(child=>child.workspace),[workspace,other,workspace]);
 const before=browsers.length,wrongProject=await post(sub,subView,'stop_browser',{project_id:otherView.project_id});
 assert.equal(wrongProject.status,409);assert.equal(browsers.length,before);assert(browsers.every(child=>!child.killed));
 assert.equal((await post(nested,rootView,'stop_browser')).status,403);
 const stopped=await post(sub,subView,'stop_browser');assert.equal(stopped.status,200);assert.equal(stopped.value.stopped,true);
 assert.deepEqual(browsers.map(child=>child.killed),[true,false,true]);
 assert.equal((await request('GET',workspace)).value.browser_running,false);
 assert.equal((await request('GET',sub)).value.browser_running,false);
 assert.equal((await request('GET',other)).value.browser_running,true);
 assert.equal((await post(sub,subView,'stop_browser')).value.stopped,false);
 assert(checks.some(row=>row.workspace===sub&&row.panel_bound_workspace===workspace&&row.panel_project_id==='project-a'));
}finally{globalThis.__ALP_DSH_DISPOSE__();}
assert(browsers.every(child=>child.killed));console.log('ok');
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code,
                                 str(plugin / "host/index.mjs"), str(self.workspace), str(sub), str(other), str(nested)],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
