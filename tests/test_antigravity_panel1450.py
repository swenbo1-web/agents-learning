"""Registration changes and delayed AG panel responses stay inside their scope."""
import json
import shutil
import unittest
from unittest.mock import patch

from tests import test_antigravity_panel as backend
from tests import test_antigravity_panel_recovery as frontend


SCENARIOS = r'''
 if(scenario==='new_registration_manual_read'){
   await flush();reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   assert.equal(elements.panel.hidden,true);assert.equal(elements.reconnect.hidden,false);
   const req=await reconnect();reply(req,valid('registered-now'));await flush();
   assert.equal(elements.panel.hidden,false);assert.equal(elements.project.textContent,'current');
   assert(requests.every(r=>r.url==='/api/bootstrap'),'a read retry sent an action');
 }else if(scenario==='new_registration_encoded_drive'){
   await flush();assert.deepEqual(requests[0].body.workspace_uris,['file:///d%3A/Projects/visual%20agent']);
   reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   const req=await reconnect();assert.deepEqual(req.body.workspace_uris,['file:///d%3A/Projects/visual%20agent']);
   reply(req,valid('encoded-project'));await flush();assert.equal(elements.panel.hidden,false);
   assert(requests.every(r=>!r.body.action));
 }else if(scenario==='new_registration_activation'){
   let now=10000;context.performance={now:()=>now};
   await flush();reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   await pageEvent('focus');await pageEvent('focus');
   assert.equal(requests.length,2,'activation must perform one bounded scope read');
   reply(requests[1],{visible:false,reason:'workspace_not_bound'});await flush();
   await pageEvent('focus');assert.equal(requests.length,2,'repeated activation exceeded cooldown');
   now+=2500;await pageEvent('focus');assert.equal(requests.length,3);
   reply(requests[2],valid('registered-now'));await flush();assert.equal(elements.panel.hidden,false);
   now+=2500;await pageEvent('focus');assert.equal(requests.length,3,'a healthy panel was polled');
   assert(requests.every(r=>r.url==='/api/bootstrap'));
 }else if(scenario==='new_registration_scope_switch'){
   await flush();reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   const old=await reconnect();currentUris=[otherUri];window.sidecar.conversationId='other-native';changed(currentUris);await flush();
   const next=requests.at(-1);assert.notEqual(old,next);reply(old,valid('obsolete-cap'));await flush();
   assert.equal(elements.panel.hidden,true,'old registration restored previous controls');
   reply(next,{visible:false,reason:'workspace_not_bound'});await flush();assert.equal(elements.panel.hidden,true);
   const current=await reconnect();assert.equal(current.body.conversation_id,'other-native');
   assert.deepEqual(current.body.workspace_uris,[otherUri]);reply(current,valid('other-cap',snapshot('other','p2')));
   await flush();assert.equal(elements.project.textContent,'other');assert(requests.every(r=>!r.body.action));
 }else if(scenario==='new_registration_ambiguous_context'){
   await flush();reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   currentUris=[uri,otherUri];const before=requests.length;await click('reconnect');
   assert.equal(requests.length,before);assert.equal(elements.panel.hidden,true);
   await pageEvent('focus');assert.equal(requests.length,before);
 }else if(scenario==='new_registration_during_action'){
   context.performance={now:()=>10000};await boot();await click('history');
   const old=requests.at(-1);await pageEvent('focus');assert.equal(requests.length,2,'activation replayed a pending action');
   old.reject(new Error('uncertain historical archive'));await flush();
   await pageEvent('focus');assert.equal(requests.at(-1).url,'/api/bootstrap');
   reply(requests.at(-1),valid('after-uncertain'));await flush();
   assert.equal(requests.filter(r=>r.body.action==='archive_history').length,1);
 }else if(scenario==='new_registration_hidden_page'){
   context.performance={now:()=>10000};document.visibilityState='hidden';
   await flush();reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   await pageEvent('focus');assert.equal(requests.length,1,'a hidden page retried registration');
   document.visibilityState='visible';await pageEvent('focus');assert.equal(requests.length,2);
   reply(requests[1],valid('visible-project'));await flush();assert.equal(elements.panel.hidden,false);
 }else if(scenario.startsWith('new_registration_invalid_')){
   await flush();reply(requests[0],{visible:false,reason:'workspace_not_bound'});await flush();
   const values={remote:'file://untrusted-host/share',http:'https://example.invalid/project',query:uri+'?other=1',fragment:uri+'#other'};
   currentUris=[values[scenario.slice('new_registration_invalid_'.length)]];
   await click('reconnect');assert.equal(requests.length,1);assert.equal(elements.panel.hidden,true);
 }else
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for isolated AG panel checks")
class PanelRegistrationRecoveryTests(unittest.TestCase):
    setUp = frontend.AntigravityPanelRecoveryTests.setUp

    def scenario(self, name):
        harness = frontend.HARNESS.replace(" if(scenario==='initial_uninstalled'){", SCENARIOS + " if(scenario==='initial_uninstalled'){", 1)
        harness = harness.replace(" runInNewContext(script,context);", " if(scenario==='new_registration_encoded_drive')currentUris=['file:///d%3A/Projects/visual%20agent'];\n runInNewContext(script,context);", 1)
        self.assertNotEqual(harness, frontend.HARNESS)
        with patch.object(frontend, "HARNESS", harness):
            frontend.AntigravityPanelRecoveryTests.scenario(self, name)

    def test_manual_read_recovers_project_registered_after_initial_bootstrap(self):
        self.scenario("new_registration_manual_read")

    def test_native_percent_encoded_windows_drive_uri_survives_reconnect(self):
        self.scenario("new_registration_encoded_drive")

    def test_failed_registration_read_refreshes_on_activation_without_polling(self):
        self.scenario("new_registration_activation")

    def test_registration_recovery_discards_late_old_scope_response(self):
        self.scenario("new_registration_scope_switch")

    def test_read_retry_cannot_guess_an_ambiguous_project(self):
        self.scenario("new_registration_ambiguous_context")

    def test_activation_never_replays_an_uncertain_mutation(self):
        self.scenario("new_registration_during_action")

    def test_hidden_page_does_not_auto_refresh_a_registration(self):
        self.scenario("new_registration_hidden_page")

    def test_registration_retry_rejects_remote_and_decorated_contexts(self):
        for suffix in ("remote", "http", "query", "fragment"):
            with self.subTest(suffix=suffix):
                self.scenario("new_registration_invalid_" + suffix)


FAKE = r'''
const {readFileSync}=await import('node:fs');
const registryPath=join(process.env.ALP_PANEL_REGISTRY,'ui-registry.json');
const registry=api.loadRegistry(process.env.ALP_PANEL_REGISTRY);
let pending=null,spawned=0,killed=0;
function spawnFake(){
  const child=new EventEmitter();child.exitCode=null;spawned++;
  child.stdout=new EventEmitter();child.stdout.setEncoding=()=>{};child.stderr=new EventEmitter();
  child.stdin={write(line,done){const body=JSON.parse(line);const finish=()=>child.stdout.emit('data',JSON.stringify({id:body.id,result:{visible:true,capability:'initial-cap',summary:{workspace:body.params.workspace}}})+'\n');
    if(body.method===DELAY_METHOD)pending=finish;else queueMicrotask(finish);done();},end(){}};
  child.kill=()=>{killed++;};return child;
}
const router=api.createRegistryController(process.env.ALP_PANEL_REGISTRY,spawnFake);
const base={workspace_uris:[pathToFileURL(installation.receipt.workspace).href],conversation_id:'current-chat'};
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for isolated AG panel checks")
class PanelRegistryCompletionTests(unittest.TestCase):
    setUp = backend.AntigravityPanelTests.setUp
    run_node = backend.AntigravityPanelTests.run_node

    def test_workspace_resolution_accepts_native_encoded_drive_and_spaces(self):
        self.run_node(r'''
const standard=pathToFileURL(installation.receipt.workspace).href;
const encoded=standard.replace(/^file:\/\/\/([A-Za-z]):/,(_,drive)=>'file:///'+drive.toLowerCase()+'%3A');
const resolved=api.resolveWorkspace([encoded],installation);assert(resolved);
const key=value=>process.platform==='win32'?value.toLowerCase():value;
assert.equal(key(resolved),key(installation.receipt.workspace));
''')

    def test_new_registration_is_visible_to_an_existing_controller(self):
        self.run_node(FAKE.replace("DELAY_METHOD", "'never'") + r'''
writeFileSync(registryPath,JSON.stringify({...registry,projects:[]}));
assert.equal((await router.handle('bootstrap',base)).visible,false);assert.equal(spawned,0);
writeFileSync(registryPath,JSON.stringify(registry));
assert.equal((await router.handle('bootstrap',base)).visible,true);assert.equal(spawned,1);router.close();
''')

    def test_removed_registration_rejects_delayed_bootstrap_and_action(self):
        registry_path = self.global_ui / "ui-registry.json"
        original = registry_path.read_bytes()
        for method in ("bootstrap", "action"):
            with self.subTest(method=method):
                registry_path.write_bytes(original)
                setup = FAKE.replace("DELAY_METHOD", json.dumps(method))
                call = "base" if method == "bootstrap" else "{...base,capability:'initial-cap',action:'debug'}"
                if method == "action":
                    setup += "assert.equal((await router.handle('bootstrap',base)).visible,true);\n"
                self.run_node(setup + f"const response=router.handle('{method}',{call}).then(result=>({{result}}),error=>({{error}}));\n" + r'''
assert(pending);writeFileSync(registryPath,JSON.stringify({...registry,projects:[]}));pending();
const {result,error}=await response;assert(error||result.visible===false,'removed project displayed a late result');
assert.equal(killed,1);router.close();
''')

    def test_rebound_installation_rejects_late_bootstrap_with_old_capability(self):
        self.run_node(FAKE.replace("DELAY_METHOD", "'bootstrap'") + r'''
const {mkdirSync}=await import('node:fs');const {createHash}=await import('node:crypto');
const response=router.handle('bootstrap',base).then(result=>({result}),error=>({error}));
const otherPlugin=join(process.env.ALP_PANEL_REGISTRY,'replacement-plugin');mkdirSync(otherPlugin);
const raw=JSON.stringify({...installation.receipt,release_id:'replacement'});writeFileSync(join(otherPlugin,'runtime-release.json'),raw);
registry.projects[0]={...registry.projects[0],installation:otherPlugin,receipt_sha256:createHash('sha256').update(raw).digest('hex')};
writeFileSync(registryPath,JSON.stringify(registry));pending();
const {result,error}=await response;assert(error||result.visible===false,'rebound project published an old capability');
assert.equal(killed,1);router.close();
''')

    def test_unrelated_registration_change_keeps_the_original_scope(self):
        self.run_node(FAKE.replace("DELAY_METHOD", "'bootstrap'") + r'''
const {mkdirSync}=await import('node:fs');const {createHash}=await import('node:crypto');
const response=router.handle('bootstrap',base);
const otherPlugin=join(process.env.ALP_PANEL_REGISTRY,'unrelated-plugin'),workspace=join(process.env.ALP_PANEL_REGISTRY,'unrelated-workspace');
mkdirSync(otherPlugin);mkdirSync(workspace);
const raw=JSON.stringify({...installation.receipt,workspace});writeFileSync(join(otherPlugin,'runtime-release.json'),raw);
registry.projects.push({...registry.projects[0],installation:otherPlugin,workspace,receipt_sha256:createHash('sha256').update(raw).digest('hex')});
writeFileSync(registryPath,JSON.stringify(registry));pending();
assert.equal((await response).visible,true);assert.equal(killed,0);router.close();
''')

    def test_malformed_registry_rejects_a_completed_but_stale_bootstrap(self):
        self.run_node(FAKE.replace("DELAY_METHOD", "'bootstrap'") + r'''
const response=router.handle('bootstrap',base).then(result=>({result}),error=>({error}));
writeFileSync(registryPath,'{"schema_version":');pending();
const {result,error}=await response;assert(error);assert.equal(result,undefined);assert.equal(killed,1);router.close();
''')

    def test_core_update_during_an_action_rejects_its_old_display_state(self):
        self.run_node(FAKE.replace("DELAY_METHOD", "'action'") + r'''
assert.equal((await router.handle('bootstrap',base)).visible,true);
const response=router.handle('action',{...base,capability:'initial-cap',action:'debug'}).then(result=>({result}),error=>({error}));
writeFileSync(join(installation.directory,'runtime-release.json'),JSON.stringify({...installation.receipt,release_id:'new-version'}));pending();
const {result,error}=await response;assert(error);assert.equal(result,undefined);assert.equal(killed,1);router.close();
''')


if __name__ == "__main__":
    unittest.main()
