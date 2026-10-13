import { spawn } from "node:child_process";
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

function runPythonProcess(pythonExe, args, stdinText, cwd, signal, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonExe, args, {
      cwd,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      signal,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" },
    });
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {}
    }, timeoutMs);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
    if (stdinText !== undefined && stdinText !== null) {
      child.stdin.write(stdinText, "utf8");
    }
    child.stdin.end();
  });
}

const GENERIC_JSON_OUTPUT = {
  schema: { type: "object", additionalProperties: true },
  render: (_args, value) => [{ type: "text", text: JSON.stringify(value, null, 2) }],
};

const WORKER_TOOL_SPECS = [
  {
    name: "knowledge_search",
    description:
      "检索本项目正式知识的有限预览；完整正文使用 knowledge_read。预算是 JSON 正文字符数，默认 6000。",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string" },
        limit: { type: "integer" },
        filters: { type: "object", additionalProperties: false, properties: {
          category: { type: "string", enum: ["mixed", "operation", "professional", "project", "unknown"] },
          knowledge_kind: { type: "string", enum: ["fact", "pattern", "solution"] },
          tool: { type: "string" },
          tool_version: { type: "string" },
          error_code: { type: "string" },
          environment: { type: "string" },
          verification: { type: "string" },
        }},
        context_handle: { type: "string" },
        max_chars: { type: "integer" },
      },
      required: ["query"],
      additionalProperties: false,
    },
    readOnly: true,
  },
  {
    name: "knowledge_read",
    description:
      "分页读取当前有效知识正文、适用条件或限制；section 默认 content，续页保持 section 并传 next_offset 与 expected_revision。",
    parameters: {
      type: "object",
      properties: {
        knowledge_id: { type: "string" },
        context_handle: { type: "string" },
        max_chars: { type: "integer" },
        expected_revision: { type: "integer" },
        section: { type: "string", enum: ["content", "applicability", "limitations"] },
        offset: { type: "integer" },
      },
      required: ["knowledge_id"],
      additionalProperties: false,
    },
    readOnly: true,
  },
  {
    name: "work_memory_search",
    description:
      "检索本项目已观察的工作历史索引（1.0.0）；不是正式解法或执行授权。可按目标文件 file_path 筛选，默认 3000 字符。",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string" },
        file_path: { type: "string" },
        context_handle: { type: "string" },
        limit: { type: "integer" },
        max_chars: { type: "integer" },
      },
      additionalProperties: false,
    },
    readOnly: true,
  },
  {
    name: "work_memory_read",
    description:
      "绑定当前会话后，分页读取工作观察摘要 (summary) 或可见证据投影 (evidence)。续页必须保持 section 和 expected_revision。",
    parameters: {
      type: "object",
      properties: {
        memory_id: { type: "string" },
        context_handle: { type: "string" },
        expected_revision: { type: "integer" },
        offset: { type: "integer" },
        max_chars: { type: "integer" },
        section: { type: "string", enum: ["summary", "evidence"] },
      },
      required: ["memory_id"],
      additionalProperties: false,
    },
    readOnly: true,
  },
  {
    name: "session_context",
    description:
      "读取已验证的宿主会话与任务版本；未绑定时只返回项目状态，不推断其他会话。",
    parameters: {
      type: "object",
      properties: {
        context_handle: { type: "string" },
      },
      additionalProperties: false,
    },
    readOnly: true,
  },
  {
    name: "task_update",
    description: "保存当前明确的任务范围和状态；不增加执行权限。",
    parameters: {
      type: "object",
      properties: {
        context_handle: { type: "string" },
        conversation_id: { type: "string" },
        goal: { type: "string" },
        scope: { type: "object", additionalProperties: true },
        expected_revision: { type: "integer" },
        status: { type: "string", enum: ["active", "cancelled", "delivered", "interrupted", "paused"] },
        new_task: { type: "boolean" },
        expected_task_id: { type: "string" },
        expected_write_version: { type: "integer" },
        resume: { type: "boolean" },
      },
      required: ["goal"],
      additionalProperties: false,
    },
    readOnly: false,
  },
  {
    name: "case_feedback",
    description: "补充专业问题、用户纠正或关键依据；常规工具事件由 Hook 采集。",
    parameters: {
      type: "object",
      properties: {
        context_handle: { type: "string" },
        conversation_id: { type: "string" },
        summary: { type: "string" },
        category: { type: "string", enum: ["mixed", "operation", "professional", "project", "unknown"] },
        event_refs: { type: "array", items: { type: "string" } },
        resolution: { type: "string" },
        knowledge_id: { type: "string" },
        knowledge_revision: { type: "integer" },
        feedback_kind: { type: "string", enum: ["observation", "counterexample"] },
        idempotency_key: { type: "string" },
        expected_task_id: { type: "string" },
        expected_task_revision: { type: "integer" },
      },
      required: ["summary"],
      additionalProperties: false,
    },
    readOnly: false,
  },
  {
    name: "knowledge_use",
    description: "简短声明实际采用的知识修订，可关联本任务工具操作；声明和工具观察不证明因果或用户验收。",
    parameters: {
      type: "object",
      properties: {
        context_handle: { type: "string" },
        knowledge_id: { type: "string" },
        knowledge_revision: { type: "integer" },
        expected_task_id: { type: "string" },
        expected_task_revision: { type: "integer" },
        expected_write_version: { type: "integer" },
        operation_id: { type: "string" },
        idempotency_key: { type: "string" },
      },
      required: [
        "knowledge_id",
        "knowledge_revision",
        "expected_task_id",
        "expected_task_revision",
        "expected_write_version",
      ],
      additionalProperties: false,
    },
    readOnly: false,
  },
  {
    name: "learning_status",
    description: "查看本项目记录、工作历史索引和待治理状态。",
    parameters: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    readOnly: true,
  },
];

export const name = "agent-learning";
export const inject = ["tools", "systemPrompt"];

export function apply(ctx) {
  const sessionContexts = new Map();
  const sessionHandles = new Map();
  const sessionMeta = new Map();

  function captureHandleFromText(agentId, text) {
    if (!agentId || typeof text !== "string") return;
    const match = text.match(/context_handle=([^\s;]+)/);
    if (match && match[1]) {
      sessionHandles.set(agentId, match[1]);
    }
  }

  async function dispatchEvent(eventName, payload, signal) {
    let receipt;
    try {
      receipt = loadRuntimeReceipt();
    } catch {
      return {};
    }
    const dispatcher = join(receipt.runtime_directory, "hooks", "dsh_dispatch.py");
    const argv = ["-B", dispatcher, "--event", eventName];
    if (receipt.hook_mode) {
      argv.push("--hook-mode", receipt.hook_mode);
    }
    if (receipt.workspace) {
      argv.push("--workspace", receipt.workspace);
    }
    if (receipt.home) {
      argv.push("--home", receipt.home);
    }
    const cwd = payload.cwd || receipt.workspace || process.cwd();
    try {
      const { code, stdout } = await runPythonProcess(
        receipt.python,
        argv,
        JSON.stringify(payload),
        cwd,
        signal,
        10000,
      );
      if (code !== 0 || !stdout.trim()) return {};
      return JSON.parse(stdout.trim());
    } catch {
      return {};
    }
  }

  async function invokeMcpTool(toolName, rawArgs, exec) {
    const receipt = loadRuntimeReceipt();
    const agentId = exec?.agent?.id;
    const meta = (agentId && sessionMeta.get(agentId)) || {};
    const workspace = meta.cwd || receipt.workspace || process.cwd();
    const args = { ...(rawArgs || {}) };
    if (!args.context_handle && agentId && sessionHandles.has(agentId)) {
      args.context_handle = sessionHandles.get(agentId);
    }
    const inlineRunner = [
      "import json, sys; from pathlib import Path;",
      "sys.path.insert(0, sys.argv[1]);",
      "from agent_learning.release import validate_release_root; validate_release_root(Path(sys.argv[1]));",
      "from agent_learning.runtime import Runtime, encode; from agent_learning.mcp import Server;",
      "payload = json.loads(sys.stdin.read());",
      "r = Runtime(home=payload.get('home'), host='dsh');",
      "try:",
      "    p = r.project(payload['workspace']);",
      "    s = Server(r, p, 'worker', workspace=payload['workspace']);",
      "    res = s.call(payload['tool'], payload['args']);",
      "    print(encode({'ok': True, 'result': res}));",
      "except Exception as e:",
      "    print(encode({'ok': False, 'error': str(e)}));",
      "finally:",
      "    r.close()",
    ].join(" ");
    const inputPayload = JSON.stringify({
      home: receipt.home || null,
      workspace,
      tool: toolName,
      args,
    });
    const { code, stdout, stderr } = await runPythonProcess(
      receipt.python,
      ["-B", "-c", inlineRunner, receipt.runtime_directory],
      inputPayload,
      workspace,
      exec?.signal,
      15000,
    );
    if (code !== 0 || !stdout.trim()) {
      throw new Error(`Agent Learning tool ${toolName} failed: ${(stderr || stdout).trim()}`);
    }
    const parsed = JSON.parse(stdout.trim());
    if (!parsed.ok) {
      throw new Error(parsed.error || `Agent Learning tool ${toolName} rejected call`);
    }
    return parsed.result;
  }

  for (const spec of WORKER_TOOL_SPECS) {
    ctx.tools.register({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      output: GENERIC_JSON_OUTPUT,
      timeoutMs: 20000,
      isConcurrencySafe: () => spec.readOnly,
      presentCall: (args) => ({
        card: "generic",
        title: spec.name,
        kind: spec.readOnly ? "read" : "other",
        rawInput: args,
      }),
      async execute(args, exec) {
        return invokeMcpTool(spec.name, args, exec);
      },
    });
  }

  if (typeof ctx.inject === "function") {
    ctx.inject(["skills"], (scope) => {
      for (const [skillName, desc] of [
        [
          "learning-worker",
          "Use Agent Learning project knowledge, work history, file associations and task state in DeepSeek Harness (DSH).",
        ],
        [
          "learning-admin",
          "Review bounded Agent Learning evidence and propose knowledge candidates, governance or optional work summaries in DSH.",
        ],
        [
          "learning-history",
          "用户明确要求为已有项目吸收历史文档、源码和选定的 DSH 对话记录时，操作独立有限历史批次。",
        ],
      ]) {
        try {
          const skillPath = join(PLUGIN_ROOT, "skills", skillName, "SKILL.md");
          const content = readFileSync(skillPath, "utf8");
          scope.skills.register({
            name: skillName,
            description: desc,
            content,
            source: "bundled",
            resourceBase: { kind: "directory", path: dirname(skillPath) },
          });
        } catch {}
      }
    });
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
      captureHandleFromText(agent.id, additional);
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
        captureHandleFromText(agent.id, additional);
      }
    }
    return next();
  });

  ctx.on("tools/pre-execute", async (exec, next) => {
    const agent = exec?.agent;
    if (!agent?.id) return next();
    const meta = sessionMeta.get(agent.id) || {};
    const toolArgs = { ...(exec.arguments || {}) };
    if (!toolArgs.context_handle && sessionHandles.has(agent.id)) {
      toolArgs.context_handle = sessionHandles.get(agent.id);
    }
    const result = await dispatchEvent(
      "PreToolUse",
      {
        sessionId: meta.parentSession || agent.id,
        agentId: meta.origin === "subagent" ? agent.id : undefined,
        cwd: meta.cwd,
        callId: exec.callId,
        name: exec.name,
        arguments: toolArgs,
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
      captureHandleFromText(agent.id, additional);
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
