// Antigravity App UI adapter. Authority and write decisions remain in Python.
import { readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";

const HERE = dirname(fileURLToPath(import.meta.url));
const MAX_BODY = 16384;
const MAX_OUTPUT = 131072;
const ACTIONS = new Set(["status", "open_browser", "open_review", "set_admin_preset", "archive_history"]);
const PRESETS = new Set(["off", "minimal", "light", "balanced", "active"]);
const EXPECTED = new Set(["expected_policy_version", "expected_project_budget_version", "expected_settings_version", "expected_session_epoch", "expected_governance_epoch"]);

function plain(value) {
  return value && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function text(value, maximum) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum && !value.includes("\0") && value.trim() === value;
}

function pathKey(value) {
  const resolved = realpathSync(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

export function loadInstallation(root) {
  const directory = realpathSync(root);
  const raw = readFileSync(join(directory, "runtime-release.json"), "utf8");
  const receipt = JSON.parse(raw);
  if (!plain(receipt) || receipt.target_host !== "antigravity") throw new Error("Antigravity installation required");
  for (const key of ["python", "runtime_directory", "workspace", "home"]) {
    if (!text(receipt[key], 8192) || !isAbsolute(receipt[key])) throw new Error("Invalid installation binding");
  }
  if (!statSync(receipt.workspace).isDirectory() || !statSync(receipt.runtime_directory).isDirectory()
      || !statSync(receipt.python).isFile() || !statSync(join(receipt.runtime_directory, "scripts", "learning.py")).isFile()) {
    throw new Error("Installed runtime is unavailable");
  }
  const fingerprint = createHash("sha256").update(raw).digest("hex");
  return { directory, receipt, fingerprint, workspaceKey: pathKey(receipt.workspace) };
}

export function resolveWorkspace(uris, installation) {
  if (!Array.isArray(uris) || uris.length !== 1 || !text(uris[0], 16384)) return null;
  try {
    const uri = new URL(uris[0]);
    if (uri.protocol !== "file:" || (uri.hostname && uri.hostname !== "localhost") || uri.search || uri.hash) return null;
    const workspace = realpathSync(fileURLToPath(uri));
    return statSync(workspace).isDirectory() && pathKey(workspace) === installation.workspaceKey ? workspace : null;
  } catch { return null; }
}

export function validateInput(method, data) {
  if (!plain(data) || Buffer.byteLength(JSON.stringify(data), "utf8") > MAX_BODY) throw new Error("Invalid panel request");
  const allowed = new Set(["workspace_uris", "conversation_id"]);
  if (method === "action") for (const key of ["capability", "action", "request_id", "expected", "preset", "confirm"]) allowed.add(key);
  if (Object.keys(data).some((key) => !allowed.has(key)) || !text(data.conversation_id, 500)
      || !Array.isArray(data.workspace_uris) || data.workspace_uris.length > 8
      || data.workspace_uris.some((uri) => !text(uri, 16384))) throw new Error("Invalid host context");
  if (method === "action") {
    if (!text(data.capability, 512) || !ACTIONS.has(data.action)) throw new Error("Invalid panel action");
    if (data.request_id !== undefined && !text(data.request_id, 200)) throw new Error("Invalid request identity");
    if (data.action !== "status" && !text(data.request_id, 200)) throw new Error("Panel action identity is required");
    if (data.confirm !== undefined && typeof data.confirm !== "boolean") throw new Error("Invalid confirmation");
    if (data.expected !== undefined && (!plain(data.expected) || Object.keys(data.expected).length !== EXPECTED.size
        || Object.keys(data.expected).some((key) => !EXPECTED.has(key) || !Number.isSafeInteger(data.expected[key]) || data.expected[key] < 0))) {
      throw new Error("Invalid settings snapshot");
    }
    if (data.action === "set_admin_preset" && (!PRESETS.has(data.preset) || !data.expected || !text(data.request_id, 200) || data.confirm !== true)) {
      throw new Error("Confirm administrator settings before saving");
    }
    if (data.action === "archive_history" && (data.confirm !== true || !text(data.request_id, 200))) throw new Error("Confirm history archival before starting");
    if (data.preset !== undefined && data.action !== "set_admin_preset") throw new Error("Unexpected preset");
  }
  return data;
}

export class PanelBridge {
  constructor(installation, spawnProcess = spawn) {
    this.installation = installation;
    this.spawnProcess = spawnProcess;
    this.child = null;
    this.pending = new Map();
    this.closed = false;
    this.buffer = "";
  }

  assertCurrent() {
    const current = loadInstallation(this.installation.directory);
    if (current.fingerprint !== this.installation.fingerprint || current.workspaceKey !== this.installation.workspaceKey) {
      throw new Error("Installation changed; reopen the panel after updating");
    }
  }

  start() {
    if (this.closed) throw new Error("Panel bridge is closed");
    this.assertCurrent();
    if (this.child) return;
    const receipt = this.installation.receipt;
    const child = this.spawnProcess(receipt.python, ["-B", join(receipt.runtime_directory, "scripts", "learning.py"),
      "panel-serve", "--installation", this.installation.directory], {
      cwd: this.installation.directory, stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" },
    });
    this.child = child;
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      this.buffer += chunk;
      if (Buffer.byteLength(this.buffer, "utf8") > MAX_OUTPUT) return this.close();
      let newline;
      while ((newline = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, newline);
        this.buffer = this.buffer.slice(newline + 1);
        let response;
        try { response = JSON.parse(line); } catch { this.close(); return; }
        const pending = this.pending.get(response.id);
        if (!pending) continue;
        this.pending.delete(response.id);
        clearTimeout(pending.timer);
        if (response.error) pending.reject(new Error("Panel request failed; refresh the project connection"));
        else if (!plain(response.result)) pending.reject(new Error("Invalid panel response"));
        else pending.resolve(response.result);
      }
    });
    // Never forward runtime output, capability keys or host credentials to logs.
    child.stderr.on("data", () => {});
    child.on("error", () => this.close());
    child.on("exit", () => this.close());
  }

  request(method, params) {
    this.start();
    if (this.pending.size >= 8) return Promise.reject(new Error("Panel is busy; wait for the current action"));
    return new Promise((resolve, reject) => {
      const id = randomUUID();
      const timer = setTimeout(() => this.close(), method === "action" && params.action === "archive_history" ? 210000 : 30000);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + "\n", (error) => { if (error) this.close(); });
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    const child = this.child;
    this.child = null;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error("Panel connection ended; reopen the panel"));
    }
    this.pending.clear();
    if (child) {
      child.stdin.end();
      if (child.exitCode === null) child.kill();
    }
  }
}

export function createController(installation, bridge) {
  return async function handle(method, input) {
    const data = validateInput(method, input);
    bridge.assertCurrent();
    const workspace = resolveWorkspace(data.workspace_uris, installation);
    if (!workspace) return { visible: false, reason: "workspace_not_bound" };
    const { workspace_uris, ...params } = data;
    return bridge.request(method, { ...params, workspace });
  };
}

export function guardRequests(server, tokenPresent) {
  if (!tokenPresent) throw new Error("Antigravity UI authentication is unavailable");
  const handlers = server.listeners("request");
  server.removeAllListeners("request");
  server.on("request", (request, response) => {
    let path;
    try { path = new URL(request.url, "http://127.0.0.1").pathname; } catch { response.writeHead(400); response.end(); return; }
    const page = request.method === "GET" && ["/", "/preload.js"].includes(path);
    const api = request.method === "POST" && ["/api/bootstrap", "/api/action"].includes(path);
    const length = request.headers["content-length"];
    if ((!page && !api) || (api && (request.headers["transfer-encoding"] || typeof length !== "string"
        || !/^\d+$/.test(length) || Number(length) > MAX_BODY || Number(length) < 1
        || request.headers["content-type"]?.split(";", 1)[0].trim().toLowerCase() !== "application/json"))) {
      response.writeHead(400, { "Content-Type": "application/json" });
      response.end('{"error":"unsupported panel request"}');
      return;
    }
    for (const handler of handlers) handler.call(server, request, response);
  });
}

export async function startPanel({ SidecarApp, Response }, root = join(HERE, "..", ".."), spawnProcess = spawn) {
  if (!process.env.ANTIGRAVITY_SIDECAR_UI_TOKEN) throw new Error("Antigravity UI authentication is unavailable");
  const installation = loadInstallation(root);
  const bridge = new PanelBridge(installation, spawnProcess);
  const handle = createController(installation, bridge);
  const app = new SidecarApp();
  app.page("/", () => new Response(readFileSync(join(HERE, "index.html"), "utf8"), {
    contentType: "text/html; charset=utf-8", headers: { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" },
  }));
  for (const method of ["bootstrap", "action"]) {
    app.api("/api/" + method, async (data) => {
      try { return await handle(method, data); }
      catch { return { visible: false, reason: "connection_unavailable" }; }
    });
  }
  const server = await app.run();
  guardRequests(server, true);
  const close = () => { bridge.close(); app.close(); };
  process.once("SIGTERM", close);
  process.once("SIGINT", close);
  process.once("exit", () => bridge.close());
  server.once("close", () => bridge.close());
  return { app, bridge, server, close };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === realpathSync(process.argv[1])) {
  try { await startPanel(await import("sidecar_sdk")); }
  catch { process.stderr.write("Agent Learning UI unavailable; verify its installed runtime and host support.\n"); process.exitCode = 1; }
}
