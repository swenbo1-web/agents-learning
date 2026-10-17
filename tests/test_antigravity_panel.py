"""Offline Antigravity App panel boundary checks, without starting a real host."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


RESOURCE = Path(__file__).resolve().parents[1] / "host_resources" / "antigravity" / "sidecars" / "control"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for Antigravity panel checks")
class AntigravityPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-antigravity-panel-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.runtime = self.root / "runtime"
        (self.runtime / "scripts").mkdir(parents=True)
        (self.runtime / "scripts" / "learning.py").write_text("# isolated fake entry\n", encoding="utf-8")
        self.plugin = self.root / "plugin"
        self.plugin.mkdir()
        self.receipt = {"target_host": "antigravity", "python": sys.executable,
                        "runtime_directory": str(self.runtime), "workspace": str(self.project),
                        "home": str(self.root / "home")}
        self.receipt_path = self.plugin / "runtime-release.json"
        self.receipt_path.write_text(json.dumps(self.receipt), encoding="utf-8")

    def run_node(self, body):
        code = "\n".join([
            "import assert from 'node:assert/strict';",
            "import { EventEmitter } from 'node:events';",
            "import { writeFileSync } from 'node:fs';",
            "import { join } from 'node:path';",
            "import { pathToFileURL } from 'node:url';",
            "const api = await import(pathToFileURL(process.env.ALP_PANEL_MODULE));",
            "const installation = api.loadInstallation(process.env.ALP_PANEL_INSTALLATION);",
            body,
        ])
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code],
                                env={**os.environ, "ALP_PANEL_MODULE": str(RESOURCE / "main.mjs"),
                                     "ALP_PANEL_INSTALLATION": str(self.plugin)},
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_workspace_requires_one_local_uri_matching_exact_installation(self):
        self.run_node("""
const uri = pathToFileURL(installation.receipt.workspace).href;
assert.equal(api.resolveWorkspace([uri], installation), installation.receipt.workspace);
for (const uris of [[], [uri, uri], ['https://example.com'], ['file://remote/share'],
                    [uri + '?x=1'], [uri + '#x'], [pathToFileURL(installation.directory).href]]) {
  assert.equal(api.resolveWorkspace(uris, installation), null);
}
""")

    def test_backend_hides_wrong_workspace_before_calling_python(self):
        self.run_node("""
let calls = 0;
const bridge = { assertCurrent() {}, async request(method, params) { calls++; return {visible:true,params}; } };
const handle = api.createController(installation, bridge);
const wrong = await handle('bootstrap', {workspace_uris:[pathToFileURL(installation.directory).href],conversation_id:'native-chat'});
assert.equal(wrong.visible, false); assert.equal(calls, 0);
const good = await handle('bootstrap', {workspace_uris:[pathToFileURL(installation.receipt.workspace).href],conversation_id:'native-chat'});
assert.equal(good.visible, true); assert.equal(calls, 1);
assert.equal(good.params.workspace, installation.receipt.workspace);
assert.equal(good.params.conversation_id, 'native-chat');
assert.equal(good.params.workspace_uris, undefined);
""")

    def test_write_actions_require_explicit_confirmation_and_snapshot(self):
        self.run_node("""
const base = {workspace_uris:[pathToFileURL(installation.receipt.workspace).href],conversation_id:'native-chat',capability:'memory-only',action:'set_admin_preset',preset:'balanced',request_id:'click-1'};
const expected = {expected_policy_version:1,expected_project_budget_version:1,expected_settings_version:1,expected_session_epoch:0,expected_governance_epoch:0};
assert.throws(() => api.validateInput('action', base));
assert.throws(() => api.validateInput('action', {...base,confirm:true}));
assert.throws(() => api.validateInput('action', {...base,confirm:true,expected:{...expected,rogue:0}}));
assert.throws(() => api.validateInput('action', {...base,confirm:true,expected:{...expected,expected_settings_version:-1}}));
assert.equal(api.validateInput('action', {...base,confirm:true,expected}).preset, 'balanced');
assert.throws(() => api.validateInput('action', {...base,action:'archive_history',preset:undefined,confirm:false}));
assert.throws(() => api.validateInput('action', {...base,action:'publish_all',confirm:true,expected}));
assert.throws(() => api.validateInput('bootstrap', {workspace_uris:base.workspace_uris,conversation_id:'native-chat',workspace:'/forged'}));
assert.throws(() => api.validateInput('action', {...base,capability:'x'.repeat(20000)}));
""")

    def test_bridge_uses_fixed_entry_and_closes_pending_requests(self):
        self.run_node("""
let spawned;
const child = new EventEmitter(); child.exitCode = null;
child.stdout = new EventEmitter(); child.stdout.setEncoding = () => {};
child.stderr = new EventEmitter();
let sent, ended = false, killed = false;
child.stdin = {write(line, done) { sent = JSON.parse(line); done(); },end() { ended = true; }};
child.kill = () => { killed = true; };
const bridge = new api.PanelBridge(installation, (exe,args,opts) => {spawned={exe,args,opts}; return child;});
const timeouts=[]; const originalTimeout=globalThis.setTimeout;
globalThis.setTimeout=(callback,delay)=>{timeouts.push(delay);return originalTimeout(callback,delay);};
const result = bridge.request('bootstrap',{workspace:installation.receipt.workspace,conversation_id:'chat'});
assert.equal(spawned.exe, installation.receipt.python);
assert.deepEqual(spawned.args, ['-B', join(installation.receipt.runtime_directory,'scripts','learning.py'), 'panel-serve','--installation',installation.directory]);
assert.equal(spawned.opts.windowsHide, true);
assert.equal(spawned.opts.shell, undefined);
child.stdout.emit('data', JSON.stringify({id:sent.id,result:{visible:true}}) + '\\n');
assert.equal((await result).visible, true);
const pending = bridge.request('action',{action:'archive_history'});
assert.equal(timeouts.at(-1),210000);
bridge.close();
await assert.rejects(pending);
assert.equal(ended, true); assert.equal(killed, true);
assert.throws(() => bridge.start());
""")

    def test_receipt_change_and_uninstall_fail_closed(self):
        self.run_node("""
const bridge = new api.PanelBridge(installation);
writeFileSync(installation.directory + '/runtime-release.json', JSON.stringify({...installation.receipt,release_id:'changed'}));
assert.throws(() => bridge.assertCurrent());
assert.throws(() => api.loadInstallation(installation.directory + '/missing'));
""")

    def test_http_guard_rejects_sdk_agent_routes_and_unbounded_bodies(self):
        self.run_node("""
const server = new EventEmitter(); let accepted = 0;
server.on('request', () => {accepted++;}); api.guardRequests(server, true);
function send(url, method='POST', headers={'content-length':'2','content-type':'application/json'}) {
  let status = 0; const response={writeHead(code){status=code;},end(){}};
  server.emit('request',{url,method,headers},response); return status;
}
assert.equal(send('/api/bootstrap'),0); assert.equal(accepted,1);
for (const [url,method,headers] of [
 ['/_sidecar/send-message','POST',{'content-length':'2','content-type':'application/json'}],
 ['/api/action','GET',{}],
 ['/api/action','POST',{'content-length':'20000','content-type':'application/json'}],
 ['/api/action','POST',{'transfer-encoding':'chunked','content-type':'application/json'}],
 ['/api/action','POST',{'content-length':'2','content-type':'text/plain'}],
]) assert.equal(send(url,method,headers),400);
assert.equal(accepted,1);
assert.throws(() => api.guardRequests(new EventEmitter(),false));
""")

    def test_ui_keeps_capability_in_memory_and_invalidates_on_workspace_switch(self):
        html = (RESOURCE / "index.html").read_text(encoding="utf-8")
        self.assertIn("onWorkspaceChange((uris) => bind(uris))", html)
        self.assertIn('const turn = ++generation;\n    hide("connecting")', html)
        self.assertIn("turn !== generation", html)
        self.assertIn('window.parent === window', html)
        for forbidden in ("localStorage", "sessionStorage", "agent.sendMessage", "innerHTML", "console.log", "http://translate"):
            self.assertNotIn(forbidden, html)
        self.assertIn('"zh-CN"', html)
        self.assertIn("confirm:true", html)

    def test_manifest_declares_auxiliary_pane_without_enabling_admin(self):
        manifest = json.loads((RESOURCE / "sidecar.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["has_web_ui"])
        self.assertEqual(manifest["ui_config"]["views"][0]["entrypoint"], "SIDECAR_UI_ENTRYPOINT_AUX_PANE")
        self.assertEqual(manifest["args"], ["main.mjs"])
        self.assertNotIn("admin", manifest)

    def test_frontend_discards_old_bootstrap_after_workspace_changes(self):
        self.run_node("""
const {readFileSync} = await import('node:fs');
const {runInNewContext} = await import('node:vm');
const html=readFileSync(new URL('./index.html',pathToFileURL(process.env.ALP_PANEL_MODULE)),'utf8');
const script=[...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)].at(-1)[1];
class Element {
  constructor(){this.dataset={};this.hidden=false;this.children=[];this.events={};this.value='';this.textContent='';}
  addEventListener(name,fn){this.events[name]=fn;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];}
}
const ids=['language','panel','notice','result','refresh','browser','review','frequencyTitle','shared','quota','historyTitle','historyNote','history','project','workspace','stats','admin','presets'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
const document={documentElement:{},getElementById:id=>elements[id],createElement:()=>new Element(),querySelectorAll:()=>[]};
let changed;const requests=[];
const window={parent:{},addEventListener(){},confirm:()=>true,sidecar:{conversationId:'native-chat',getWorkspaceUris:async()=>[],onWorkspaceChange:fn=>{changed=fn;},fetch:(_url,opts)=>new Promise(resolve=>requests.push({body:JSON.parse(opts.body),resolve}))}};
runInNewContext(script,{window,document,navigator:{language:'en'},crypto:globalThis.crypto,URL,console});
await new Promise(resolve=>setImmediate(resolve));
const uri=pathToFileURL(installation.receipt.workspace).href;
changed([uri]);assert.equal(elements.panel.hidden,true);assert.equal(requests.length,1);
changed([]);assert.equal(elements.panel.hidden,true);
requests[0].resolve({ok:true,json:async()=>({visible:true,capability:'obsolete-capability',summary:{project_name:'old'}})});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(elements.panel.hidden,true);assert.equal(elements.project.textContent,'');
changed([uri]);
requests[1].resolve({ok:true,json:async()=>({visible:true,capability:'new-capability',summary:{project_name:'current',counts:{active:2}}})});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(elements.panel.hidden,false);assert.equal(elements.project.textContent,'current');assert.match(elements.admin.textContent,/Project usage unavailable/);
elements.browser.events.click();assert.equal(requests[2].body.capability,'new-capability');
changed([]);assert.equal(elements.panel.hidden,true);
requests[2].resolve({ok:true,json:async()=>({visible:true,summary:{project_name:'stale action'}})});
await new Promise(resolve=>setImmediate(resolve));assert.equal(elements.panel.hidden,true);
changed([uri]);
requests[3].resolve({ok:true,json:async()=>({visible:true,capability:'history-capability',summary:{project_name:'current'}})});
await new Promise(resolve=>setImmediate(resolve));
elements.history.events.click();
assert.equal(requests[4].body.action,'archive_history');
assert.equal(requests[4].body.confirm,true);
requests[4].resolve({ok:true,json:async()=>({visible:true,summary:{project_name:'current'},history:{state:'paused',archived_cases:7}})});
await new Promise(resolve=>setImmediate(resolve));
assert.match(elements.result.textContent,/partially archived/);
assert.match(elements.result.textContent,/no review or publication/);
""")


if __name__ == "__main__":
    unittest.main()
