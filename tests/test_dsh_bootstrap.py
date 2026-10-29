"""A fresh GitHub bundle must initialize without a developer-generated receipt."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required to exercise DSH first-time setup")
class DshBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-bootstrap-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.cleanup_root)
        self.dsh = self.root / "DSH home"
        self.profile = self.dsh / "profiles" / "web"
        self.profile.mkdir(parents=True)
        (self.profile / "package.json").write_text(
            '{"name":"synthetic-web-profile","private":true,"dsh":{"profile":{"bundles":[]}}}',
            encoding="utf-8")
        self.workspace = self.root / "project 中文 with spaces"
        self.workspace.mkdir()
        self.home = self.root / "knowledge"
        self.config = self.root / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.environment = dict(os.environ, DSH_HOME=str(self.dsh), ALP_HOME=str(self.home),
                                ALP_CONFIG=str(self.config), PYTHONDONTWRITEBYTECODE="1",
                                PYTHONIOENCODING="utf-8")
        self.environment.pop("ALP_DISABLED", None)
        self.environment.pop("ALP_CONTEXT_HANDLE", None)

    def cleanup_root(self):
        # The fixed release includes deeply nested resources. Use the same
        # extended Windows path handling as the existing install fixtures.
        from agent_learning.release import filesystem_path
        if (self.root.name.startswith("alp-dsh-bootstrap-")
                and self.root.parent == Path(tempfile.gettempdir()).resolve()):
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)

    def node(self, body, data=None, *, timeout=45, environment=None):
        script = """
import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
const bootstrap = await import(pathToFileURL(process.argv[1]).href);
const data = JSON.parse(process.argv[2]);
""" + body
        response = subprocess.run(
            [NODE, "--input-type=module", "-e", script,
             str(SOURCE / "host_resources/dsh/bootstrap.mjs"), json.dumps(data or {})],
            cwd=self.workspace, env=environment or self.environment,
            capture_output=True, timeout=timeout)
        self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
        return json.loads(response.stdout.decode("utf-8"))

    def layout(self, profile=None):
        return self.node("console.log(JSON.stringify(bootstrap.profileLayout(data.profile)));",
                         {"profile": str(profile or self.profile)})

    def initialize(self, workspace=None, profile=None):
        return self.node("""
const result = await bootstrap.initialize({profileDirectory:data.profile,
  workspace:data.workspace, python:data.python});
console.log(JSON.stringify(result));
""", {"profile": str(profile or self.profile), "workspace": str(workspace or self.workspace),
        "python": os.environ.get("ALP_TEST_PYTHON", sys.executable)})

    def apply(self, profile=None):
        return self.node("""
const commands = [], events = [], tools = [], skills = [], routes = [];
const ctx = {
  baseUrl:pathToFileURL(data.profile + '/'),
  effect() {}, on(name) { events.push(name); return () => {}; },
  tools:{register(spec) { tools.push(spec.name); return () => {}; }},
  inject(names, callback) {
    callback({
      effect(factory) { return factory(); },
      commands:{register(spec) { commands.push(spec.name); return () => {}; }},
      skills:{register(spec) { skills.push(spec.name); return () => {}; }},
      webServer:{register(spec) { routes.push(spec.path); return () => {}; }},
    });
  },
};
await bootstrap.apply(ctx);
console.log(JSON.stringify({commands,events,tools,skills,routes}));
""", {"profile": str(profile or self.profile)})

    def test_root_bundle_manifest_contains_source_and_no_automatic_install_scripts(self):
        package = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "agent-learning")
        self.assertEqual(package["dsh"]["bundle"]["patch"], "./dsh.patch.yml")
        self.assertTrue((SOURCE / package["exports"]["."]).is_file())
        self.assertTrue((SOURCE / package["exports"]["./client"]).is_file())
        self.assertNotIn("prepare", package.get("scripts", {}))
        self.assertNotIn("postinstall", package.get("scripts", {}))
        self.assertFalse((SOURCE / "runtime-release.json").exists())

    def test_fresh_apply_registers_setup_without_tool_gate_or_knowledge_creation(self):
        result = self.apply()
        self.assertIn("learning-init", result["commands"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["tools"], [])
        self.assertEqual(result["skills"], [])
        self.assertFalse(self.home.exists())
        self.assertFalse(Path(self.layout()["installation"]).exists())

    def test_profiles_use_distinct_stable_installations_and_runtime_stores(self):
        second = self.dsh / "profiles" / "tui"
        second.mkdir()
        left, right = self.layout(), self.layout(second)
        self.assertEqual(left, self.layout())
        self.assertNotEqual(left["installation"], right["installation"])
        self.assertNotEqual(left["runtime_store"], right["runtime_store"])
        for key in ("installation", "runtime_store"):
            self.assertTrue(Path(left[key]).is_relative_to(self.dsh))
            self.assertFalse(Path(left[key]).is_relative_to(self.profile))

    def test_unknown_profile_path_is_rejected_without_writes(self):
        external = self.root / "unrelated profile"
        external.mkdir()
        result = self.node("""
let error;
try { bootstrap.profileLayout(data.profile); } catch (e) { error = e.message; }
console.log(JSON.stringify({error}));
""", {"profile": str(external)})
        self.assertTrue(result.get("error"))
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse(self.home.exists())

    def test_missing_python_fails_before_export_or_database_creation(self):
        target = self.layout()["installation"]
        result = self.node("""
let error;
try { await bootstrap.initialize({profileDirectory:data.profile,
  workspace:data.workspace, python:data.python}); } catch (e) { error = e.message; }
console.log(JSON.stringify({error}));
""", {"profile": str(self.profile), "workspace": str(self.workspace),
        "python": str(self.root / "missing Python.exe")})
        self.assertTrue(result.get("error"))
        self.assertFalse(Path(target).exists())
        self.assertFalse(self.home.exists())

    def test_bad_runtime_receipt_does_not_activate_hooks_or_block_setup(self):
        target = Path(self.layout()["installation"])
        target.mkdir(parents=True)
        (target / "runtime-release.json").write_text("{bad json", encoding="utf-8")
        before = (target / "runtime-release.json").read_bytes()
        result = self.apply()
        self.assertIn("learning-init", result["commands"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["tools"], [])
        self.assertEqual((target / "runtime-release.json").read_bytes(), before)
        self.assertFalse(self.home.exists())

    def test_preaborted_initializer_does_not_spawn_or_create_an_export(self):
        result = self.node("""
const controller = new AbortController(); controller.abort();
let error;
try { await bootstrap.initialize({profileDirectory:data.profile, workspace:data.workspace,
  python:data.python, signal:controller.signal}); } catch (e) { error = e.message; }
console.log(JSON.stringify({error}));
""", {"profile": str(self.profile), "workspace": str(self.workspace),
        "python": os.environ.get("ALP_TEST_PYTHON", sys.executable)})
        self.assertIn("cancel", result.get("error", "").lower())
        self.assertFalse(Path(self.layout()["installation"]).exists())
        self.assertFalse(self.home.exists())

    def test_initializer_deadline_reports_unknown_completion_without_retry(self):
        result = self.node("""
const {EventEmitter} = await import('node:events');
const {syncBuiltinESMExports} = await import('node:module');
const childProcess = (await import('node:child_process')).default;
let calls = 0, kills = 0, error;
childProcess.spawn = () => {
  calls++;
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdout.setEncoding = child.stderr.setEncoding = () => {};
  child.kill = () => { kills++; return true; };
  if (calls === 1) queueMicrotask(() => {
    child.stdout.emit('data', JSON.stringify({executable:data.python,version:[3,14]}));
    child.emit('close',0);
  });
  return child;
};
syncBuiltinESMExports();
const timer = globalThis.setTimeout;
globalThis.setTimeout = (fn,ms) => timer(fn,Math.min(ms,25));
try { await bootstrap.initialize({profileDirectory:data.profile, workspace:data.workspace,
  python:data.python}); } catch (e) { error = e.message; }
console.log(JSON.stringify({error,calls,kills}));
""", {"profile": str(self.profile), "workspace": str(self.workspace),
        "python": os.environ.get("ALP_TEST_PYTHON", sys.executable)})
        self.assertEqual(result["calls"], 2, "one interpreter check and one install attempt only")
        self.assertEqual(result["kills"], 1)
        self.assertIn("unknown", result.get("error", "").lower())
        self.assertFalse(Path(self.layout()["installation"]).exists())
        self.assertFalse(self.home.exists())

    def test_plugin_unload_cancels_setup_even_with_independent_invocation_signal(self):
        result = self.node("""
const {EventEmitter} = await import('node:events');
const {syncBuiltinESMExports} = await import('node:module');
const childProcess = (await import('node:child_process')).default;
let command, calls = 0, kills = 0;
childProcess.spawn = () => {
  calls++;
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdout.setEncoding = child.stderr.setEncoding = () => {};
  child.kill = () => { kills++; return true; };
  return child;
};
syncBuiltinESMExports();
const timer = globalThis.setTimeout;
globalThis.setTimeout = (fn,ms) => timer(fn,Math.min(ms,100));
const disposers = [];
const ctx = {baseUrl:pathToFileURL(data.profile + '/'),
  effect(factory) { const dispose=factory(); if (typeof dispose === 'function') disposers.push(dispose); },
  inject(names,callback) { callback({commands:{register(spec) { command=spec; return () => {}; }}}); }};
await bootstrap.apply(ctx,{python:data.python});
const invocation = new AbortController();
const pending = command.handler({rawInput:'/learning-init "' + data.workspace + '"', signal:invocation.signal});
await new Promise(resolve => setImmediate(resolve));
for (const dispose of disposers) dispose();
const response = await pending;
console.log(JSON.stringify({response,calls,kills,invocationAborted:invocation.signal.aborted}));
""", {"profile": str(self.profile), "workspace": str(self.workspace),
        "python": os.environ.get("ALP_TEST_PYTHON", sys.executable)})
        self.assertEqual(result["calls"], 1, "only the pending interpreter probe should have started")
        self.assertEqual(result["kills"], 1)
        self.assertFalse(result["invocationAborted"], "the independent caller signal is still active")
        self.assertEqual(result["response"]["kind"], "error")
        self.assertIn("cancel", result["response"]["text"].lower())
        self.assertFalse(Path(self.layout()["installation"]).exists())
        self.assertFalse(self.home.exists())

    def test_native_setup_command_preserves_quoted_absolute_project_path(self):
        result = self.node("""
let command;
const ctx = {baseUrl:pathToFileURL(data.profile + '/'), effect() {},
  inject(names,callback) { callback({commands:{register(spec) { command=spec; }}}); }};
await bootstrap.apply(ctx,{python:data.python});
const response = await command.handler({rawInput:'/learning-init "' + data.workspace + '"'});
console.log(JSON.stringify({name:command.name,response,layout:bootstrap.profileLayout(data.profile)}));
""", {"profile": str(self.profile), "workspace": str(self.workspace),
        "python": os.environ.get("ALP_TEST_PYTHON", sys.executable)})
        self.assertEqual(result["name"], "learning-init")
        self.assertEqual(result["response"]["kind"], "success", result["response"])
        receipt = json.loads((Path(result["layout"]["installation"]) / "runtime-release.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(receipt["workspace"]), self.workspace)
        self.assertIn("reload", result["response"]["text"].lower())
        self.assertFalse(self.home.exists())

    def test_explicit_initialization_preserves_spaces_and_loads_exported_host_on_reload(self):
        result = self.initialize()
        self.assertTrue(result["initialized"])
        self.assertTrue(result["reload_required"])
        target = Path(result["installation"])
        receipt = json.loads((target / "runtime-release.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(receipt["workspace"]), self.workspace)
        self.assertEqual(Path(receipt["home"]), self.home)
        self.assertEqual(receipt["hook_mode"], "observe")
        self.assertEqual(receipt["target_host"], "dsh")
        self.assertTrue(Path(receipt["runtime_directory"]).is_dir())
        self.assertFalse(self.home.exists(), "install and apply must not open the knowledge database")
        reloaded = self.apply()
        self.assertIn("session/created", reloaded["events"])
        self.assertIn("tools/pre-execute", reloaded["events"])
        self.assertIn("learning_status", reloaded["tools"])
        self.assertIn("learning-worker", reloaded["skills"])
        self.assertIn("/agent-learning/panel-v2", reloaded["routes"])
        self.assertFalse(self.home.exists())

    def test_second_project_initialization_reuses_release_and_preserves_first_project(self):
        first = self.initialize()
        target = Path(first["installation"])
        before = json.loads((target / "runtime-release.json").read_text(encoding="utf-8"))
        second_workspace = self.root / "second project"
        second_workspace.mkdir()
        second = self.initialize(second_workspace)
        after = json.loads((target / "runtime-release.json").read_text(encoding="utf-8"))
        self.assertEqual(first["installation"], second["installation"])
        self.assertEqual(before["release_id"], after["release_id"])
        self.assertEqual(set(after["workspaces"]), {str(self.workspace), str(second_workspace)})
        self.assertFalse(self.home.exists())

    def test_bad_existing_installation_is_preserved_on_explicit_initialization(self):
        target = Path(self.layout()["installation"])
        target.mkdir(parents=True)
        marker = target / "user-custom-file.txt"
        marker.write_text("preserve", encoding="utf-8")
        result = self.node("""
let error;
try { await bootstrap.initialize({profileDirectory:data.profile,
  workspace:data.workspace, python:data.python}); } catch (e) { error = e.message; }
console.log(JSON.stringify({error}));
""", {"profile": str(self.profile), "workspace": str(self.workspace),
        "python": os.environ.get("ALP_TEST_PYTHON", sys.executable)})
        self.assertTrue(result.get("error"))
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
        self.assertFalse(self.home.exists())

    def test_cli_requires_explicit_project_and_reports_missing_python(self):
        args = [NODE, str(SOURCE / "scripts/dsh-init.mjs"), "--profile-directory", str(self.profile)]
        missing = subprocess.run(args, cwd=self.workspace, env=self.environment,
                                 capture_output=True, timeout=15)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("workspace", missing.stderr.decode("utf-8", "replace").lower())
        failed = subprocess.run(args + ["--workspace", str(self.workspace), "--python",
                                       str(self.root / "missing Python.exe")],
                                cwd=self.workspace, env=self.environment,
                                capture_output=True, timeout=15)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
