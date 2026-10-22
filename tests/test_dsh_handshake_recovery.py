"""DSH trajectory recovery without real hooks, model calls or user databases."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_learning.dsh import DshAdapter, reset_session
from agent_learning.runtime import ContractError, Runtime


ROOT = Path(__file__).resolve().parents[1]
LOCAL = Path(os.environ.get("ALP_TEST_ROOT", ROOT / "build/local/dsh150-tests"))
NODE = shutil.which("node")
BRIDGE = ROOT / "host_resources/dsh/host/index.mjs"


PROBE = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
import childProcess from 'node:child_process';
import {pathToFileURL} from 'node:url';

const scenario = process.argv[2];
const workspace = process.argv[3];
const otherWorkspace = process.argv[4];
const calls = [], bindings = new Map();
let generation = 1, epoch = 1, minted = 0, starts = 0;
let dropped = false, activeStarts = 0, maximumActiveStarts = 0;
let toolSignal;

function deny(event, code) {
  return {decision:'deny', reason:'Agent Learning: '+code, dsh_reconnect_required:true,
    hookSpecificOutput:{hookEventName:event,permissionDecision:'deny',
      permissionDecisionReason:'Agent Learning: '+code}};
}
childProcess.spawn = (_python, argv, options) => {
  assert.ok(argv.includes('--event'), 'all spawned processes must be synthetic hook calls');
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  let input = '';
  child.stdin = {write(value) { input += value; }, end() {
    const event = argv[argv.indexOf('--event')+1];
    const payload = JSON.parse(input);
    calls.push({event,payload,cwd:options.cwd});
    const isStart = event === 'SessionStart' || event === 'SubagentStart';
    if (isStart) {
      starts += 1; activeStarts += 1;
      maximumActiveStarts = Math.max(maximumActiveStarts, activeStarts);
    }
    const complete = () => {
      if (isStart) activeStarts -= 1;
      assert.match(payload.dsh_bridge_instance,/^[0-9a-f-]{36}$/);
      const instance = payload.dsh_bridge_instance;
      let binding = bindings.get(instance);
      let result;
      if (binding && (binding.generation !== generation || binding.epoch !== epoch)) {
        result = deny(event,'dsh_trajectory_reconnect_required');
      } else if (!binding && !isStart) {
        result = deny(event,'dsh_trajectory_handshake_required');
      } else {
        if (!binding) {
          minted += 1;
          binding = {generation,epoch,token:minted.toString(16).padStart(32,'0'),
            sessionId:payload.sessionId,agentId:payload.agentId||null,cwd:payload.cwd};
          bindings.set(instance,binding);
        }
        assert.equal(payload.sessionId,binding.sessionId);
        assert.equal(payload.agentId||null,binding.agentId);
        assert.equal(payload.cwd,binding.cwd);
        const expected = {generation:binding.generation,token:binding.token};
        if (!isStart && !payload.dsh_trajectory) result = deny(event,'dsh_trajectory_token_required');
        else if (payload.dsh_trajectory && JSON.stringify(payload.dsh_trajectory) !== JSON.stringify(expected)) {
          result = deny(event,'dsh_trajectory_reconnect_required');
        } else {
          result = {dsh_trajectory:expected};
          if (isStart || event === 'UserPromptSubmit') {
            result.hookSpecificOutput = {additionalContext:
              'context_handle=synthetic-'+instance+'; matched project knowledge'};
          }
          if (event === 'UserPromptSubmit' && scenario === 'successful-warning') {
            result.systemMessage = 'Agent Learning: event_recorded; admin_schedule_failed';
          }
        }
      }
      if (isStart && (scenario === 'lost-start-reply' || scenario === 'terminated-start') && !dropped) {
        dropped = true; child.emit('close',scenario === 'terminated-start' ? null : 1); return;
      }
      if (isStart && scenario === 'handle-without-token' && !dropped) {
        dropped = true; delete result.dsh_trajectory;
      }
      if (isStart && scenario === 'recovery-fails') {
        child.emit('close',1); return;
      }
      if (isStart && scenario === 'prompt-recovery-retry' && !dropped) {
        dropped = true; child.emit('close',1); return;
      }
      if (event === 'PreToolUse' && scenario === 'observe-runtime-failure') {
        child.emit('close',1); return;
      }
      child.stdout.emit('data',Buffer.from(JSON.stringify(result)));
      child.emit('close',0);
    };
    if (event === 'PreToolUse' && scenario === 'disposed-inflight') handlers.get('session/disposed')(owner);
    if (event === 'PreToolUse' && ['canceled-inflight','canceled-error','canceled-nonzero'].includes(scenario)) {
      toolSignal.aborted = true;
      if (scenario === 'canceled-error') { child.emit('error',new Error('AbortError')); return; }
      if (scenario === 'canceled-nonzero') { child.emit('close',1); return; }
    }
    if (event === 'PreToolUse' && scenario === 'hot-reload-inflight') globalThis.__ALP_DSH_DISPOSE__();
    if (isStart) setTimeout(complete,15); else queueMicrotask(complete);
  }};
  child.kill = () => child.emit('close',null);
  return child;
};
syncBuiltinESMExports();
const handlers = new Map();
const {apply} = await import(pathToFileURL(process.argv[1]).href);
apply({tools:{register() {}},effect() {},inject() {},
  on(name,callback) {handlers.set(name,callback);},
  systemPrompt:{context() {}},
});
const session = (id,header={}) => {
  const value = {id,header:{cwd:workspace,...header}};
  handlers.get('session/created')(value); return value;
};
const owner = session('owner');
const startup = value => handlers.get('agent/created')({agent:{id:value.id},source:'resume'});
const step = (value,id='question') => handlers.get('agent/pre-step')(
  {agent:{id:value.id},turn:1,step:1,signal:{aborted:false}},
  async () => ({kind:'enter',messages:[{id,role:'user',source:{kind:'user'},
    content:[{type:'text',text:'Verify the project state.'}]}]}));
async function tool(value,name='read') {
  let executed = 0, result, error;
  try {
    result = await handlers.get('tools/pre-execute')({agent:{id:value.id},name,
      callId:'call-'+calls.length,arguments:{},signal:toolSignal},async () => {
        executed += 1; return {kind:'execute'};
      });
  } catch (caught) { error = caught; }
  return {executed,result,error};
}
const successfulNonStarts = () => calls.filter(call => !['SessionStart','SubagentStart'].includes(call.event));
function assertCarriedToken() {
  for (const call of successfulNonStarts()) {
    const binding = bindings.get(call.payload.dsh_bridge_instance);
    assert.deepEqual(call.payload.dsh_trajectory,{generation:binding.generation,token:binding.token});
  }
}

if (scenario === 'lost-start-reply' || scenario === 'terminated-start') {
  await startup(owner);
  const original = calls[0].payload.dsh_bridge_instance;
  const decision = await step(owner);
  assert.match(JSON.stringify(decision),/matched project knowledge/);
  assert.equal((await tool(owner)).executed,1);
  assert.equal(starts,2,'one initial attempt and one bounded recovery');
  assert.equal(calls.filter(call=>call.event==='SessionStart')[1].payload.dsh_handshake_only,true);
  assert.equal(minted,1,'recover the existing server token rather than mint another binding');
  assert.ok(calls.every(call=>call.payload.dsh_bridge_instance===original));
  assertCarriedToken();
} else if (scenario === 'resume-without-created') {
  const decision = await step(owner);
  assert.match(JSON.stringify(decision),/matched project knowledge/);
  assert.equal(starts,1);
  assert.equal(calls[0].event,'SessionStart');
  assert.equal(calls[0].payload.source,'resume');
  assert.equal(calls[0].payload.dsh_handshake_only,true);
  assert.equal((await tool(owner,'session_context')).executed,1);
  assertCarriedToken();
} else if (scenario === 'tool-before-created') {
  assert.equal((await tool(owner,'session_context')).executed,1);
  assert.deepEqual(calls.map(call=>call.event),['SessionStart','PreToolUse']);
  assert.equal(calls[0].payload.source,'resume');
  assert.equal(calls[0].payload.dsh_handshake_only,true);
  assertCarriedToken();
} else if (scenario === 'handle-without-token') {
  await startup(owner);
  assert.equal((await tool(owner,'session_context')).executed,1);
  assert.equal(starts,2,'an existing context handle cannot skip trajectory recovery');
  assert.equal(calls.filter(call=>call.event==='SessionStart')[1].payload.dsh_handshake_only,true);
  assert.equal(minted,1);
  assertCarriedToken();
} else if (scenario === 'concurrent-startup') {
  const results = await Promise.all([startup(owner),step(owner),tool(owner),tool(owner,'learning_status')]);
  assert.equal(starts,1); assert.equal(minted,1); assert.equal(maximumActiveStarts,1);
  assert.equal(results[2].executed,1); assert.equal(results[3].executed,1);
  assertCarriedToken();
} else if (scenario === 'duplicate-startup') {
  const results = await Promise.all([startup(owner),startup(owner),step(owner),tool(owner)]);
  assert.equal(starts,1,'duplicate native start notifications share one in-flight full start');
  assert.equal(minted,1); assert.equal(maximumActiveStarts,1);
  assert.equal(results[3].executed,1);
  assertCarriedToken();
} else if (scenario === 'concurrent-resume') {
  const results = await Promise.all([step(owner),tool(owner),tool(owner,'learning_status')]);
  assert.equal(starts,1); assert.equal(minted,1); assert.equal(maximumActiveStarts,1);
  assert.match(JSON.stringify(results[0]),/matched project knowledge/);
  assert.equal(results[1].executed,1); assert.equal(results[2].executed,1);
  assertCarriedToken();
} else if (scenario === 'prompt-recovery-retry') {
  await step(owner,'same-human-message');
  const decision = await step(owner,'same-human-message');
  assert.match(JSON.stringify(decision),/matched project knowledge/);
  assert.equal(starts,2); assert.equal(minted,1);
  assert.equal(calls.filter(call=>call.event==='UserPromptSubmit').length,1,
    'a failed handshake must not permanently consume the human-message dedup identity');
  assertCarriedToken();
} else if (scenario === 'successful-warning') {
  const decision = await step(owner,'same-human-message');
  assert.match(JSON.stringify(decision),/matched project knowledge/,
    'an administrator warning cannot discard successfully retrieved worker context');
  await step(owner,'same-human-message');
  assert.equal(calls.filter(call=>call.event==='UserPromptSubmit').length,1,
    'a successful prompt with a warning remains captured');
} else if (scenario === 'disabled') {
  process.env.ALP_DISABLED = '1';
  assert.equal((await tool(owner)).executed,1);
  await step(owner);
  assert.equal(calls.length,0,'disabled plugin must not launch synthetic hook processes');
} else if (scenario === 'observe-runtime-failure') {
  await startup(owner);
  assert.equal((await tool(owner)).executed,1,'ordinary observation failure preserves observe behavior');
} else if (['disposed-inflight','canceled-inflight','canceled-error','canceled-nonzero','hot-reload-inflight'].includes(scenario)) {
  await startup(owner);
  if (scenario.startsWith('canceled-')) toolSignal = {aborted:false};
  const outcome = await tool(owner);
  assert.equal(outcome.executed,0,'a disposed or canceled request cannot continue into the real tool');
} else if (scenario === 'disposed-during-startup') {
  const starting = startup(owner);
  const pending = tool(owner);
  handlers.get('session/disposed')(owner);
  await starting;
  assert.equal((await pending).executed,0,
    'a tool waiting for startup must not recreate a trajectory after its session was disposed');
} else if (scenario === 'stale-generation' || scenario === 'stale-epoch') {
  await startup(owner);
  const original = calls[0].payload.dsh_bridge_instance;
  if (scenario === 'stale-generation') generation += 1; else epoch += 1;
  for (let index=0;index<2;index++) {
    const outcome = await tool(owner);
    assert.equal(outcome.executed,0);
    assert.match(String(outcome.error||JSON.stringify(outcome.result)),/reconnect_required/);
  }
  assert.equal(starts,1,'a known old token must not trigger a fresh handshake');
  assert.equal(minted,1);
  assert.ok(calls.every(call=>call.payload.dsh_bridge_instance===original));
  assertCarriedToken();
} else if (scenario === 'scope-isolation') {
  const child = session('child',{parentSession:'owner',origin:'subagent'});
  const other = session('other',{cwd:otherWorkspace});
  await startup(owner); await startup(child); await startup(other);
  assert.equal((await tool(owner)).executed,1);
  assert.equal((await tool(child)).executed,1);
  assert.equal((await tool(other)).executed,1);
  assert.equal(starts,3); assert.equal(minted,3);
  const roots = calls.filter(call=>['SessionStart','SubagentStart'].includes(call.event));
  assert.equal(new Set(roots.map(call=>call.payload.dsh_bridge_instance)).size,3);
  assert.equal(roots[1].event,'SubagentStart');
  assert.equal(roots[1].payload.sessionId,'owner'); assert.equal(roots[1].payload.agentId,'child');
  assert.equal(roots[2].payload.cwd,otherWorkspace);
  assertCarriedToken();
} else if (scenario === 'recovery-fails') {
  const outcome = await tool(owner,'session_context');
  assert.equal(outcome.executed,0,'no real tool may execute without the handshake');
  assert.match(String(outcome.error||JSON.stringify(outcome.result)),/Agent Learning/);
  assert.ok(starts>=1 && starts<=2,'recovery must be bounded within one tool request');
  assert.equal(successfulNonStarts().length,0,'the tool pre-event must wait for a valid trajectory');
} else throw new Error('unknown scenario '+scenario);
console.log(JSON.stringify({scenario,ok:true,calls:calls.length,starts,minted}));
"""


@unittest.skipUnless(NODE, "Node.js is required for the DSH native bridge")
class DshHandshakeRecoveryBridgeTests(unittest.TestCase):
    def setUp(self):
        LOCAL.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-", dir=LOCAL)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "project"
        self.other = self.root / "second-project"
        self.workspace.mkdir()
        self.other.mkdir()
        self.plugin = self.root / "plugin"
        self.bridge = self.plugin / "host/index.mjs"
        shutil.copytree(BRIDGE.parent, self.bridge.parent)
        (self.plugin / "runtime-release.json").write_text(json.dumps({
            "python": sys.executable, "runtime_directory": str(self.root / "runtime"),
            "workspace": str(self.workspace), "workspaces": [str(self.workspace), str(self.other)],
            "home": str(self.root / "home"), "hook_mode": "observe",
        }), encoding="utf-8")

    def run_scenario(self, scenario):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
                       "ALP_CONFIG": str(self.root / "config.json"), "CODEX_HOME": str(self.root / "codex"),
                       "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
                       "TMP": str(self.root), "TEMP": str(self.root), "TMPDIR": str(self.root)}
        response = subprocess.run(
            [NODE, "--input-type=module", "-e", PROBE, str(self.bridge), scenario,
             str(self.workspace), str(self.other)],
            cwd=self.workspace, env=environment, capture_output=True, timeout=15,
        )
        self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
        self.assertTrue(json.loads(response.stdout.decode("utf-8"))["ok"])

    def test_lost_first_reply_recovers_the_same_instance_token(self):
        self.run_scenario("lost-start-reply")

    def test_terminated_initial_process_recovers_on_the_next_event(self):
        self.run_scenario("terminated-start")

    def test_resumed_session_without_agent_created_handshakes_before_prompt(self):
        self.run_scenario("resume-without-created")

    def test_recovery_tool_before_agent_created_handshakes_before_execution(self):
        self.run_scenario("tool-before-created")

    def test_existing_context_handle_does_not_replace_trajectory_readiness(self):
        self.run_scenario("handle-without-token")

    def test_concurrent_startup_prompt_and_tools_share_one_handshake(self):
        self.run_scenario("concurrent-startup")

    def test_duplicate_native_startups_share_one_full_handshake(self):
        self.run_scenario("duplicate-startup")

    def test_concurrent_resumed_prompt_and_tools_share_one_light_handshake(self):
        self.run_scenario("concurrent-resume")

    def test_failed_prompt_handshake_does_not_permanently_consume_message_identity(self):
        self.run_scenario("prompt-recovery-retry")

    def test_successful_prompt_with_admin_warning_keeps_context_and_dedup(self):
        self.run_scenario("successful-warning")

    def test_disabled_plugin_never_handshakes_or_blocks_tools(self):
        self.run_scenario("disabled")

    def test_observe_runtime_failure_keeps_existing_observation_behavior(self):
        self.run_scenario("observe-runtime-failure")

    def test_disposed_session_cannot_continue_an_inflight_tool(self):
        self.run_scenario("disposed-inflight")

    def test_canceled_request_cannot_continue_an_inflight_tool(self):
        self.run_scenario("canceled-inflight")

    def test_aborted_process_error_cannot_continue_an_observe_mode_tool(self):
        self.run_scenario("canceled-error")

    def test_canceled_nonzero_process_exit_cannot_continue_an_observe_mode_tool(self):
        self.run_scenario("canceled-nonzero")

    def test_hot_reloaded_bridge_cannot_continue_an_inflight_tool(self):
        self.run_scenario("hot-reload-inflight")

    def test_session_disposed_while_tool_awaits_startup_is_not_implicitly_recreated(self):
        self.run_scenario("disposed-during-startup")

    def test_old_generation_token_is_never_cleared_or_automatically_rebound(self):
        self.run_scenario("stale-generation")

    def test_old_account_epoch_token_is_never_automatically_rebound(self):
        self.run_scenario("stale-epoch")

    def test_parent_child_and_installed_workspace_trajectories_stay_separate(self):
        self.run_scenario("scope-isolation")

    def test_failed_handshake_denies_tools_with_an_explicit_bounded_error(self):
        self.run_scenario("recovery-fails")


class DshHandshakeRecoveryBackendTests(unittest.TestCase):
    def setUp(self):
        LOCAL.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="backend-", dir=LOCAL)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime = Runtime(self.root / "home", config={}, host="dsh")
        self.addCleanup(self.runtime.close)
        self.adapter = DshAdapter(self.runtime, self.workspace)
        self.instance = uuid.uuid4().hex
        self.raw = {"sessionId": "native-resume", "dsh_bridge_instance": self.instance}

    def test_committed_token_after_later_error_can_be_retrieved_by_same_instance_start(self):
        with patch.object(self.runtime, "handle_hook", side_effect=RuntimeError("synthetic lost response")):
            with self.assertRaisesRegex(RuntimeError, "synthetic lost response"):
                self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        token = self.runtime.db.execute("SELECT token FROM dsh_bridge_bindings").fetchone()[0]
        blocked = self.adapter.handle("PreToolUse", {**self.raw, "name": "read", "callId": "before", "arguments": {}})
        self.assertIn("dsh_trajectory_token_required", blocked["reason"])
        recovered = self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        self.assertEqual(recovered["dsh_trajectory"], {"generation": 1, "token": token})
        allowed = self.adapter.handle("PreToolUse", {**self.raw, "name": "read", "callId": "after",
                                                    "arguments": {}, "dsh_trajectory": recovered["dsh_trajectory"]})
        self.assertNotEqual(allowed.get("decision"), "deny")
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM dsh_bridge_bindings").fetchone()[0], 1)

    def test_lost_token_recovery_does_not_rebind_after_explicit_reset(self):
        self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        reset_session(self.runtime, self.workspace, "native-resume")
        result = self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        self.assertTrue(result["dsh_reconnect_required"])
        self.assertIn("dsh_trajectory_reconnect_required", result["systemMessage"])
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM dsh_bridge_bindings").fetchone()[0], 1)

    def test_handshake_only_retrieves_token_without_runtime_hooks_or_context(self):
        original = self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        before = self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with patch.object(self.runtime, "handle_hook") as hook:
            result = self.adapter.handle("SessionStart", {**self.raw, "source": "resume", "dsh_handshake_only": True})
        hook.assert_not_called()
        self.assertEqual(result["dsh_trajectory"], original["dsh_trajectory"])
        self.assertNotIn("hookSpecificOutput", result)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_new_bridge_can_light_handshake_after_reset_without_task_delivery(self):
        self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        reset_session(self.runtime, self.workspace, "native-resume")
        project = self.runtime.project(self.workspace)
        conversation = self.runtime.host_conversation("native-resume")
        task_before = self.runtime.task(project, conversation)
        before = self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with patch.object(self.runtime, "handle_hook") as hook:
            result = self.adapter.handle("SessionStart", {
                "sessionId": "native-resume", "dsh_bridge_instance": uuid.uuid4().hex,
                "source": "resume", "dsh_handshake_only": True,
            })
        hook.assert_not_called()
        self.assertEqual(result["dsh_trajectory"]["generation"], 2)
        self.assertEqual(self.runtime.task(project, conversation), task_before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM dsh_bridge_bindings").fetchone()[0], 2)

    def test_handshake_only_invalid_type_event_source_or_instance_cannot_short_circuit(self):
        cases = [
            ("SessionStart", {"dsh_handshake_only": value, "source": "resume"})
            for value in (None, 1, "true", [])
        ]
        cases += [(event, {"dsh_handshake_only": True, "source": "resume"})
                  for event in ("UserPromptSubmit", "PreToolUse", "Stop")]
        cases += [("SessionStart", {"dsh_handshake_only": True, "source": source})
                  for source in (None, "startup", "clear", "compact")]
        cases.append(("SessionStart", {"dsh_handshake_only": True, "source": "resume", "dsh_bridge_instance": None}))
        before = self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with patch.object(self.runtime, "handle_hook") as hook:
            for event, values in cases:
                with self.subTest(event=event, values=values):
                    with self.assertRaisesRegex(ContractError, "dsh_invalid_handshake_request"):
                        self.adapter.handle(event, {**self.raw, **values})
        hook.assert_not_called()
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM dsh_bridge_bindings").fetchone()[0], 0)

    def _assert_old_token_light_handshake_refused(self, original):
        before = self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with patch.object(self.runtime, "handle_hook") as hook:
            result = self.adapter.handle("SessionStart", {
                **self.raw, "source": "resume", "dsh_handshake_only": True,
                "dsh_trajectory": original["dsh_trajectory"],
            })
        hook.assert_not_called()
        self.assertTrue(result["dsh_reconnect_required"])
        self.assertIn("dsh_trajectory_reconnect_required", result["systemMessage"])
        self.assertNotIn("dsh_trajectory", result)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM dsh_bridge_bindings").fetchone()[0], 1)

    def test_handshake_only_with_known_token_cannot_rebind_after_generation_reset(self):
        original = self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        reset_session(self.runtime, self.workspace, "native-resume")
        self._assert_old_token_light_handshake_refused(original)

    def test_handshake_only_with_known_token_cannot_rebind_after_account_epoch_change(self):
        original = self.adapter.handle("SessionStart", {**self.raw, "source": "resume"})
        self.runtime.scope_account_changed("synthetic-account-switch")
        self._assert_old_token_light_handshake_refused(original)


if __name__ == "__main__":
    unittest.main()
