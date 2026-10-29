// Repository installs have no developer-generated runtime receipt. Only an
// explicit initializer creates a local export; startup merely verifies it.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const VERSION = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8")).version;
const pending = new Set();
const canonical = (value) => process.platform === "win32" ? value.toLowerCase() : value;

function absoluteDirectory(value, label) {
  if (typeof value !== "string" || !isAbsolute(value)) throw new Error(`${label} must be an absolute existing directory`);
  const path = realpathSync.native(value);
  if (!statSync(path).isDirectory()) throw new Error(`${label} must be an existing directory`);
  return path;
}

export function profileLayout(profileDirectory, environment = process.env) {
  const directory = absoluteDirectory(profileDirectory, "DSH profile directory");
  const configured = environment.DSH_HOME?.trim();
  const home = configured ? (configured === "~" ? homedir()
    : configured.startsWith("~/") || configured.startsWith("~\\") ? join(homedir(), configured.slice(2)) : configured)
    : join(homedir(), ".dsh");
  const profiles = absoluteDirectory(join(home, "profiles"), "DSH profiles directory");
  if (canonical(dirname(directory)) !== canonical(profiles)) {
    throw new Error("DSH profile must be a direct child of DSH_HOME/profiles; an unknown host profile cannot be initialized");
  }
  const identity = createHash("sha256").update(canonical(directory)).digest("hex").slice(0, 12);
  const base = join(dirname(profiles), "agent-learning", `${basename(directory)}-${identity}`);
  return { profile_directory: directory, profile: basename(directory), base,
    installation: join(base, "plugin"), runtime_store: join(base, "runtime-releases") };
}

function processResult(command, args, { cwd = ROOT, environment = process.env, signal, timeout = 30000 } = {}) {
  return new Promise((resolveResult, reject) => {
    let child, timer, settled = false, stdout = "", stderr = "", bytes = 0;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
      if (error) reject(error); else resolveResult(value);
    };
    const stop = (message) => {
      finish(new Error(message));
      try { child?.kill(); } catch {}
    };
    const cancel = () => stop("Initialization canceled; check installation state before retrying");
    if (signal?.aborted) return cancel();
    try {
      child = spawn(command, args, { cwd, env: { ...environment, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" },
        shell: false, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
      signal?.addEventListener("abort", cancel, { once: true });
      timer = setTimeout(() => stop("Initialization process deadline reached; completion is unknown, inspect before retrying"), timeout);
      const append = (kind, chunk) => {
        bytes += Buffer.byteLength(chunk, "utf8");
        if (bytes > 1024 * 1024) return stop("Initializer output exceeded its safety limit; inspect installation before retrying");
        if (kind === "out") stdout += chunk; else stderr += chunk;
      };
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", chunk => append("out", chunk));
      child.stderr.on("data", chunk => append("err", chunk));
      child.on("error", error => finish(error));
      child.on("close", code => code === 0 ? finish(null, stdout) : finish(new Error((stderr || stdout).trim() || `Process exited with ${code}`)));
    } catch (error) { finish(error); }
  });
}

async function pythonExecutable(requested, options) {
  if (requested !== undefined && (typeof requested !== "string" || !isAbsolute(requested))) {
    throw new Error("Python must be an absolute executable path");
  }
  const candidates = requested ? [[requested, []]] : process.platform === "win32"
    ? [["python", []], ["python3", []], ["py", ["-3"]]] : [["python3", []], ["python", []]];
  for (const [command, prefix] of candidates) {
    try {
      const text = await processResult(command, [...prefix, "-I", "-S", "-B", "-c",
        "import json,sys; print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:2])}))"],
      { ...options, timeout: 10000 });
      const result = JSON.parse(text);
      if (isAbsolute(result.executable) && result.version?.[0] === 3 && result.version?.[1] >= 10) return result.executable;
    } catch (error) {
      if (options.signal?.aborted || requested) throw error;
    }
  }
  throw new Error("Install an independent Python 3.10+ interpreter, then configure its absolute path using --python or plugin config.python");
}

function receiptAt(layout) {
  const receipt = JSON.parse(readFileSync(join(layout.installation, "runtime-release.json"), "utf8").replace(/^\uFEFF/, ""));
  if (receipt?.target_host !== "dsh" || typeof receipt.python !== "string" || !isAbsolute(receipt.python)) {
    throw new Error("Existing installation receipt is invalid; preserve it and use installation-check before repairing");
  }
  return receipt;
}

export async function initialize({ profileDirectory, workspace, python, signal, environment = process.env }) {
  const layout = profileLayout(profileDirectory, environment);
  const project = absoluteDirectory(workspace, "Project workspace");
  if (pending.has(layout.installation)) throw new Error("This profile is already being initialized; wait for its result");
  pending.add(layout.installation);
  try {
    const existing = existsSync(layout.installation);
    const receipt = existing ? receiptAt(layout) : null;
    if (receipt && python && canonical(resolve(python)) !== canonical(resolve(receipt.python))) {
      throw new Error("An existing installation preserves its interpreter; use repair-python explicitly to change it");
    }
    if (receipt && environment.ALP_HOME && receipt.home
        && canonical(resolve(environment.ALP_HOME)) !== canonical(resolve(receipt.home))) {
      throw new Error("ALP_HOME differs from this profile's installed knowledge directory; use the original directory when upgrading");
    }
    const options = { environment, signal };
    const executable = await pythonExecutable(receipt?.python || python, options);
    const argv = ["-B", join(ROOT, "scripts", "learning.py"), "--workspace", project];
    if (!receipt) {
      const dataHome = environment.ALP_HOME || (process.platform === "win32" && environment.LOCALAPPDATA
        ? join(environment.LOCALAPPDATA, "AgentLearning") : join(homedir(), ".local", "share", "agent-learning"));
      if (!isAbsolute(dataHome)) throw new Error("ALP_HOME must be an absolute directory");
      argv.push("--home", dataHome);
    }
    argv.push("install", layout.installation, "--host", "dsh", "--runtime-store", layout.runtime_store);
    if (receipt) argv.push("--upgrade"); else argv.push("--hook-mode", "observe");
    const installed = JSON.parse(await processResult(executable, argv, { ...options, timeout: 120000 }));
    return { ...installed, initialized: true, reload_required: true,
      profile_directory: layout.profile_directory, installation: layout.installation, runtime_store: layout.runtime_store };
  } finally { pending.delete(layout.installation); }
}

function workspaceInput(raw) {
  let input = String(raw || "").trim().replace(/^\/?learning-init(?:\s+|$)/i, "").trim();
  if ((input.startsWith('"') && input.endsWith('"')) || (input.startsWith("'") && input.endsWith("'"))) input = input.slice(1, -1);
  return input;
}

function linkedSignals(...values) {
  const controller = new AbortController();
  const sources = [...new Set(values.filter(Boolean))];
  const abort = () => controller.abort();
  for (const signal of sources) {
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  return { signal: controller.signal,
    dispose() { for (const signal of sources) signal.removeEventListener("abort", abort); } };
}

export async function apply(ctx, config = {}) {
  let layout, problem, disposed = false;
  const controller = new AbortController();
  ctx.effect(() => () => { disposed = true; controller.abort(); });
  try { layout = profileLayout(fileURLToPath(ctx.baseUrl)); }
  catch (error) { problem = error.message; }
  ctx.inject(["commands"], scope => {
    const dispose = scope.commands.register({ name: "learning-init",
      description: "Initialize or update Agent Learning for an explicitly selected project (no model calls)",
      input: { hint: "<absolute project path>" }, recordInput: true,
      handler: async invocation => {
        const linked = linkedSignals(invocation?.signal, controller.signal);
        try {
          if (!layout) throw new Error(problem);
          const result = await initialize({ profileDirectory: layout.profile_directory,
            workspace: workspaceInput(invocation?.rawInput), python: config.python,
            signal: linked.signal });
          return { kind: "success", text: `Agent Learning initialized for ${result.workspace || workspaceInput(invocation?.rawInput)}. `
            + `Reload this plugin or restart the same DSH profile. Installation: ${result.installation}. `
            + "Administrator, history import and automatic publication were not enabled." };
        } catch (error) { return { kind: "error", text: `Agent Learning initialization: ${error.message}` }; }
        finally { linked.dispose(); }
      } });
    if (typeof dispose === "function") ctx.effect(() => dispose);
  });
  if (!layout || !existsSync(layout.installation)) return;
  try {
    const receipt = receiptAt(layout);
    const checked = JSON.parse(await processResult(receipt.python,
      ["-B", join(ROOT, "scripts", "learning.py"), "installation-check", "--installation", layout.installation],
      { signal: controller.signal }));
    if (disposed) return;
    if (checked.status !== "healthy" || checked.version !== VERSION || checked.target_host !== "dsh"
        || checked.installation_integrity !== "verified") throw new Error("The local export needs initialization or upgrade");
    const native = await import(pathToFileURL(join(layout.installation, "host", "index.mjs")).href);
    if (!disposed) return native.apply(ctx, config);
  } catch (error) {
    // An uninitialized/broken optional bridge must not install a tool guard.
    ctx.logger?.warn?.(`Agent Learning is not initialized for this profile: ${error.message}. Use /learning-init <absolute project path>.`);
  }
}
