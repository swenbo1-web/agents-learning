import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function loadRuntimeReceipt() {
  const receiptPath = join(PLUGIN_ROOT, "runtime-release.json");
  return JSON.parse(readFileSync(receiptPath, "utf8"));
}

function extractTextFromBlocks(blocks) {
  if (typeof blocks === "string") return blocks;
  if (!Array.isArray(blocks)) return "";
  const parts = [];
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "reasoning" || block.type === "thinking") continue;
    if (block.type === "text" && typeof block.text === "string") {
      parts.push(block.text);
    }
  }
  return parts.join("\n");
}

export const name = "agent-learning-host";
export const inject = {
  optional: ["subprocess", "systemPrompt", "sessionQuery", "agents"],
};

export function apply(ctx) {
  const sessionContexts = new Map();
  const sessionMeta = new Map();

  async function dispatchEvent(eventName, payload, signal) {
    if (!ctx.subprocess) return {};
    let receipt;
    try {
      receipt = loadRuntimeReceipt();
    } catch {
      return {};
    }
    const dispatcher = join(receipt.runtime_directory, "hooks", "dsh_dispatch.py");
    const argv = [receipt.python, "-B", dispatcher, "--event", eventName];
    if (receipt.hook_mode) {
      argv.push("--hook-mode", receipt.hook_mode);
    }
    if (receipt.workspace) {
      argv.push("--workspace", receipt.workspace);
    }
    if (receipt.home) {
      argv.push("--home", receipt.home);
    }
    const cwd = payload.cwd || receipt.workspace || PLUGIN_ROOT;
    const handle = ctx.subprocess.spawn({
      argv,
      cwd,
      stdio: {
        stdin: { data: JSON.stringify(payload) },
        stdout: { maxBytes: 256 * 1024 },
        stderr: { maxBytes: 64 * 1024 },
      },
      graceMs: 3000,
      signal,
    });
    const outcome = await handle.done;
    if (outcome.exitCode !== 0 || !handle.collected.stdout) {
      return {};
    }
    const out = handle.collected.stdout.readFrom(0).text.trim();
    if (!out) return {};
    try {
      return JSON.parse(out);
    } catch {
      return {};
    }
  }

  if (ctx.systemPrompt) {
    ctx.systemPrompt.context({
      name: "agent-learning-context",
      order: 850,
      text: (assembleCtx) => {
        const scope = assembleCtx?.scope;
        const id = scope && typeof scope === "object" && "id" in scope ? scope.id : undefined;
        return (id && sessionContexts.get(id)) || "";
      },
    });
  }

  ctx.on("session/created", (session) => {
    if (!session || !session.header) return;
    sessionMeta.set(session.id, {
      cwd: session.header.cwd,
      parentSession: session.header.parentSession,
      origin: session.header.origin,
      requestContext: typeof session.requestContext === "function" ? session.requestContext() : undefined,
    });
  });

  ctx.on("agent/created", async (payload) => {
    const agent = payload?.agent;
    if (!agent?.id) return;
    const meta = sessionMeta.get(agent.id) || {};
    const eventKind = meta.origin === "subagent" ? "SubagentStart" : "SessionStart";
    const result = await dispatchEvent(
      eventKind,
      {
        sessionId: meta.parentSession || agent.id,
        agentId: meta.origin === "subagent" ? agent.id : undefined,
        cwd: meta.cwd,
        source: payload.source || "startup",
        requestContext: meta.requestContext,
      },
      payload.signal,
    );
    const additional = result?.hookSpecificOutput?.additionalContext;
    if (additional) {
      sessionContexts.set(agent.id, additional);
    }
  });

  ctx.on("agent/pre-step", async (payload, next) => {
    const agent = payload?.agent;
    if (agent?.id && payload.step === 0) {
      const meta = sessionMeta.get(agent.id) || {};
      const promptText = Array.isArray(payload.messages)
        ? payload.messages.map((m) => extractTextFromBlocks(m?.content)).filter(Boolean).join("\n")
        : "";
      const result = await dispatchEvent(
        "UserPromptSubmit",
        {
          sessionId: meta.parentSession || agent.id,
          agentId: meta.origin === "subagent" ? agent.id : undefined,
          cwd: meta.cwd,
          turn: payload.turn,
          prompt: promptText,
          requestContext: meta.requestContext,
        },
        payload.signal,
      );
      const additional = result?.hookSpecificOutput?.additionalContext;
      if (additional) {
        sessionContexts.set(agent.id, additional);
      }
    }
    return next();
  });

  ctx.on("tools/pre-execute", async (exec, next) => {
    const agent = exec?.agent;
    if (!agent?.id) return next();
    const meta = sessionMeta.get(agent.id) || {};
    const result = await dispatchEvent(
      "PreToolUse",
      {
        sessionId: meta.parentSession || agent.id,
        agentId: meta.origin === "subagent" ? agent.id : undefined,
        cwd: meta.cwd,
        callId: exec.callId,
        name: exec.name,
        arguments: exec.arguments || {},
      },
      exec.signal,
    );
    if (result?.decision === "deny" || result?.hookSpecificOutput?.permissionDecision === "deny") {
      return {
        kind: "deny",
        reason: result.reason || result?.hookSpecificOutput?.permissionDecisionReason || "Denied by Agent Learning policy",
      };
    }
    const additional = result?.hookSpecificOutput?.additionalContext;
    if (additional) {
      sessionContexts.set(agent.id, additional);
    }
    return next();
  });

  ctx.on("tools/post-execute", async (exec, toolResult, next) => {
    const agent = exec?.agent;
    if (agent?.id) {
      const meta = sessionMeta.get(agent.id) || {};
      await dispatchEvent(
        "PostToolUse",
        {
          sessionId: meta.parentSession || agent.id,
          agentId: meta.origin === "subagent" ? agent.id : undefined,
          cwd: meta.cwd,
          callId: exec.callId,
          name: exec.name,
          arguments: exec.arguments || {},
          result: toolResult,
        },
        exec.signal,
      );
    }
    return next();
  });

  ctx.on("session/event", (session, event) => {
    if (!session?.id || !event) return;
    if (event.type === "request/context" && event.data) {
      const prev = sessionMeta.get(session.id) || { cwd: session.header?.cwd };
      sessionMeta.set(session.id, { ...prev, requestContext: event.data });
    } else if (event.type === "turn/end" && event.data) {
      const meta = sessionMeta.get(session.id) || { cwd: session.header?.cwd };
      const reasonKind = event.data.reason?.kind;
      const hookEvent = reasonKind === "aborted" || reasonKind === "interrupted" ? "Interrupt" : "Stop";
      void dispatchEvent(hookEvent, {
        sessionId: meta.parentSession || session.id,
        agentId: meta.origin === "subagent" ? session.id : undefined,
        cwd: meta.cwd,
        turn: event.data.turn,
        requestContext: meta.requestContext,
      });
    }
  });
}
