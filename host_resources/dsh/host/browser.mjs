// Temporary project browsers. Concurrent callers share one startup per mode.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";
import { join } from "node:path";

export function browserPathKey(canonical, platform = process.platform) {
  return platform === "win32" ? canonical.toLowerCase() : canonical;
}

export function createBrowserManager({ loadReceipt, runPython, environment = () => ({}),
  spawnProcess = spawn, canonicalWorkspace = value => realpathSync.native(value),
  platform = process.platform, startupTimeout = 10000 }) {
  const processes = new Map();
  let closed = false;
  const alive = entry => entry.child && typeof entry.child.exitCode !== "number" && !entry.child.killed;

  function cancel(key, entry) {
    entry.cancelled = true;
    entry.rejectStartup?.(new Error("Agent Learning browser startup cancelled"));
    try { entry.child?.kill(); } catch {}
    if (processes.get(key) === entry) processes.delete(key);
  }

  async function open(receipt, info, workspace, signal) {
    const result = await runPython(receipt.python,
      ["-B", "-c", "import sys, webbrowser; webbrowser.open(sys.argv[1])", info.browser_url],
      "", workspace, signal, 5000).catch(() => null);
    return result !== null && result.code === 0;
  }

  async function launch(entry, receipt, workspace, port, review, adminControl, signal) {
    const args = ["-B", join(receipt.runtime_directory, "scripts", "learning.py")];
    if (receipt.home) args.push("--home", receipt.home);
    args.push("--workspace", workspace, "--host", "dsh");
    await runPython(receipt.python, [...args, "status"], "", workspace, signal, 10000);
    if (entry.cancelled || closed || signal?.aborted) throw new Error("Agent Learning browser startup cancelled");
    const browserArgs = [...args, "browser", "--port", String(port)];
    if (review) browserArgs.push("--review");
    if (adminControl) browserArgs.push("--admin-control");
    return new Promise((resolve, reject) => {
      let settled = false, buffer = "", outputBytes = 0;
      const child = spawnProcess(receipt.python, browserArgs, { cwd: workspace,
        stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
        env: { ...process.env, ...environment(), PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" } });
      entry.child = child;
      const finish = (error, info) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        entry.rejectStartup = null;
        if (error) {
          try { child.kill(); } catch {}
          reject(error);
        } else { entry.info = info; resolve(info); }
      };
      const abort = () => finish(new Error("Agent Learning browser startup cancelled"));
      const timer = setTimeout(() => finish(new Error("Timed out starting Agent Learning knowledge browser")), startupTimeout);
      entry.rejectStartup = error => finish(error);
      signal?.addEventListener("abort", abort, { once: true });
      child.stdout.setEncoding?.("utf8");
      child.stdout.on("data", chunk => {
        if (settled) return;
        outputBytes += Buffer.byteLength(chunk);
        if (outputBytes > 131072) return finish(new Error("Browser startup response exceeds budget"));
        buffer += chunk.toString();
        const newline = buffer.indexOf("\n");
        if (newline < 0) return;
        try {
          const parsed = JSON.parse(buffer.slice(0, newline));
          const url = new URL(parsed.url);
          if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port
              || typeof parsed.project !== "string" || typeof parsed.access_token !== "string"
              || parsed.read_only !== (!review && !adminControl)) throw new Error();
          const browserUrl = adminControl ? parsed.admin_control_url : review ? parsed.review_url
            : parsed.url + "#token=" + encodeURIComponent(parsed.access_token);
          if (typeof browserUrl !== "string") throw new Error();
          const info = { project: parsed.project, workspace, url: parsed.url,
            browser_url: browserUrl,
            access_token: parsed.access_token, port: Number(url.port), pid: child.pid,
            read_only: Boolean(parsed.read_only), review, admin_control: adminControl };
          finish(null, info);
        } catch { finish(new Error("Invalid Agent Learning browser startup response")); }
      });
      // Drain diagnostics without retaining access codes or unbounded logs.
      child.stderr.on("data", () => {});
      child.on("error", () => finish(new Error("Agent Learning browser failed to start")));
      child.on("close", () => {
        finish(new Error("Agent Learning browser exited before startup completed"));
        if (processes.get(entry.key) === entry) processes.delete(entry.key);
      });
      if (signal?.aborted || entry.cancelled || closed) abort();
    });
  }

  async function manage(action, options, workspace, signal) {
    if (closed) throw new Error("Agent Learning browser manager is closed");
    if (!["start", "status", "stop"].includes(action)) throw new Error("learning_browser action must be start, status or stop");
    const receipt = loadReceipt();
    const targetWorkspace = canonicalWorkspace(workspace || receipt.workspace || process.cwd());
    const workspaceKey = browserPathKey(targetWorkspace, platform);
    const installation = createHash("sha256").update(JSON.stringify(receipt)).digest("hex");
    for (const [key, entry] of processes) {
      if (entry.workspaceKey === workspaceKey && entry.installation !== installation) cancel(key, entry);
      else if (entry.info && !alive(entry)) processes.delete(key);
    }
    const matching = () => [...processes.values()].filter(entry => entry.workspaceKey === workspaceKey);
    if (action === "stop") {
      const entries = matching();
      for (const entry of entries) cancel(entry.key, entry);
      return { status: "stopped", running: false, stopped: entries.length > 0, workspace: targetWorkspace };
    }
    if (action === "status") {
      const entry = matching().find(item => item.info && alive(item));
      return entry ? { status: "serving", running: true, ...entry.info }
        : { status: matching().length ? "starting" : "stopped", running: false, workspace: targetWorkspace, read_only: true };
    }
    const review = Boolean(options?.review), adminControl = Boolean(options?.adminControl);
    const port = Number.isInteger(options?.port) && options.port >= 0 && options.port <= 65535 ? options.port : 0;
    const key = JSON.stringify([workspaceKey, installation, review, adminControl]);
    let entry = processes.get(key), reused = Boolean(entry);
    if (entry && !entry.info) {
      await entry.promise;
      // Recheck installation identity and an explicit port after shared startup.
      return manage(action, options, targetWorkspace, signal);
    }
    if (entry && port !== 0 && entry.info.port !== port) { cancel(key, entry); entry = null; reused = false; }
    if (!entry) {
      entry = { key, workspaceKey, installation, cancelled: false, child: null, info: null, promise: null };
      // Reserve before the preflight await, so no concurrent start can escape tracking.
      processes.set(key, entry);
      entry.promise = launch(entry, receipt, targetWorkspace, port, review, adminControl, signal);
      try { await entry.promise; }
      catch (error) { if (processes.get(key) === entry) processes.delete(key); throw error; }
    }
    if (entry.cancelled || closed) throw new Error("Agent Learning browser startup cancelled");
    const opened = options?.open !== false ? await open(receipt, entry.info, targetWorkspace, signal) : false;
    return { status: "serving", running: true, reused, opened, ...entry.info };
  }

  return { manage, close() { closed = true; for (const [key, entry] of processes) cancel(key, entry); } };
}
