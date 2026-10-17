"""DSH (DeepSeek Harness) 宿主导出、固定运行副本共用与覆盖升级的隔离保护检查。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from agent_learning.cli import main
from agent_learning.install import install, _hook_commands
from agent_learning.install_update import INSTALL_MANIFEST, inspect_installation, snapshot
from agent_learning.release import validate_release_root
from agent_learning.runtime import ContractError


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class DshInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-install-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self._cleanup_root)
        self.workspace = self.root / "dsh 中文 workspace"
        self.workspace.mkdir()
        self.home = self.root / "shared knowledge"
        self.target = self.root / "dsh plugin"
        self.store = self.root / "fixed releases"

    def _cleanup_root(self):
        import shutil
        from agent_learning.release import filesystem_path
        if self.root.exists() and self.root.name.startswith("alp-dsh-install-"):
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.temp.cleanup()

    def initial(self, mode=None, bind_workspace=True):
        return install(
            self.target,
            runtime_store=self.store,
            hook_mode=mode,
            host="dsh",
            workspace=self.workspace if bind_workspace else None,
            home=self.home if bind_workspace else None,
        )

    def test_dsh_export_creates_bundle_hooks_mcp_and_validates_manifest(self):
        result = self.initial()
        self.assertEqual(result["target_host"], "dsh")
        self.assertEqual(result["hook_mode"], "observe")
        self.assertFalse(self.home.exists(), "export must not open or create a knowledge database")
        self.assertFalse((self.target / ".codex-plugin").exists())
        self.assertTrue((self.target / "plugin.json").is_file())
        self.assertTrue((self.target / "package.json").is_file())
        self.assertTrue((self.target / "host/index.mjs").is_file())
        for name in ("extensions", "fetch-compat", "delegation-policy", "google-thinking", "messages-compat", "net-tune"):
            relative = "host/" + name + ".mjs"
            self.assertEqual((self.target / relative).read_bytes(),
                             (Path(result["runtime_directory"]) / "host_resources/dsh" / relative).read_bytes())
        self.assertTrue((self.target / "client/index.js").is_file())
        reviewer = "reviewer/runner.mjs"
        self.assertEqual((self.target / reviewer).read_bytes(),
                         (Path(result["runtime_directory"]) / "host_resources/dsh" / reviewer).read_bytes())
        self.assertTrue((self.target / "rules/learning.md").is_file())
        self.assertTrue((self.target / "skills/learning-worker/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-admin/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-history/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-browser/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-review/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-review/agents/openai.yaml").is_file())
        self.assertTrue((self.target / "skills/learning-debug/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-debug/agents/openai.yaml").is_file())
        plugin = read_json(self.target / "plugin.json")
        pkg = read_json(self.target / "package.json")
        self.assertEqual(plugin["name"], "agent-learning")
        self.assertEqual(pkg["name"], "agent-learning")
        self.assertIn("dsh", pkg)
        hooks = read_json(self.target / "hooks.json")["agent-learning"]
        self.assertEqual(set(hooks), {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "Stop", "Interrupt", "SubagentStart", "SubagentStop",
        })
        runtime = Path(result["runtime_directory"])
        mcp = read_json(self.target / "mcp_config.json")["mcpServers"]["agent_learning"]
        self.assertEqual(mcp["command"], result["python"])
        self.assertEqual(mcp["args"], [
            "-B", str(runtime / "scripts/learning.py"),
            "--workspace", str(self.workspace), "--home", str(self.home),
            "serve", "--role", "worker", "--host", "dsh",
        ])
        self.assertEqual(read_json(self.target / INSTALL_MANIFEST)["target_host"], "dsh")
        self.assertEqual(inspect_installation(self.target, _hook_commands)["target_host"], "dsh")
        validate_release_root(runtime)
        self.assertTrue((runtime / "host_resources/dsh/plugin.json").is_file())
        self.assertTrue((runtime / "hooks/dsh_dispatch.py").is_file())
        self.assertEqual((runtime / "host_resources/dsh/skills/learning-review/SKILL.md").read_bytes(),
                         (self.target / "skills/learning-review/SKILL.md").read_bytes())
        for relative in ("SKILL.md", "agents/openai.yaml"):
            self.assertEqual((runtime / "host_resources/dsh/skills/learning-debug" / relative).read_bytes(),
                             (self.target / "skills/learning-debug" / relative).read_bytes())

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to exercise the DSH native bridge")
    def test_native_bridge_registers_review_skill_with_exported_resource_directory(self):
        self.initial()
        probe = r"""
import { pathToFileURL } from 'node:url';
const { apply } = await import(pathToFileURL(process.argv[1]).href);
const skills = [];
const commands = [];
const tools = [];
const ctx = {
  tools: { register(spec) { tools.push(spec.name); } },
  effect() {},
  on() {},
  inject(names, callback) {
    callback({
      skills: { register(spec) { skills.push(spec); } },
      commands: { register(spec) { commands.push(spec.name); } },
    });
  },
};
apply(ctx);
console.log(JSON.stringify({ skills, commands, tools }));
"""
        response = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", probe, str(self.target / "host/index.mjs")],
            cwd=self.workspace, capture_output=True, timeout=10,
        )
        self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
        result = json.loads(response.stdout.decode("utf-8"))
        skills = {item["name"]: item for item in result["skills"]}
        self.assertEqual(set(skills), {"learning-worker", "learning-admin", "learning-history",
                                       "learning-browser", "learning-review", "learning-debug"})
        review = skills["learning-review"]
        expected_directory = self.target / "skills/learning-review"
        self.assertEqual(review["resourceBase"]["kind"], "directory")
        self.assertEqual(Path(review["resourceBase"]["path"]), expected_directory)
        # Node preserves resource line endings; compare the exact decoded bytes on every platform.
        self.assertEqual(review["content"], (expected_directory / "SKILL.md").read_bytes().decode("utf-8"))
        self.assertEqual(review["source"], "bundled")
        debug = skills["learning-debug"]
        self.assertEqual(debug["resourceBase"]["kind"], "directory")
        self.assertEqual(Path(debug["resourceBase"]["path"]), self.target / "skills/learning-debug")
        self.assertEqual(debug["content"], (self.target / "skills/learning-debug/SKILL.md").read_bytes().decode("utf-8"))
        self.assertEqual(debug["source"], "bundled")
        self.assertIn("learning-browser", result["commands"])
        self.assertIn("learning-debug", result["commands"])
        self.assertNotIn("learning-review", result["commands"])
        self.assertIn("learning_browser", result["tools"])
        self.assertNotIn("learning_review", result["tools"])
        self.assertFalse(self.home.exists(), "skill registration must not initialize the knowledge database")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to exercise the DSH native bridge")
    def test_debug_command_uses_only_native_status_even_with_action_suffixes(self):
        self.initial()
        probe = r"""
import { EventEmitter } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import childProcess from 'node:child_process';
import { pathToFileURL } from 'node:url';
const calls = [];
const historyMarker = 'PRIVATE_HISTORY_SHOULD_NOT_RENDER';
const statusData = {
  project_id: 'synthetic-project', home: 'synthetic-knowledge-home', host_scope: 'dsh:synthetic-host',
  admin_enabled: true, admin_admission_enabled: false,
  admin_routing: {
    policy: {model_mode: 'fixed', model: 'configured-model'},
    executor: {model_mode: 'fixed', model: 'resolved-model'},
    ready: false, reason: 'synthetic_route_blocked',
  },
  admin_budget: {remaining_calls: 999, admission_status: 'available'},
  project_usage: {scope:'project', project_id:'synthetic-project', counted_attempts:0, remaining_calls:7},
  global_usage: {counted_attempts:32, remaining_calls:68},
  admission: {admission_status:'concurrency_limited'},
  history_import: {body: historyMarker.repeat(2000)},
  work_memory: {evidence: historyMarker.repeat(2000)},
};
childProcess.spawn = (executable, args, options) => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  const call = { executable, args, cwd: options.cwd, hidden: options.windowsHide, input: '' };
  calls.push(call);
  child.stdin = {
    write(value) { call.input += value; },
    end() { queueMicrotask(() => {
      const result = calls.length === 5
        ? {text: 'UNSAFE_RAW_FALLBACK_' + historyMarker, unexpected_history: statusData.history_import}
        : {text: 'Synthetic native status', data: statusData};
      child.stdout.emit('data', Buffer.from(JSON.stringify({ok:true,result})));
      child.emit('close', 0);
    }); },
  };
  child.kill = () => {};
  return child;
};
syncBuiltinESMExports();
const commands = new Map();
const { apply } = await import(pathToFileURL(process.argv[1]).href);
apply({
  tools: { register() {} }, effect() {}, on() {},
  inject(_names, callback) { callback({
    skills: { register() {} },
    commands: { register(spec) { commands.set(spec.name, spec); } },
  }); },
});
const debug = commands.get('learning-debug');
const outputs = [];
for (const rawInput of ['/learning-debug', '/learning-debug on fixed model --allow-review', '/learning-debug once', '/learning-debug browser start']) {
  outputs.push(await debug.handler({rawInput}));
}
const missingData = await debug.handler({rawInput:'/learning-debug'});
const legacy = await commands.get('learning-status').handler({rawInput:'/learning-status original suffix'});
console.log(JSON.stringify({calls,outputs,missingData,legacy,inputClaim:debug.input??null}));
"""
        response = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", probe, str(self.target / "host/index.mjs")],
            cwd=self.workspace, capture_output=True, timeout=10,
        )
        self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
        result = json.loads(response.stdout.decode("utf-8"))
        self.assertIsNone(result["inputClaim"], "debug should execute as a fixed command without an argument prompt")
        self.assertEqual(len(result["calls"]), 6)
        for call in result["calls"][:5]:
            payload = json.loads(call["input"])
            self.assertEqual(payload["slash_command"], "learning-status")
            self.assertEqual(payload["raw_input"], "/learning-status")
            self.assertEqual(payload["home"], str(self.home))
            self.assertEqual(payload["workspace"], str(self.workspace))
            self.assertEqual(call["args"][0], "-B")
            self.assertEqual(Path(call["args"][1]).name, "dsh_tool.py")
            self.assertTrue(call["hidden"])
        for output in result["outputs"]:
            self.assertEqual(output["kind"], "success")
            self.assertLess(len(output["text"]), 2000)
            for expected in ("synthetic-project", "synthetic-knowledge-home", "dsh:synthetic-host",
                             "已关闭", "fixed", "resolved-model", "7", "synthetic_route_blocked",
                             "concurrency_limited", "所有项目合计尝试：32", "项目剩余审核额度：7", "不能据此确认 Hooks 已连接"):
                self.assertIn(expected, output["text"])
            self.assertNotIn("configured-model", output["text"])
            self.assertNotIn("PRIVATE_HISTORY_SHOULD_NOT_RENDER", output["text"])
            self.assertNotIn("Synthetic native status", output["text"])
        self.assertEqual(result["missingData"]["kind"], "success")
        self.assertIn("unknown", result["missingData"]["text"])
        self.assertNotIn("UNSAFE_RAW_FALLBACK", result["missingData"]["text"])
        self.assertNotIn("PRIVATE_HISTORY_SHOULD_NOT_RENDER", result["missingData"]["text"])
        self.assertEqual(result["legacy"], {"kind": "success", "text": "Synthetic native status"})
        legacy_payload = json.loads(result["calls"][-1]["input"])
        self.assertEqual(legacy_payload["slash_command"], "learning-status")
        self.assertEqual(legacy_payload["raw_input"], "/learning-status original suffix")
        self.assertFalse(self.home.exists(), "debug routing probe must not initialize storage or run a real model")

    def test_dsh_export_supports_dynamic_workspace_and_enforce_mode(self):
        result = self.initial(mode="enforce", bind_workspace=False)
        self.assertEqual(result["hook_mode"], "enforce")
        self.assertNotIn("workspace", result)
        runtime = Path(result["runtime_directory"])
        mcp = read_json(self.target / "mcp_config.json")["mcpServers"]["agent_learning"]
        self.assertEqual(mcp["args"], [
            "-B", str(runtime / "scripts/learning.py"),
            "serve", "--role", "worker", "--host", "dsh",
        ])
        hooks = read_json(self.target / "hooks.json")["agent-learning"]
        for event, handlers in hooks.items():
            command, windows = _hook_commands(
                result["python"], runtime / "hooks/dsh_dispatch.py", event, "enforce", host="dsh"
            )
            for entry in handlers:
                for handler in entry.get("hooks", [entry]):
                    self.assertEqual(handler["command"], windows if os.name == "nt" else command)
                    if os.name == "nt":
                        decoded = base64.b64decode(handler["command"].split()[-1]).decode("utf-16-le")
                        self.assertIn("'-B'", decoded)
                        self.assertIn("'--hook-mode' 'enforce'", decoded)

    def test_three_hosts_share_same_fixed_runtime_release_and_reject_cross_host_overwrite(self):
        dsh_res = self.initial()
        codex_target = self.root / "codex plugin"
        ag_target = self.root / "ag plugin"
        codex_res = install(codex_target, runtime_store=self.store, host="codex")
        ag_res = install(ag_target, runtime_store=self.store, host="antigravity",
                         workspace=self.workspace, home=self.home)
        self.assertEqual(dsh_res["runtime_directory"], codex_res["runtime_directory"])
        self.assertEqual(dsh_res["runtime_directory"], ag_res["runtime_directory"])

        before_dsh = snapshot(self.target)
        for wrong_host in ("codex", "antigravity"):
            with self.assertRaisesRegex(ContractError, "different target host"):
                install(self.target, upgrade=True, host=wrong_host,
                        workspace=self.workspace if wrong_host == "antigravity" else None,
                        home=self.home if wrong_host == "antigravity" else None)
        self.assertEqual(snapshot(self.target), before_dsh)

        with self.assertRaisesRegex(ContractError, "different target host"):
            install(codex_target, upgrade=True, host="dsh")
        with self.assertRaisesRegex(ContractError, "different target host"):
            install(ag_target, upgrade=True, host="dsh")

    def test_dsh_upgrade_preserves_bindings_extra_files_and_creates_backup(self):
        self.initial("enforce", bind_workspace=True)
        (self.target / "notes/custom.txt").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "notes/custom.txt").write_text("user custom file", encoding="utf-8")
        updated = install(self.target, upgrade=True, host="dsh")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["target_host"], "dsh")
        self.assertEqual(updated["hook_mode"], "enforce")
        self.assertEqual(updated["workspace"], str(self.workspace))
        self.assertEqual(updated["home"], str(self.home))
        self.assertEqual(updated["preserved_extra_files"], ["notes/custom.txt"])
        self.assertEqual((self.target / "notes/custom.txt").read_text(encoding="utf-8"), "user custom file")
        self.assertTrue(Path(updated["backup_directory"]).is_dir())

    def _old_release_with_unmanaged_modules(self):
        """Synthetic pinned old release whose optional scripts were extra files."""
        from agent_learning.install_update import DSH_HOST_ADOPTION, dsh_hooks, mcp_document, payload_mapping
        from agent_learning.release import manifest_for
        first = self.initial()
        runtime = Path(first["runtime_directory"])
        for name in (*DSH_HOST_ADOPTION, "host/extensions.mjs", "host/fetch-compat.mjs"):
            (runtime / "host_resources/dsh" / name).unlink()
        for name in ("host/extensions.mjs", "host/fetch-compat.mjs"):
            (self.target / name).unlink()
        manifest = manifest_for(runtime)
        (runtime / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
        renamed = self.store / manifest["release_id"]
        runtime.rename(renamed)
        receipt = read_json(self.target / "runtime-release.json")
        receipt.update({key: manifest[key] for key in ("version", "protocol_version", "content_digest", "release_id")})
        receipt["runtime_directory"] = str(renamed)
        (self.target / "runtime-release.json").write_text(json.dumps(receipt), encoding="utf-8")
        mcp = mcp_document(renamed, receipt["python"], "dsh", receipt["workspace"], receipt["home"])
        hooks = dsh_hooks(renamed, receipt["python"], receipt["hook_mode"], _hook_commands,
                          receipt["workspace"], receipt["home"])
        for name, document in (("mcp_config.json", mcp), ("hooks.json", hooks)):
            (self.target / name).write_text(json.dumps(document), encoding="utf-8")
        old_manifest = read_json(self.target / INSTALL_MANIFEST)
        old_manifest.update(release_id=manifest["release_id"], files={
            name: hashlib.sha256((self.target / name).read_bytes()).hexdigest()
            for name in (*payload_mapping(manifest["files"], "dsh"), "runtime-release.json")})
        (self.target / INSTALL_MANIFEST).write_text(json.dumps(old_manifest), encoding="utf-8")
        inspect_installation(self.target, _hook_commands, target_host="dsh")

    def test_upgrade_adopts_identical_dsh_modules_and_keeps_original_backup(self):
        from agent_learning.install_update import DSH_HOST_ADOPTION
        self._old_release_with_unmanaged_modules()
        before = snapshot(self.target)
        result = install(self.target, upgrade=True, host="dsh")
        self.assertEqual(result["adopted_extra_files"], sorted(DSH_HOST_ADOPTION))
        self.assertEqual(result["preserved_extra_files"], [])
        self.assertEqual(snapshot(Path(result["backup_directory"])), before)
        self.assertTrue(set(DSH_HOST_ADOPTION) <= set(read_json(self.target / INSTALL_MANIFEST)["files"]))
        inspect_installation(self.target, _hook_commands, target_host="dsh")
        self.assertEqual(install(self.target, upgrade=True, host="dsh")["adopted_extra_files"], [])

    def test_upgrade_refuses_custom_host_module_without_changing_old_tree(self):
        self._old_release_with_unmanaged_modules()
        (self.target / "host/google-thinking.mjs").write_text("// custom implementation", encoding="utf-8")
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "user file conflicts"):
            install(self.target, upgrade=True, host="dsh")
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_dsh_upgrade_with_second_workspace_preserves_both_projects(self):
        self.initial("observe", bind_workspace=True)
        second = self.root / "second-project"
        second.mkdir()
        updated = install(self.target, upgrade=True, host="dsh", workspace=second)
        self.assertEqual(updated["workspace"], str(second.resolve()))
        self.assertEqual(updated["workspaces"], [str(self.workspace.resolve()), str(second.resolve())])
        receipt = read_json(self.target / "runtime-release.json")
        self.assertEqual(receipt["workspaces"], [str(self.workspace.resolve()), str(second.resolve())])
        again = install(self.target, upgrade=True, host="dsh")
        self.assertEqual(again["workspaces"], [str(self.workspace.resolve()), str(second.resolve())])

    @unittest.skipUnless(shutil.which("node"), "node is required for DSH bridge probe")
    def test_dsh_enforce_mode_fails_closed_when_python_bridge_process_fails(self):
        self.initial("enforce", bind_workspace=True)
        receipt_path = self.target / "runtime-release.json"
        receipt = read_json(receipt_path)
        receipt["python"] = str(self.root / "nonexistent-python-binary.exe")
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        probe = """
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[1]).href);
const handlers = new Map();
const ctx = {
  tools: { register() { return () => {}; } },
  agents: { list() { return []; } },
  effect() {},
  inject() {},
  on(event, fn) { handlers.set(event, fn); },
};
mod.apply(ctx);
const pre = handlers.get("tools/pre-execute");
let downstream = 0;
const verdict = await pre(
  { agent: { id: "s1" }, callId: "c1", name: "pwsh", arguments: { command: "echo hi" } },
  async () => { downstream += 1; return { kind: "allow" }; },
);
process.stdout.write(JSON.stringify({ verdict, downstream }));
"""
        response = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", probe, str(self.target / "host/index.mjs")],
            cwd=self.workspace, capture_output=True, timeout=10,
        )
        self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
        out = json.loads(response.stdout.decode("utf-8"))
        self.assertEqual(out["downstream"], 0)
        self.assertEqual(out["verdict"]["kind"], "deny")
        self.assertIn("runtime_unavailable", out["verdict"]["reason"])


if __name__ == "__main__":
    unittest.main()
