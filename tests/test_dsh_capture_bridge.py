"""Execute the native DSH event bridge against deterministic SDK-shaped events."""
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
const calls = [];
childProcess.spawn = (_python, argv, options) => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  let input = '';
  child.stdin = {write(value) { input += value; }, end() {
    const complete = () => {
      const event = argv[argv.indexOf('--event') + 1];
      const payload = JSON.parse(input);
      calls.push({event, payload, workspace: options.cwd});
      const result = event === 'UserPromptSubmit'
        ? {hookSpecificOutput:{additionalContext:'context_handle=synthetic-handle; matched project knowledge'}}
        : event === 'SessionStart' ? {hookSpecificOutput:{additionalContext:'context_handle=old-startup; initial context'}} : {};
      child.stdout.emit('data', Buffer.from(JSON.stringify(result)));
      child.emit('close', 0);
    };
    if (argv[argv.indexOf('--event')+1] === 'SessionStart') setTimeout(complete,20);
    else queueMicrotask(complete);
  }};
  child.kill = () => {};
  return child;
};
syncBuiltinESMExports();
const handlers = new Map();
const systemSections = [];
const {apply} = await import(pathToFileURL(process.argv[1]).href);
apply({tools:{register() {}}, effect() {}, inject() {},
  on(name, callback) {handlers.set(name, callback);},
  systemPrompt:{context(spec) {systemSections.push(spec);}},
});
const workspace = process.argv[3];
const session = (id, header = {}) => {
  const result = {id, header:{cwd:workspace, ...header}};
  handlers.get('session/created')(result);
  return result;
};
const user = (id, text, source = {kind:'user'}) => ({
  id, role:'user', content:[{type:'text',text}], source,
});
const step = async (owner, turn, number, messages, options = {}) => {
  let nextCalls = 0;
  const result = await handlers.get('agent/pre-step')({agent:{id:owner.id},turn,
    step:number,messages,signal:{aborted:options.aborted === true}}, async () => {
    nextCalls += 1;
    return options.reject ? {kind:'reject'} : {kind:'enter',messages:options.replacement ?? messages};
  });
  assert.equal(nextCalls, 1);
  return result;
};
const emit = (owner, type, data) => handlers.get('session/event')(owner,{type,data});
const reply = (owner, turn, number, content, extra = {}) => emit(owner,'assistant/message',{
  turn,step:number,message:{id:'answer-'+turn+'-'+number,role:'assistant',source:{kind:'model'},content},...extra,
});
const end = (owner, turn, kind = 'completed') => emit(owner,'turn/end',{turn,reason:{kind}});
const text = value => [{type:'text',text:value}];
const scenario = process.argv[2];
const owner = session('session-a');

if (scenario === 'startup-race') {
  const startup = handlers.get('agent/created')({agent:{id:owner.id}});
  const decision = await step(owner,1,1,[user('u','current requirement')]);
  await startup;
  assert.deepEqual(calls.map(call=>call.event),['SessionStart','UserPromptSubmit']);
  assert.match(decision.messages.at(-1).content[0].text,/matched project knowledge/);
  assert.equal(systemSections[0].text({scope:{id:owner.id}}),'');
} else if (scenario === 'current-step') {
  const original = user('human-a','Explain the project design');
  const decision = await step(owner,1,1,[original]);
  assert.equal(calls.length,1);
  assert.equal(calls[0].event,'UserPromptSubmit');
  assert.equal(calls[0].payload.prompt,original.content[0].text);
  assert.deepEqual(calls[0].payload.message_ids,['human-a']);
  assert.equal(calls[0].payload.step,1);
  assert.equal(decision.messages[0],original);
  assert.equal(decision.messages.length,2);
  const recall = decision.messages[1];
  assert.equal(recall.source.kind,'plugin:agent-learning'); assert.equal(recall.source.form,'recall');
  assert.equal('plugin' in recall.source, false);
  assert.match(recall.id,/^[0-9a-f-]{36}$/);
  assert.match(recall.content[0].text,/matched project knowledge/);
  assert.ok(Object.isFrozen(recall));
  assert.equal(systemSections[0].text({scope:{id:owner.id}}),'');
} else if (scenario === 'source-dedup') {
  const messages = [user('p','synthetic plugin',{kind:'plugin:other'}),
    user('t','tool result',{kind:'tool',callId:'tool'}),
    {...user('missing','missing provenance'),source:undefined},
    {...user('assistant','assistant text'),role:'assistant'},user('u','first requirement')];
  const first = await step(owner,2,1,messages);
  await step(owner,2,2,[...first.messages,user('v','new steering input')]);
  await step(owner,2,3,[...first.messages,user('v','new steering input')]);
  assert.equal(calls.length,2);
  assert.equal(calls[0].payload.prompt,'first requirement');
  assert.equal(calls[1].payload.prompt,'new steering input');
  assert.deepEqual(calls[1].payload.message_ids,['v']);
  await step(owner,3,7,[user('new-id','first requirement')]);
  assert.equal(calls.length,3,'same text with a fresh human id is a new input');
} else if (scenario === 'accepted-messages') {
  const original = user('original','discarded');
  const replacement = user('accepted','accepted human requirement');
  const decision = await step(owner,1,4,[original],{replacement:[replacement]});
  assert.equal(calls[0].payload.prompt,'accepted human requirement');
  assert.equal(decision.messages[0],replacement);
  await step(owner,2,1,[original],{reject:true});
  await step(owner,3,1,[original],{aborted:true});
  assert.equal(calls.length,1,'rejected or canceled steps must not record prompts');
} else if (scenario === 'batch') {
  await step(owner,1,1,[user('one','first'),user('one','first'),user('two','second')]);
  assert.equal(calls.length,1);
  assert.equal(calls[0].payload.prompt,'first\nsecond');
  assert.deepEqual(calls[0].payload.message_ids,['one','two']);
  assert.equal(calls[0].payload.prompt_truncated,false);
  assert.equal(calls[0].payload.prompt_chars,12);
} else if (scenario === 'public-final') {
  await reply(owner,1,1,text('progress report'));
  await reply(owner,1,2,[{type:'reasoning',text:'PRIVATE_REASONING'},
    ...text('public final analysis'),{type:'thinking',text:'PRIVATE_THINKING'},
    {type:'tool-call',arguments:'PRIVATE_TOOL_ARGS'}]);
  await end(owner,1);
  assert.equal(calls.length,1);
  assert.equal(calls[0].event,'Stop');
  assert.equal(calls[0].payload.last_assistant_message,'public final analysis');
  assert.equal(calls[0].payload.last_assistant_message_chars,21);
  assert.equal(calls[0].payload.last_assistant_message_truncated,false);
  assert.ok(!JSON.stringify(calls).includes('PRIVATE_'));
  await end(owner,2);
  assert.equal(calls[1].payload.last_assistant_message,undefined,'never reuse prior-turn text');
} else if (scenario === 'session-turn-isolation') {
  const other = session('session-b');
  const child = session('child',{parentSession:'session-a',origin:'subagent'});
  await reply(owner,10,1,text('a10')); await reply(owner,11,2,text('a11'));
  await reply(other,10,1,text('b10')); await reply(child,10,1,text('child10'));
  await end(other,10); await end(owner,11); await end(owner,10); await end(child,10);
  assert.deepEqual(calls.map(call=>call.payload.last_assistant_message),['b10','a11','a10','child10']);
  assert.equal(calls[3].payload.sessionId,'session-a');
  assert.equal(calls[3].payload.agentId,'child');
} else if (scenario === 'partial') {
  const reasons = ['aborted','interrupted','error','blocked','max-tokens','future-kind'];
  for (let i=0;i<reasons.length;i++) {
    await reply(owner,i,1,text('partial output'));
    await end(owner,i,reasons[i]);
  }
  await reply(owner,10,1,text('flagged incomplete'),{interrupted:true});
  await end(owner,10,'completed');
  assert.equal(calls.length,7);
  for (const call of calls) {
    assert.equal(call.event,'Interrupt');
    assert.equal(call.payload.last_assistant_message,undefined);
  }
} else if (scenario === 'stale-and-empty') {
  await reply(owner,1,4,text('newest final'));
  await reply(owner,1,3,text('late older step'));
  await end(owner,1); await end(owner,1);
  await reply(owner,1,5,text('late completed reply')); await end(owner,1);
  assert.equal(calls.length,1);
  assert.equal(calls[0].payload.last_assistant_message,'newest final');
  await reply(owner,2,1,text('progress only')); await reply(owner,2,2,[]); await end(owner,2);
  assert.equal(calls[1].payload.last_assistant_message,undefined);
} else if (scenario === 'bounded') {
  await step(owner,1,1,[user('long','x'.repeat(70000))]);
  assert.equal(calls[0].payload.prompt.length,65536);
  assert.equal(calls[0].payload.prompt_chars,70000);
  assert.equal(calls[0].payload.prompt_truncated,true);
  await reply(owner,1,1,text('y'.repeat(70000))); await end(owner,1);
  assert.equal(calls[1].payload.last_assistant_message.length,65536);
  assert.equal(calls[1].payload.last_assistant_message_chars,70000);
  assert.equal(calls[1].payload.last_assistant_message_truncated,true);
  await reply(owner,2,1,text('evicted reply'));
  for (let turn=3;turn<=131;turn++) await reply(owner,turn,1,text('bounded reply '+turn));
  await end(owner,2);
  assert.equal(calls[2].payload.last_assistant_message,undefined);
} else if (scenario === 'workspace-boundary') {
  const outside = session('outside',{cwd:workspace+'-other'});
  const decision = await step(outside,1,1,[user('u','outside project')]);
  assert.equal(calls.length,0);
  assert.equal(decision.messages.length,1);
  await reply(outside,1,1,text('outside final')); await end(outside,1);
  assert.equal(calls.length,0);
} else {
  throw new Error('unknown scenario');
}
console.log(JSON.stringify({scenario,ok:true,calls:calls.length}));
"""


@unittest.skipUnless(NODE, "Node.js is required for DSH capture bridge execution")
class DshCaptureBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-capture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.plugin = self.root / "plugin"
        (self.plugin / "host").mkdir(parents=True)
        self.bridge = self.plugin / "host/index.mjs"
        shutil.copytree(BRIDGE.parent, self.bridge.parent, dirs_exist_ok=True)
        (self.plugin / "runtime-release.json").write_text(json.dumps({
            "python": sys.executable, "runtime_directory": str(self.root / "runtime"),
            "workspace": str(self.workspace), "home": str(self.root / "home"), "hook_mode": "observe",
        }), encoding="utf-8")

    def run_scenario(self, scenario):
        response = subprocess.run(
            [NODE, "--input-type=module", "-e", PROBE, str(self.bridge), scenario, str(self.workspace)],
            cwd=self.workspace, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, timeout=15,
        )
        self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
        self.assertTrue(json.loads(response.stdout.decode("utf-8"))["ok"])

    def test_first_step_retrieval_is_in_current_accepted_messages(self):
        self.run_scenario("current-step")

    def test_pending_startup_cannot_overwrite_current_prompt_retrieval(self):
        self.run_scenario("startup-race")

    def test_only_new_human_sources_trigger_capture_in_any_step(self):
        self.run_scenario("source-dedup")

    def test_downstream_rejections_and_replacements_are_preserved(self):
        self.run_scenario("accepted-messages")

    def test_message_batch_is_bounded_and_deduplicated(self):
        self.run_scenario("batch")

    def test_complete_turn_only_captures_final_public_text(self):
        self.run_scenario("public-final")

    def test_session_turn_and_subagent_replies_stay_separate(self):
        self.run_scenario("session-turn-isolation")

    def test_incomplete_turns_and_prefixes_never_become_completed_delivery(self):
        self.run_scenario("partial")

    def test_duplicate_late_and_empty_messages_never_reuse_old_delivery(self):
        self.run_scenario("stale-and-empty")

    def test_large_messages_and_active_turn_cache_have_limits(self):
        self.run_scenario("bounded")

    def test_uninstalled_project_never_dispatches_capture_or_retrieval(self):
        self.run_scenario("workspace-boundary")


if __name__ == "__main__":
    unittest.main()
