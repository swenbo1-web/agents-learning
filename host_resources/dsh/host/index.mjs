import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function loadRuntimeReceipt() {
  const receiptPath = join(PLUGIN_ROOT, "runtime-release.json");
  return JSON.parse(readFileSync(receiptPath, "utf8").replace(/^\uFEFF/, ""));
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
  {
    name: "learning_history",
    description:
      "DSH 原生项目历史知识吸收与查询工具。action='status'|'jobs'|'sources'|'cases' 为只读查询；当用户明确要求为当前项目吸收历史知识时，传 action='auto'（一键扫描并归档）或 'prepare'/'run' 并设置 confirm=true；用户选择跳过时传 action='skip'。",
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["status", "jobs", "prepare", "run", "auto", "sources", "cases", "pause", "cancel", "skip"],
          description: "执行动作：auto（一键准备并归档）、prepare（仅扫描冻结）、run（归档指定批次）、status（查看状态）、sources/cases（分页查看）、skip（跳过首次吸收）、pause/cancel。",
        },
        confirm: {
          type: "boolean",
          description: "当 action 为 auto / prepare / run 时必须显式设为 true（需用户明确要求吸收项目历史）。",
        },
        job_id: {
          type: "string",
          description: "历史批次 ID（run/sources/cases/pause/cancel 时使用；auto 可省略）。",
        },
        allow_review: {
          type: "boolean",
          description: "仅在用户明确允许调用管理员模型整理历史时设为 true；默认 false 仅归档。",
        },
        max_calls: {
          type: "integer",
          description: "allow_review=true 时的最大模型调用次数（默认 2）。",
        },
        max_seconds: {
          type: "integer",
          description: "单次运行最大秒数（默认 180）。",
        },
        section: {
          type: "string",
          enum: ["sources", "pending", "skipped"],
          description: "action='sources' 时的分页分组。",
        },
        offset: {
          type: "integer",
          description: "sources/cases 分页起始偏移（默认 0）。",
        },
        limit: {
          type: "integer",
          description: "jobs/sources/cases 每页返回条数（默认 20）。",
        },
        conversation_files: {
          type: "array",
          items: { type: "string" },
          description: "用户额外指定导入的对话记录 JSON/JSONL 文件路径列表。",
        },
        artifact_dirs: {
          type: "array",
          items: { type: "string" },
          description: "用户额外指定导入的产物目录路径列表。",
        },
      },
      required: ["action"],
      additionalProperties: false,
    },
    readOnly: false,
  },
];

export const name = "agent-learning";
export const inject = ["tools", "systemPrompt", "agents"];

export function apply(ctx) {
  const sessionContexts = new Map();
  const sessionHandles = new Map();
  const sessionMeta = new Map();
  const scopedAgentIds = new Set();
  const activeDisposers = [];

  async function resolveSessionMeta(agentId) {
    if (!agentId) return {};
    const existing = sessionMeta.get(agentId);
    if (existing && existing.cwd) return existing;
    const sessionQuery = ctx.sessionQuery || ctx.get?.("sessionQuery");
    if (sessionQuery && typeof sessionQuery.observeSession === "function") {
      try {
        const obs = await sessionQuery.observeSession(agentId, { projectionMode: "none" });
        try {
          if (obs?.header) {
            const resolved = {
              cwd: obs.header.cwd,
              parentSession: obs.header.parentSession,
              origin: obs.header.origin,
            };
            sessionMeta.set(agentId, resolved);
            return resolved;
          }
        } finally {
          if (typeof obs?.[Symbol.dispose] === "function") {
            obs[Symbol.dispose]();
          }
        }
      } catch {}
    }
    return existing || {};
  }

  ctx.effect(() => () => {
    while (activeDisposers.length > 0) {
      const dispose = activeDisposers.pop();
      try {
        if (typeof dispose === "function") dispose();
      } catch {}
    }
  });

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

  async function ensureSessionBound(agentId, cwd, signal) {
    if (!agentId || sessionHandles.has(agentId)) {
      return sessionHandles.get(agentId);
    }
    const meta = sessionMeta.get(agentId) || {};
    const eventKind = meta.origin === "subagent" ? "SubagentStart" : "SessionStart";
    const result = await dispatchEvent(
      eventKind,
      {
        sessionId: meta.parentSession || agentId,
        agentId: meta.origin === "subagent" ? agentId : undefined,
        cwd: cwd || meta.cwd || process.cwd(),
        source: "resume",
        requestContext: meta.requestContext,
      },
      signal,
    );
    const additional = result?.hookSpecificOutput?.additionalContext;
    if (additional) {
      sessionContexts.set(agentId, additional);
      captureHandleFromText(agentId, additional);
    }
    return sessionHandles.get(agentId);
  }

  async function invokeMcpTool(toolName, rawArgs, exec) {
    const receipt = loadRuntimeReceipt();
    const agentId = exec?.agent?.id;
    const meta = (agentId && (await resolveSessionMeta(agentId))) || {};
    const workspace = meta.cwd || receipt.workspace || process.cwd();
    const args = { ...(rawArgs || {}) };
    if (!args.context_handle && agentId) {
      const boundHandle = await ensureSessionBound(agentId, workspace, exec?.signal);
      if (boundHandle) {
        args.context_handle = boundHandle;
      }
    }
    const toolScript = join(receipt.runtime_directory, "hooks", "dsh_tool.py");
    const inputPayload = JSON.stringify({
      home: receipt.home || null,
      workspace,
      session_id: meta.parentSession || agentId || null,
      agent_id: meta.origin === "subagent" ? agentId : null,
      tool: toolName,
      args,
    });
    const { code, stdout, stderr } = await runPythonProcess(
      receipt.python,
      ["-B", toolScript],
      inputPayload,
      workspace,
      exec?.signal,
      60000,
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

  async function invokeSlashCommand(commandName, invocation) {
    const receipt = loadRuntimeReceipt();
    const agentId = invocation?.agent?.id;
    const meta = (agentId && (await resolveSessionMeta(agentId))) || {};
    const workspace = meta.cwd || receipt.workspace || process.cwd();
    const toolScript = join(receipt.runtime_directory, "hooks", "dsh_tool.py");
    const inputPayload = JSON.stringify({
      home: receipt.home || null,
      workspace,
      slash_command: commandName,
      raw_input: invocation?.rawInput || "",
    });
    const { code, stdout, stderr } = await runPythonProcess(
      receipt.python,
      ["-B", toolScript],
      inputPayload,
      workspace,
      invocation?.signal,
      120000,
    );
    if (code !== 0 || !stdout.trim()) {
      return {
        kind: "error",
        text: `Agent Learning /${commandName} 执行失败：${(stderr || stdout).trim()}`,
      };
    }
    const parsed = JSON.parse(stdout.trim());
    if (!parsed.ok) {
      return {
        kind: "error",
        text: `Agent Learning /${commandName} 拒绝：${parsed.error || "unknown error"}`,
      };
    }
    return {
      kind: "success",
      text: parsed.result?.text || JSON.stringify(parsed.result, null, 2),
    };
  }

  function makeToolDefinition(spec) {
    return {
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
    };
  }

  function registerToolsForTarget(toolsService) {
    if (!toolsService || typeof toolsService.register !== "function") return;
    for (const spec of WORKER_TOOL_SPECS) {
      try {
        const dispose = toolsService.register(makeToolDefinition(spec));
        if (typeof dispose === "function") {
          activeDisposers.push(dispose);
        }
      } catch {}
    }
  }

  function ensureAgentScopedTools(agent) {
    if (!agent?.id || scopedAgentIds.has(agent.id)) return;
    const scopedTools = agent.ctx?.tools || agent.ctx?.get?.("tools");
    if (scopedTools && scopedTools !== ctx.tools) {
      registerToolsForTarget(scopedTools);
      scopedAgentIds.add(agent.id);
    }
  }

  registerToolsForTarget(ctx.tools);
  if (ctx.agents && typeof ctx.agents.list === "function") {
    for (const liveAgent of ctx.agents.list()) {
      ensureAgentScopedTools(liveAgent);
    }
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
          const dispose = scope.skills.register({
            name: skillName,
            description: desc,
            content,
            source: "bundled",
            resourceBase: { kind: "directory", path: dirname(skillPath) },
          });
          if (typeof dispose === "function") {
            activeDisposers.push(dispose);
          }
        } catch {}
      }
    });
    ctx.inject(["commands"], (scope) => {
      for (const cmd of [
        {
          name: "learning-history",
          description: "一键吸收当前项目历史文档与源码，或查看/跳过历史吸收状态",
          hint: "[auto | prepare | run [job_id] | status | skip | pause <job_id> | cancel <job_id>]",
        },
        {
          name: "learning-status",
          description: "查看当前项目的 Agent Learning 知识库、1.0.0 工作历史与吸收状态",
          hint: "",
        },
        {
          name: "learning-admin",
          description: "配置或触发当前项目的 DSH 管理员知识审核与工作摘要整理",
          hint: "[status | on [current|fixed|default] [model_id] | off | once | batch]",
        },
      ]) {
        try {
          const dispose = scope.commands.register({
            name: cmd.name,
            description: cmd.description,
            input: cmd.hint ? { hint: cmd.hint } : undefined,
            recordInput: true,
            handler: (invocation) => invokeSlashCommand(cmd.name, invocation),
          });
          if (typeof dispose === "function") {
            activeDisposers.push(dispose);
          }
        } catch {}
      }
    });
  }

  if (ctx.systemPrompt) {
    try {
      const dispose = ctx.systemPrompt.context({
        name: "agent-learning-context",
        order: 850,
        text: (assembleCtx) => {
          const scope = assembleCtx?.scope;
          const id = scope && typeof scope === "object" && "id" in scope ? scope.id : undefined;
          return (id && sessionContexts.get(id)) || "";
        },
      });
      if (typeof dispose === "function") {
        activeDisposers.push(dispose);
      }
    } catch {}
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
    ensureAgentScopedTools(agent);
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
    if (agent?.id) {
      ensureAgentScopedTools(agent);
    }
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

  const WORKER_TOOL_NAMES = new Set(WORKER_TOOL_SPECS.map((s) => s.name));

  ctx.on("tools/post-execute", async (exec, toolResult, next) => {
    const agent = exec?.agent;
    if (
      exec?.name &&
      WORKER_TOOL_NAMES.has(exec.name) &&
      toolResult?.isError &&
      typeof toolResult?.error?.message === "string" &&
      toolResult.error.message.includes("SyntaxError: invalid syntax")
    ) {
      const recovered = await invokeMcpTool(exec.name, exec.arguments || {}, exec);
      return {
        kind: "accept",
        content: [{ type: "text", text: JSON.stringify(recovered, null, 2) }],
      };
    }
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
