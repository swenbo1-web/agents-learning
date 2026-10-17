"""Optional host modules: actual exported JS, fake transports, no model calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


NODE = shutil.which("node")
HOST = Path(__file__).resolve().parents[1] / "host_resources/dsh/host"
PROBE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {writeFileSync,readFileSync} from 'node:fs';
const [root,area,scenario]=process.argv.slice(1);
const {extensionConfig,applyExtensions}=await import(pathToFileURL(join(root,'extensions.mjs')));
const baseline=globalThis.fetch;
const calls=[];
const fakeFetch=async(input,init)=>{calls.push({input,init});return {ok:true};};
globalThis.fetch=fakeFetch;
let initiator;
const sections=new Map(),listeners=new Map();
const ctx={baseUrl:pathToFileURL(area+'/profile/').href,
 get(key){return key==='systemPrompt'?{section(spec){sections.set(spec.name,spec);return()=>sections.delete(spec.name);}}
   :key==='agents'?{currentInitiator:()=>initiator}:undefined;},
 on(name,listener){listeners.set(name,listener);return()=>{if(listeners.get(name)===listener)listeners.delete(name);};},
};
const allowed=({agent,sessionId})=>agent?.session?.header?.cwd===area+'/project'||sessionId==='installed';
const url='https://vertex.invalid/v1/models/gemini-4-argon:streamGenerateContent?alt=sse';
const google={generationConfig:{thinkingConfig:{thinkingBudget:8192,includeThoughts:true}}};
async function send(provider,model,body,target=url,sessionId='installed',extra={}) {
 const options={provider,model,sessionId};
 const next=()=> (async function*(){
   await Promise.resolve();
   const init={method:'post',body:JSON.stringify(body),...extra};
   await globalThis.fetch(target,init);
   yield {done:true};
 })();
 const stream=listeners.get('llm/stream')?.(options,next)??next();
 for await(const _ of stream){}
 return calls.at(-1).init;
}
if(scenario==='defaults') {
 const stop=applyExtensions(ctx,extensionConfig(),allowed);
 assert.equal(sections.size,0);assert.equal(listeners.size,0);assert.equal(globalThis.fetch,fakeFetch);
 stop();
 for(const value of [null,[],{googleThinking:'true'},{surprise:true},{delegation:null},{delegation:{provider:'one'}},
   {delegation:{enabled:true,maxAgents:5}},{thinkingLanguage:'all'}])assert.throws(()=>extensionConfig(value));
} else if(scenario==='google') {
 const stop=applyExtensions(ctx,extensionConfig({googleThinking:true}),allowed);
 let init=await send('google-vertex','gemini-4-argon',google,new URL(url));
 assert.deepEqual(JSON.parse(init.body).generationConfig.thinkingConfig,{thinkingLevel:'MEDIUM',includeThoughts:true});
 assert.equal(google.generationConfig.thinkingConfig.thinkingBudget,8192);
 for(const [provider,model,target,session] of [
   ['other','gemini-4-argon',url,'installed'],['google','gemini-3.1-pro',url,'installed'],
   ['google','gemini-4-argon',url,'uninstalled'],
   ['google','gemini-4-argon',url.replace('gemini-4-argon','gemini-4-other'),'installed']]) {
   init=await send(provider,model,google,target,session);
   assert.equal(init.body,JSON.stringify(google));
 }
 init=await send('google','gemini-4-argon',{generationConfig:{thinkingConfig:{thinkingBudget:0,includeThoughts:true}}});
 assert.deepEqual(JSON.parse(init.body).generationConfig.thinkingConfig,{includeThoughts:true});
 const malformed={generationConfig:{thinkingConfig:{thinkingBudget:'0'}}};
 assert.equal((await send('google','gemini-4-argon',malformed)).body,JSON.stringify(malformed));
 stop();assert.equal(globalThis.fetch,fakeFetch);assert.equal(listeners.size,0);
} else if(scenario==='messages') {
 const stop=applyExtensions(ctx,extensionConfig({messagesCompat:true}),allowed);
 const body={messages:[{role:'assistant',content:[{type:'thinking',thinking:'private',signature:'keep'},
   {type:'tool_use',id:'id-a',name:'read',input:{}},{type:'text',text:''},{type:'text',text:'meaningful'}]},
   {role:'user',content:[{type:'tool_result',tool_use_id:'id-a',content:'done'}]}]};
 const init=await send('deepseek-account','model',body,'https://deepseek.invalid/messages');
 const expected=structuredClone(body);expected.messages[0].content.splice(2,1);
 assert.deepEqual(JSON.parse(init.body),expected);
 assert.equal((await send('anthropic','model',body,'https://anthropic.invalid/messages')).body,JSON.stringify(body));
 assert.equal((await send('deepseek-account','model',body,'https://deepseek.invalid/messages','other')).body,JSON.stringify(body));
 stop();assert.equal(globalThis.fetch,fakeFetch);
} else if(scenario==='concurrency') {
 const stop=applyExtensions(ctx,extensionConfig({googleThinking:true,messagesCompat:true}),allowed);
 await Promise.all([send('google','gemini-4-argon',google),send('google','gemini-4-argon',google,url,'outside'),
   send('other','gemini-4-argon',google)]);
 assert.equal(calls.filter(c=>JSON.parse(c.init.body).generationConfig.thinkingConfig.thinkingLevel==='MEDIUM').length,1);
 await globalThis.fetch(url,{method:'POST',body:JSON.stringify(google)});
 assert.equal(calls.at(-1).init.body,JSON.stringify(google));
 stop();assert.equal(globalThis.fetch,fakeFetch);
} else if(scenario==='delegation') {
 const config=extensionConfig({delegation:{enabled:true,maxAgents:2},thinkingLanguage:'zh-CN'});
 const stop=applyExtensions(ctx,config,allowed);
 const agent={session:{header:{cwd:area+'/project'},requestContext:()=>({provider:'deepseek-account',model:'choice'})}};
 const section=sections.get('agent-learning-delegation');
 assert.ok(section.text({agent}).includes('继承当前模型'));assert.ok(!section.text({agent}).includes('deepseek-flash'));
 assert.equal(section.text({}), '');
 const language=sections.get('agent-learning-thinking-language');assert.ok(language.text({agent}).includes('简体中文'));
 agent.session.requestContext=()=>({provider:'google',model:'gemini-4-argon'});
 assert.equal(language.text({agent}), '');
 stop();assert.equal(sections.size,0);
 const fixed=applyExtensions(ctx,extensionConfig({delegation:{enabled:true,provider:'custom',model:'owned'}}),allowed);
 agent.session.requestContext=()=>({provider:'custom',model:'owned'});
 assert.equal(sections.get('agent-learning-delegation').text({agent}),'');fixed();
} else if(scenario==='net') {
 globalThis.__fakeDispatcher={dispatch(options,handler){globalThis.__dispatchOptions=options;return true;}};
 const stop=applyExtensions(ctx,extensionConfig({netTune:true}),allowed);
 const init=await send('google','gemini-4-argon',google);
 assert.ok(init.dispatcher);init.dispatcher.dispatch({path:'/keep',bodyTimeout:100},{});
 assert.deepEqual(globalThis.__dispatchOptions,{path:'/keep',bodyTimeout:1800000,headersTimeout:600000});
 assert.equal(globalThis.__setDispatcherCalls,undefined);
 const custom={dispatch(){}};
 assert.equal((await send('google','gemini-4-argon',google,url,'installed',{dispatcher:custom})).dispatcher,custom);
 assert.equal((await send('google','gemini-4-argon',google,url,'outside')).dispatcher,undefined);
 assert.equal((await send('google','gemini-4-argon',google,'https://example.invalid/telemetry')).dispatcher,undefined);
 stop();assert.equal(globalThis.fetch,fakeFetch);assert.equal(globalThis.__setDispatcherCalls,undefined);
} else if(scenario==='disposal') {
 const {registerFetchTransform}=await import(pathToFileURL(join(root,'fetch-compat.mjs')));
 let transformed=0;
 const first=registerFetchTransform((_input,init)=>{transformed++;return init;});
 const second=registerFetchTransform((_input,init)=>init);
 first();await globalThis.fetch(url,{});assert.equal(transformed,0);
 second();assert.equal(globalThis.fetch,fakeFetch);
 const stale=registerFetchTransform((_input,init)=>init);
 const owned=globalThis.fetch;
 const external=(...args)=>owned(...args);globalThis.fetch=external;
 stale();
 const fresh=registerFetchTransform((_input,init)=>{transformed++;return init;});
 await globalThis.fetch(url,{});assert.equal(transformed,1);
 fresh();assert.equal(globalThis.fetch,external);globalThis.fetch=fakeFetch;
} else if(scenario==='main') {
 const {apply}=await import(pathToFileURL(join(root,'index.mjs')));
 const effects=[];
 ctx.tools={register(){}};ctx.effect=fn=>{const d=fn();if(typeof d==='function')effects.push(d);};
 ctx.inject=()=>{};
 apply(ctx,{extensions:{googleThinking:true,delegation:{enabled:true},thinkingLanguage:'zh-CN'}});
 const section=sections.get('agent-learning-delegation');
 const agent=cwd=>({id:'installed',session:{header:{cwd},requestContext:()=>({provider:'google',model:'gemini-4-argon'})}});
 initiator=agent(join(area,'project'));
 assert.ok(section.text({agent:initiator}));
 assert.ok(JSON.parse((await send('google','gemini-4-argon',google)).body).generationConfig.thinkingConfig.thinkingLevel);
 for(const cwd of [join(area,'outside'),join(area,'project','nested')]) {
   initiator=agent(cwd);assert.equal(section.text({agent:initiator}),'');
   assert.equal((await send('google','gemini-4-argon',google)).body,JSON.stringify(google));
 }
 while(effects.length)effects.pop()();
 assert.equal(globalThis.fetch,fakeFetch);assert.equal(sections.size,0);
} else throw new Error('unknown scenario');
globalThis.fetch=baseline;
console.log(JSON.stringify({ok:true,scenario}));
"""


@unittest.skipUnless(NODE, "Node.js required for DSH host extension checks")
class DshExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-ext-")
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name)
        for name in ("project/nested/.git", "outside", "plugin/host", "profile/node_modules/undici"):
            (self.area / name).mkdir(parents=True)
        shutil.copytree(HOST, self.area / "plugin/host", dirs_exist_ok=True)
        (self.area / "plugin/runtime-release.json").write_text(json.dumps({"workspace": str(self.area / "project")}), encoding="utf-8")
        package = self.area / "profile/node_modules/undici"
        (package / "package.json").write_text('{"main":"index.cjs"}', encoding="utf-8")
        (package / "index.cjs").write_text(
            "exports.getGlobalDispatcher=()=>globalThis.__fakeDispatcher;"
            "exports.setGlobalDispatcher=()=>{globalThis.__setDispatcherCalls=true;};", encoding="utf-8")

    def run_probe(self, scenario):
        result = subprocess.run([NODE, "--input-type=module", "-e", PROBE,
                                 str(self.area / "plugin/host"), str(self.area), scenario],
                                cwd=self.area, env={**os.environ}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_defaults_and_invalid_configuration_have_no_host_side_effects(self):
        self.run_probe("defaults")

    def test_google_patch_checks_project_route_wire_model_and_explicit_fields(self):
        self.run_probe("google")

    def test_messages_preserve_meaningful_content_and_tool_order(self):
        self.run_probe("messages")

    def test_concurrent_projects_and_agentless_fetches_do_not_share_request_scope(self):
        self.run_probe("concurrency")

    def test_delegation_is_optional_project_scoped_and_has_no_fixed_vendor(self):
        self.run_probe("delegation")

    def test_transport_delegates_proxy_tls_and_explicit_dispatcher_without_global_changes(self):
        self.run_probe("net")

    def test_disposal_is_order_independent_and_preserves_foreign_fetch_wrappers(self):
        self.run_probe("disposal")

    def test_standard_host_entry_loads_optional_modules_only_for_installed_checkout(self):
        self.run_probe("main")
