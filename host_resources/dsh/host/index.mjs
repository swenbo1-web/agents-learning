import { spawn } from "node:child_process";
import { randomUUID, createHash } from "node:crypto";
import { readFileSync, realpathSync, statSync, existsSync } from "node:fs";
import { dirname, join, resolve, relative, isAbsolute, sep } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { applyExtensions, extensionConfig } from "./extensions.mjs";

const PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function loadRuntimeReceipt() {
  const receiptPath = join(PLUGIN_ROOT, "runtime-release.json");
  return JSON.parse(readFileSync(receiptPath, "utf8").replace(/^\uFEFF/, ""));
}

function normalizeWinPath(p) {
  return String(p || "")
    .trim()
    .replace(/\//g, "\\")
    .replace(/\\+$/, "")
    .toLowerCase();
}

function isWithinWorkspace(childPath, rootPath) {
  const c = normalizeWinPath(childPath);
  const r = normalizeWinPath(rootPath);
  if (!c || !r) return false;
  return c === r || c.startsWith(r + "\\");
}

function loadAllowedWorkspaces(receipt) {
  const list = [];
  if (receipt && Array.isArray(receipt.workspaces)) {
    for (const item of receipt.workspaces) {
      if (typeof item === "string" && item.trim() && !list.includes(item.trim())) {
        list.push(item.trim());
      }
    }
  }
  if (receipt && typeof receipt.workspace === "string" && receipt.workspace.trim()) {
    if (!list.includes(receipt.workspace.trim())) {
      list.push(receipt.workspace.trim());
    }
  }
  return list;
}

function matchInstalledWorkspace(candidateCwd, receipt) {
  const allowed = loadAllowedWorkspaces(receipt);
  const cand = String(candidateCwd || "").trim();
  if (!allowed.length || !cand || !isAbsolute(cand)) return null;
  const sorted = [...allowed].sort((a, b) => b.length - a.length);
  for (const root of sorted) {
    try {
      if (!isAbsolute(root)) continue;
      const actual = realpathSync.native(cand), bound = realpathSync.native(root);
      if (!statSync(actual).isDirectory() || !statSync(bound).isDirectory()) continue;
      const delta = relative(bound, actual);
      if (isAbsolute(delta) || delta === ".." || delta.startsWith(".." + sep)) continue;
      let cursor = actual, nested = false;
      while (relative(bound, cursor)) {
        if (existsSync(join(cursor, ".git"))) { nested = true; break; }
        const parent = dirname(cursor);
        if (parent === cursor) { nested = true; break; }
        cursor = parent;
      }
      if (!nested) return bound;
    } catch {}
  }
  return null;
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

const OBSERVABLE_TEXT_LIMIT = 65536;
const CAPTURE_TURN_LIMIT = 128;
const CAPTURE_MESSAGE_LIMIT = 512;

function boundedPublicText(blocks, maximum = OBSERVABLE_TEXT_LIMIT) {
  // The SDK's assembled message is public text plus explicitly typed blocks.
  // Never stringify other blocks: reasoning and provider replay data are private.
  const parts = typeof blocks === "string" ? [blocks] : Array.isArray(blocks)
    ? blocks.filter((block) => block?.type === "text" && typeof block.text === "string")
      .map((block) => block.text) : [];
  let text = "";
  let chars = 0;
  for (const [index, part] of parts.entries()) {
    const value = (index ? "\n" : "") + part;
    chars += value.length;
    if (text.length < maximum) text += value.slice(0, maximum - text.length);
  }
  return { text, chars, truncated: chars > maximum };
}

function recallMessage(text) {
  // Matches the SDK's immutable, identified UserMessage contract without an
  // extra package dependency in the exported Bundle.
  return Object.freeze({
    id: randomUUID(), role: "user",
    source: Object.freeze({ kind: "plugin:agent-learning", form: "recall" }),
    content: Object.freeze([Object.freeze({ type: "text", text })]),
  });
}

function debugStatusText(value) {
  // Project status includes history and governance records. Debug deliberately
  // projects only scalar diagnostics, even when the native result has no data.
  const object = (item) => item && typeof item === "object" && !Array.isArray(item) ? item : {};
  const text = (item, maximum = 180) => typeof item === "string" && item.trim()
    ? item.replace(/[\u0000-\u001f\u007f`]/g, " ").trim().slice(0, maximum)
    : "unknown";
  const data = object(value);
  const routing = object(data.admin_routing);
  const policy = object(routing.policy);
  const executor = object(routing.executor);
  const budget = object(data.project_usage);
  const globalUsage = object(data.global_usage);
  const admission = object(data.admission);
  const mode = text(executor.model_mode || policy.model_mode, 24);
  const model = text(executor.model || (mode === "fixed" ? policy.model : ""));
  const resolvedModel = model === "unknown" && mode === "default" && routing.routing_ready === true
    ? "宿主默认（未固定）" : model;
  const enabled = data.admin_admission_enabled === true ? "已开启"
    : data.admin_admission_enabled === false ? "已关闭" : "unknown";
  const remaining = Number.isSafeInteger(budget.remaining_calls) && budget.remaining_calls >= 0
    ? String(budget.remaining_calls) : "unknown";
  const reasons = [];
  if (data.admin_admission_enabled === false) reasons.push("administrator_disabled");
  if (typeof routing.reason === "string" && routing.reason.trim()) reasons.push(text(routing.reason));
  if (typeof admission.admission_status === "string" && admission.admission_status !== "available") {
    reasons.push(text(admission.admission_status));
  }
  if (routing.ready === false && !routing.reason) reasons.push("administrator_route_unavailable");
  const blocking = reasons.length ? [...new Set(reasons)].join(" / ")
    : routing.ready === true && admission.admission_status === "available" ? "无" : "unknown";
  return [
    "### Agent Learning 调试（只读）",
    `- 项目：\`${text(data.project_id)}\``,
    `- 知识目录：\`${text(data.home, 384)}\``,
    `- 宿主：\`${text(data.host_scope)}\``,
    `- 管理员实际准入：${enabled} | 模式：\`${mode}\` | 模型：\`${resolvedModel}\``,
    `- 项目剩余审核额度：${budget.scope === "project" && budget.project_id === data.project_id ? remaining : "项目统计暂不可用"}（项目独立、宿主共用，不代表平台额度）`,
    `- 所有项目合计尝试：${Number.isSafeInteger(globalUsage.counted_attempts) ? globalUsage.counted_attempts : "unknown"} | 全局保护剩余：${Number.isSafeInteger(globalUsage.remaining_calls) ? globalUsage.remaining_calls : "unknown"}`,
    `- 阻塞原因：\`${blocking}\``,
    "- 会话 / Hook：未验证；此命令未绑定当前对话，不能据此确认 Hooks 已连接。",
    "- 详细核对使用 learning-debug 技能；知识库与审批使用 learning-browser / learning-review 技能。",
  ].join("\n");
}

function baseRunPythonProcess(pythonExe, args, stdinText, cwd, signal, timeoutMs = 15000, hostEnvironment = {}) {
  return new Promise((resolve, reject) => {
    if (typeof pythonExe !== "string" || !isAbsolute(pythonExe) || !existsSync(pythonExe)
        || !statSync(pythonExe).isFile()) {
      const error = new Error("Agent Learning Python unavailable. Use an independent Python to run "
        + "installation-check, then repair-python --confirm and reload this plugin.");
      error.code = "ALP_PYTHON_UNAVAILABLE";
      reject(error);
      return;
    }
    const child = spawn(pythonExe, args, {
      cwd,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      signal,
      env: { ...process.env, ...hostEnvironment, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" },
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
        mode: {
          type: "string", enum: ["continue", "incremental", "remaining", "full"],
          description: "默认 continue 先继续未结束主审；incremental 检测增量，remaining 优先补扫预算遗漏，full 显式全量。新扫描需先完成或在审批页明确暂存前批。",
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
  {
    name: "learning_browser",
    description:
      "DSH 原生可视化知识库浏览器工具（learning-browser 技能）。在本机 127.0.0.1 启动、查看或关闭当前项目的只读可视化知识库页面（搜索筛选正式知识、查看正文/条件/限制、来源健康、修订历史、事件时间线与管理员待办）。action='start' 启动并默认打开浏览器，'status' 查看运行状态与链接，'stop' 停止服务。",
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["start", "status", "stop"],
          description: "执行动作：start（启动并打开可视化知识库浏览器）、status（查看当前运行状态与访问链接）、stop（停止后台知识库浏览器服务）。",
        },
        open: {
          type: "boolean",
          description: "action='start' 时是否在默认浏览器中自动打开页面（默认 true）。",
        },
        port: {
          type: "integer",
          description: "可选本机监听端口（默认 0 自动分配空闲端口）。",
        },
      },
      required: ["action"],
      additionalProperties: false,
    },
    readOnly: true,
  },
];

export const name = "agent-learning";
export const inject = ["tools", "systemPrompt", "agents"];

const PROFILE_CONFIG_DEPTH = 16;

function plainProfileConfig(value, depth = 0, ancestors = new WeakSet(), size = { nodes: 0 }) {
  // This DSH version marks model configuration fields as schemastery
  // `volatile`, so a plugin's resolved config holds reference objects whose
  // JSON form is `{}`. Snapshotting that literal shape would erase the provider
  // and default-model values, and the reviewer could not mount its adapter.
  // Resolve such references here, inside the trusted host tree, and keep only
  // plain JSON data.
  if (++size.nodes > 10000) throw new Error("profile_snapshot_limit");
  if (value === null || typeof value !== "object") return value;
  // A partial snapshot can erase an explicit model or compatibility setting.
  // Refuse the snapshot as a whole rather than silently pruning that branch.
  if (depth >= PROFILE_CONFIG_DEPTH || ancestors.has(value)) throw new Error("profile_snapshot_limit");
  ancestors.add(value);
  try {
    if (typeof value.get === "function" && typeof value.then !== "function")
      return plainProfileConfig(value.get(), depth + 1, ancestors, size);
    if (Array.isArray(value))
      return value.map(item => plainProfileConfig(item, depth + 1, ancestors, size));
    const output = Object.create(null);
    for (const [key, item] of Object.entries(value)) {
      const plain = plainProfileConfig(item, depth + 1, ancestors, size);
      if (plain !== undefined) output[key] = plain;
    }
    return output;
  } finally { ancestors.delete(value); }
}

export function reviewProfileEnvironment(ctx) {
  // Only the trusted, already-composed loader tree supplies model configuration.
  // Event payloads, workspace files and the global process environment cannot
  // overwrite a host snapshot. Never persist it or publish it as model evidence.
  const unavailable = { ALP_DSH_REVIEW_PROFILE: "" };
  try {
    const directory = resolve(fileURLToPath(ctx.baseUrl));
    const configuredHome = process.env.DSH_HOME;
    const rawHome = configuredHome?.trim() ? configuredHome : join(homedir(), ".dsh");
    const hostPath = (value) => resolve(value === "~" ? homedir()
      : value.startsWith("~/") || value.startsWith("~\\") ? join(homedir(), value.slice(2)) : value);
    const home = hostPath(rawHome);
    const canonical = (path) => process.platform === "win32" ? normalizeWinPath(path) : resolve(path);
    if (canonical(dirname(directory)) !== canonical(join(home, "profiles"))
      || typeof ctx.loader?.entries !== "function") return unavailable;
    const allowed = new Set(["@deepseek-ai/dsh-settings-file", "@deepseek-ai/dsh-credentials-local",
      "@deepseek-ai/dsh-agent-default-model", "@deepseek-ai/dsh-llm-deepseek", "@deepseek-ai/dsh-llm-pi-ai"]);
    const entries = [], names = new Set();
    for (const entry of ctx.loader.entries()) {
      const name = entry?.options?.name;
      if (!allowed.has(name) || entry.disabled) continue;
      const config = entry.fiber?.config;
      const frozen = plainProfileConfig(config);
      if (!frozen || typeof frozen !== "object" || Array.isArray(frozen) || names.has(name)) return unavailable;
      names.add(name);
      if (name === "@deepseek-ai/dsh-settings-file" || name === "@deepseek-ai/dsh-credentials-local") {
        // These SDK modules resolve relative paths against invocation cwd,
        // not loader.baseUrl. Freeze that path before moving to review cwd.
        if (typeof frozen.path === "string") frozen.path = resolve(frozen.path);
        if (typeof frozen.dshHome === "string") frozen.dshHome = hostPath(frozen.dshHome);
      }
      entries.push({ name, config: frozen });
    }
    if (!entries.length) return unavailable;
    const raw = JSON.stringify({ version: 1, profile_directory: directory, entries });
    // Windows individual environment values are bounded; an explicit operator
    // snapshot file remains available for larger host configurations.
    return raw.length <= 16000 ? { ALP_DSH_REVIEW_PROFILE: raw, DSH_HOME: home } : unavailable;
  } catch { return unavailable; }
}

export function apply(ctx, config = {}) {
  const extensions = extensionConfig(config.extensions);
  const hostEnvironment = () => reviewProfileEnvironment(ctx);
  const runPythonProcess = (python, args, input, cwd, signal, timeout = 15000) =>
    baseRunPythonProcess(python, args, input, cwd, signal, timeout, hostEnvironment());
  if (typeof globalThis.__ALP_DSH_DISPOSE__ === "function") {
    try {
      globalThis.__ALP_DSH_DISPOSE__();
    } catch {}
  }
  // 清理同一进程内旧版热加载实例可能遗留在 Cordis _hooks 中的未注销监听器
  const hooksTable = ctx?.events?._hooks;
  if (hooksTable && typeof hooksTable === "object") {
    for (const eventName of [
      "session/created",
      "agent/created",
      "agent/pre-step",
      "tools/pre-execute",
      "tools/post-execute",
      "session/event",
      "session/disposed",
    ]) {
      const list = hooksTable[eventName];
      if (!Array.isArray(list)) continue;
      for (let i = list.length - 1; i >= 0; i--) {
        const cb = list[i]?.callback || list[i];
        const src = typeof cb === "function" ? Function.prototype.toString.call(cb) : "";
        if (
          src.includes("captureTurn(") ||
          src.includes("ensureAgentScopedTools(") ||
          src.includes("WORKER_TOOL_NAMES") ||
          src.includes("forgetSessionCapture(") ||
          src.includes("sessionMeta.set(")
        ) {
          list.splice(i, 1);
        }
      }
    }
  }

  const sessionContexts = new Map();
  const sessionHandles = new Map();
  const sessionMeta = new Map();
  const sessionStarts = new Map();
  const browserProcesses = new Map();
  const scopedAgentIds = new Set();
  const capturedTurns = new Map();
  const activeDisposers = [];
  activeDisposers.push(applyExtensions(ctx, extensions, ({ agent, sessionId }) => {
    try {
      if (agent?.id && sessionId && agent.id !== sessionId) return false;
      const cwd = agent?.session?.header?.cwd || sessionMeta.get(sessionId)?.cwd;
      return Boolean(matchInstalledWorkspace(cwd, loadRuntimeReceipt()));
    } catch { return false; }
  }));
  let sessionQueryService = null;
  let clientModulesDebug = "not_injected";

  const runAllDisposers = () => {
    while (activeDisposers.length > 0) {
      const dispose = activeDisposers.pop();
      try {
        if (typeof dispose === "function") dispose();
      } catch {}
    }
  };
  globalThis.__ALP_DSH_DISPOSE__ = runAllDisposers;

  function onEvent(eventName, listener) {
    const dispose = ctx.on(eventName, listener);
    if (typeof dispose === "function") {
      activeDisposers.push(dispose);
    }
    return dispose;
  }

  function captureTurn(sessionId, turn) {
    if (typeof sessionId !== "string" || !sessionId || !Number.isSafeInteger(turn) || turn < 0) return null;
    const key = JSON.stringify([sessionId, turn]);
    if (!capturedTurns.has(key)) {
      if (capturedTurns.size >= CAPTURE_TURN_LIMIT) capturedTurns.delete(capturedTurns.keys().next().value);
      capturedTurns.set(key, { sessionId, seen: new Set(), reply: null, closed: false });
    }
    return capturedTurns.get(key);
  }

  function forgetSessionCapture(sessionId) {
    for (const [key, state] of capturedTurns) {
      if (state.sessionId === sessionId) capturedTurns.delete(key);
    }
  }
  activeDisposers.push(() => { capturedTurns.clear(); sessionStarts.clear(); });

  if (typeof ctx.inject === "function") {
    ctx.inject(["sessionQuery"], (scope) => {
      sessionQueryService = scope?.sessionQuery || null;
    });
  }

  function stopAllBrowsers() {
    for (const [key, entry] of browserProcesses.entries()) {
      try {
        if (entry?.child && !entry.child.killed) {
          entry.child.kill();
        }
      } catch {}
      browserProcesses.delete(key);
    }
  }
  activeDisposers.push(stopAllBrowsers);

  async function resolveSessionMeta(agentId) {
    if (!agentId) return {};
    const existing = sessionMeta.get(agentId);
    if (existing && existing.cwd) return existing;
    const sessionQuery = sessionQueryService;
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
    runAllDisposers();
    if (globalThis.__ALP_DSH_DISPOSE__ === runAllDisposers) {
      globalThis.__ALP_DSH_DISPOSE__ = undefined;
    }
  });

  function captureHandleFromText(agentId, text) {
    if (!agentId || typeof text !== "string") return;
    const match = text.match(/context_handle=([^\s;]+)/);
    if (match && match[1]) {
      sessionHandles.set(agentId, match[1]);
    }
  }

  // Each bridge instance belongs to one native trajectory. Keep its old token
  // after a trusted reset: silently claiming a new one would relabel delayed
  // callbacks as new work. Reloading the host creates a new bridge instance.
  const trajectoryBindings = new Map();
  function trajectoryKey(payload) {
    return JSON.stringify([payload.sessionId, payload.agentId || null, normalizeWinPath(payload.cwd)]);
  }

  async function dispatchEvent(eventName, payload, signal) {
    const envMode = process.env.ALP_HOOK_MODE;
    const denyBridgeFailure = (code, detail) => ({
      decision: "deny",
      reason: `Agent Learning [${code}]: ${detail} The configured execution guard could not verify this call.`,
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: `Agent Learning [${code}]: ${detail} The configured execution guard could not verify this call.`,
      },
    });
    let receipt;
    try {
      receipt = loadRuntimeReceipt();
    } catch {
      if (eventName === "PreToolUse" && envMode !== "observe") {
        return denyBridgeFailure("release_unverified", "the installed runtime release could not be verified.");
      }
      return {};
    }
    const effectiveMode = receipt.hook_mode || envMode;
    const failClosedOnPre = eventName === "PreToolUse" && effectiveMode !== "observe";
    const dispatcher = join(receipt.runtime_directory, "hooks", "dsh_dispatch.py");
    const argv = ["-B", dispatcher, "--event", eventName];
    if (receipt.hook_mode) {
      argv.push("--hook-mode", receipt.hook_mode);
    }
    const allowedList = loadAllowedWorkspaces(receipt);
    const matchedWorkspace = matchInstalledWorkspace(payload.cwd, receipt);
    if (allowedList.length > 0 && payload.cwd && !matchedWorkspace) {
      return {};
    }
    const effectiveWorkspace = matchedWorkspace || receipt.workspace;
    if (effectiveWorkspace) {
      argv.push("--workspace", effectiveWorkspace);
    }
    if (receipt.home) {
      argv.push("--home", receipt.home);
    }
    const cwd = payload.cwd || effectiveWorkspace || process.cwd();
    const identityPayload = { ...payload, cwd };
    const key = trajectoryKey(identityPayload);
    let binding = trajectoryBindings.get(key);
    if (!binding) {
      binding = { instance: randomUUID(), trajectory: null };
      trajectoryBindings.set(key, binding);
    }
    const capturedPayload = { ...identityPayload, dsh_bridge_instance: binding.instance,
      ...(binding.trajectory ? { dsh_trajectory: { ...binding.trajectory } } : {}) };
    try {
      const { code, stdout } = await runPythonProcess(
        receipt.python,
        argv,
        JSON.stringify(capturedPayload),
        cwd,
        signal,
        10000,
      );
      if (code !== 0 || !stdout.trim()) {
        return failClosedOnPre
          ? denyBridgeFailure("runtime_unavailable", "runtime processing failed; event persistence and policy could not be confirmed.")
          : {};
      }
      const parsed = JSON.parse(stdout.trim());
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return failClosedOnPre
          ? denyBridgeFailure("response_invalid", "runtime returned an invalid response; its result could not be confirmed.")
          : {};
      }
      const trajectory = parsed.dsh_trajectory;
      if (trajectoryBindings.get(key) === binding && Number.isSafeInteger(trajectory?.generation)
          && trajectory.generation >= 1 && /^[a-f0-9]{32}$/.test(trajectory?.token || "")) {
        binding.trajectory = { generation: trajectory.generation, token: trajectory.token };
      }
      return parsed;
    } catch {
      return failClosedOnPre
        ? denyBridgeFailure("runtime_unavailable", "runtime processing failed; event persistence and policy could not be confirmed.")
        : {};
    }
  }

  async function ensureSessionBound(agentId, cwd, signal) {
    await sessionStarts.get(agentId);
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

  async function manageBrowser(action, options, workspace, signal) {
    const receipt = loadRuntimeReceipt();
    const targetWorkspace = workspace || receipt.workspace || process.cwd();
    const key = targetWorkspace.toLowerCase();
    const existing = browserProcesses.get(key);
    const isAlive = existing && existing.child && exitCodeOf(existing.child) === null && !existing.child.killed;

    if (!isAlive && existing) {
      browserProcesses.delete(key);
    }

    if (action === "status") {
      if (isAlive) {
        return {
          status: "serving",
          running: true,
          ...existing.info,
        };
      }
      return {
        status: "stopped",
        running: false,
        workspace: targetWorkspace,
        read_only: true,
      };
    }

    if (action === "stop") {
      if (isAlive) {
        try {
          existing.child.kill();
        } catch {}
        browserProcesses.delete(key);
        return {
          status: "stopped",
          running: false,
          stopped: true,
          workspace: targetWorkspace,
        };
      }
      return {
        status: "stopped",
        running: false,
        stopped: false,
        workspace: targetWorkspace,
      };
    }

    if (action !== "start") {
      throw new Error("learning_browser action must be start, status or stop");
    }

    const shouldOpen = options?.open !== false;
    const wantReview = Boolean(options?.review);
    const wantAdminControl = Boolean(options?.adminControl);
    const requestedPort =
      typeof options?.port === "number" && Number.isInteger(options.port) && options.port >= 0 && options.port <= 65535
        ? options.port
        : 0;

    if (
      isAlive &&
      (requestedPort === 0 || existing.info.port === requestedPort) &&
      (!wantReview || existing.info.review) &&
      (!wantAdminControl || existing.info.admin_control)
    ) {
      if (shouldOpen && existing.info.browser_url) {
        await runPythonProcess(
          receipt.python,
          ["-B", "-c", "import sys, webbrowser; webbrowser.open(sys.argv[1])", existing.info.browser_url],
          "",
          targetWorkspace,
          signal,
          5000,
        ).catch(() => {});
      }
      return {
        status: "serving",
        running: true,
        reused: true,
        opened: shouldOpen,
        ...existing.info,
      };
    }

    if (isAlive) {
      try {
        existing.child.kill();
      } catch {}
      browserProcesses.delete(key);
    }

    const cliScript = join(receipt.runtime_directory, "scripts", "learning.py");
    const baseArgs = ["-B", cliScript];
    if (receipt.home) {
      baseArgs.push("--home", receipt.home);
    }
    baseArgs.push("--workspace", targetWorkspace, "--host", "dsh");

    // 先确保当前项目已在数据库中注册
    await runPythonProcess(receipt.python, [...baseArgs, "status"], "", targetWorkspace, signal, 10000);

    const browserArgs = [...baseArgs, "browser", "--port", String(requestedPort)];
    if (wantReview) {
      browserArgs.push("--review");
    }
    if (wantAdminControl) {
      browserArgs.push("--admin-control");
    }
    if (shouldOpen) {
      browserArgs.push("--open");
    }

    return new Promise((resolve, reject) => {
      const child = spawn(receipt.python, browserArgs, {
        cwd: targetWorkspace,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
        env: { ...process.env, ...hostEnvironment(), PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" },
      });
      let buffer = "";
      let stderrText = "";
      let settled = false;
      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          try {
            child.kill();
          } catch {}
          reject(new Error("Timed out starting Agent Learning knowledge browser"));
        }
      }, 10000);

      child.stdout.on("data", (chunk) => {
        if (settled) return;
        buffer += chunk.toString("utf8");
        const newlineIdx = buffer.indexOf("\n");
        if (newlineIdx !== -1) {
          const firstLine = buffer.slice(0, newlineIdx).trim();
          settled = true;
          clearTimeout(timer);
          try {
            const parsed = JSON.parse(firstLine);
            const url = parsed.url;
            const token = parsed.access_token;
            const browserUrl =
              parsed.admin_control_url ||
              parsed.review_url ||
              url + "#token=" + encodeURIComponent(token);
            const portMatch = String(url).match(/:(\d+)\//);
            const info = {
              project: parsed.project,
              workspace: targetWorkspace,
              url,
              browser_url: browserUrl,
              access_token: token,
              port: portMatch ? Number(portMatch[1]) : requestedPort,
              pid: child.pid,
              read_only: Boolean(parsed.read_only),
              review: wantReview,
              admin_control: wantAdminControl,
            };
            browserProcesses.set(key, { child, info });
            child.on("close", () => {
              const cur = browserProcesses.get(key);
              if (cur && cur.child === child) {
                browserProcesses.delete(key);
              }
            });
            resolve({
              status: "serving",
              running: true,
              reused: false,
              opened: shouldOpen,
              ...info,
            });
          } catch (err) {
            try {
              child.kill();
            } catch {}
            reject(new Error(`Failed to parse browser startup output: ${firstLine}`));
          }
        }
      });

      child.stderr.on("data", (chunk) => {
        stderrText += chunk.toString("utf8");
      });

      child.on("error", (err) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(err);
        }
      });

      child.on("close", (code) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`Browser process exited with code ${code}: ${(stderrText || buffer).trim()}`));
        }
      });
    });
  }

  function exitCodeOf(child) {
    return child && typeof child.exitCode === "number" ? child.exitCode : null;
  }

  async function invokeMcpTool(toolName, rawArgs, exec) {
    const receipt = loadRuntimeReceipt();
    const agentId = exec?.agent?.id;
    const meta = (agentId && (await resolveSessionMeta(agentId))) || {};
    const matched = matchInstalledWorkspace(meta.cwd, receipt);
    const workspace = matched || meta.cwd || receipt.workspace || process.cwd();
    const args = { ...(rawArgs || {}) };
    if (toolName === "learning_browser") {
      return manageBrowser(args.action || "start", args, workspace, exec?.signal);
    }
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
    const matched = matchInstalledWorkspace(meta.cwd, receipt);
    const workspace = matched || meta.cwd || receipt.workspace || process.cwd();
    // Debug is a fixed read route. Never forward a user-supplied suffix into
    // the administrator/history command handlers or start the browser.
    const statusOnly = commandName === "learning-debug";
    if (commandName === "learning-browser") {
      const tokens = String(invocation?.rawInput || "").trim().split(/\s+/).filter(Boolean);
      if (tokens.length > 0 && tokens[0].replace(/^\//, "").toLowerCase() === "learning-browser") {
        tokens.shift();
      }
      const sub = (tokens[0] || "start").toLowerCase();
      if (!["start", "status", "stop"].includes(sub)) {
        return {
          kind: "success",
          text: "用法：`/learning-browser [start | status | stop]`（默认 `start` 启动并在浏览器打开只读可视化知识库）",
        };
      }
      try {
        const res = await manageBrowser(sub, { open: true }, workspace, invocation?.signal);
        if (sub === "stop") {
          return {
            kind: "success",
            text: res.stopped
              ? "已停止当前项目的可视化知识库浏览器服务。"
              : "当前项目没有正在运行的可视化知识库浏览器服务。",
          };
        }
        if (!res.running) {
          return {
            kind: "success",
            text: "当前项目的可视化知识库浏览器未运行。输入 `/learning-browser` 或 `/learning-browser start` 即可启动并打开。",
          };
        }
        return {
          kind: "success",
          text: [
            `### 可视化知识库浏览器已就绪（只读模式）`,
            `- **项目 ID**: \`${res.project}\`（\`${res.workspace}\`）`,
            `- **直连地址（含临时访问码）**: [点击打开可视化知识库](${res.browser_url})`,
            `- **本机监听地址**: \`${res.url}\` | **PID**: \`${res.pid}\``,
            `- **临时访问码**: \`${res.access_token}\`（页面加载后自动从地址栏清除，仅保留在页面内存中）`,
            `- 关闭方式：输入 \`/learning-browser stop\` 或调用 \`learning_browser(action="stop")\`。`,
          ].join("\n"),
        };
      } catch (err) {
        return {
          kind: "error",
          text: `可视化知识库启动失败：${err?.message || err}`,
        };
      }
    }
    const toolScript = join(receipt.runtime_directory, "hooks", "dsh_tool.py");
    const inputPayload = JSON.stringify({
      home: receipt.home || null,
      workspace,
      slash_command: statusOnly ? "learning-status" : commandName,
      raw_input: statusOnly ? "/learning-status" : invocation?.rawInput || "",
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
        text: statusOnly ? "Agent Learning 调试：只读状态读取失败，请使用 learning-debug 技能继续核对。"
          : `Agent Learning /${commandName} 执行失败：${(stderr || stdout).trim()}`,
      };
    }
    const parsed = JSON.parse(stdout.trim());
    if (!parsed.ok) {
      return {
        kind: "error",
        text: statusOnly ? "Agent Learning 调试：只读状态读取被拒绝，请使用 learning-debug 技能继续核对。"
          : `Agent Learning /${commandName} 拒绝：${parsed.error || "unknown error"}`,
      };
    }
    return {
      kind: "success",
      text: statusOnly ? debugStatusText(parsed.result?.data)
        : parsed.result?.text || JSON.stringify(parsed.result, null, 2),
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
        [
          "learning-browser",
          "在 DSH 中启动、查看或关闭本机只读可视化知识库浏览器（127.0.0.1），搜索和筛选正式知识、查看来源健康、修订历史、事件时间线与管理员待办。",
        ],
        [
          "learning-review",
          "打开当前项目的本机可视化知识审批台，预览候选和证据，由用户在页面批准发布、拒绝或暂缓选中条目。",
        ],
        [
          "learning-debug",
          "Agent Learning 调试：只读检查当前项目连接、管理员、模型与预算，不启动模型、不修改设置、不展开知识正文。",
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
          name: "learning-debug",
          description: "Agent Learning 调试：只读查看连接、管理员、模型与预算",
          hint: "",
        },
        {
          name: "learning-admin",
          description: "配置或触发当前项目的 DSH 管理员知识审核与工作摘要整理",
          hint: "[status | on [first|current|fixed|default] [model_id] | off | once | batch]",
        },
        {
          name: "learning-browser",
          description: "启动、查看或关闭当前项目的本机只读可视化知识库浏览器",
          hint: "[start | status | stop]",
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
    ctx.inject(["webServer"], (scope) => {
      if (!scope?.webServer || typeof scope.webServer.register !== "function") return;
      const route = {
        name: "agent-learning-panel-v2",
        kind: "exact",
        path: "/agent-learning/panel-v2",
        handler: async (req, res) => {
          const send = (status, body) => {
            res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
            res.end(JSON.stringify(body));
          };
          if (req.headers?.["sec-fetch-site"] === "cross-site") {
            send(403, { ok: false, error: "cross-site request denied" });
            return;
          }
          let receipt;
          try {
            receipt = loadRuntimeReceipt();
          } catch (err) {
            send(500, { ok: false, error: "runtime receipt unavailable" });
            return;
          }
          const installationId = createHash("sha256").update(JSON.stringify(receipt)).digest("hex");
          const reqUrl = new URL(req.url || "/agent-learning/panel", "http://127.0.0.1");
          let requestedCwd = (reqUrl.searchParams.get("cwd") || "").trim();
          const requestedSessionId = (reqUrl.searchParams.get("sessionId") || "").trim();
          if (!requestedCwd && requestedSessionId) {
            const meta = await resolveSessionMeta(requestedSessionId);
            if (meta?.cwd) {
              requestedCwd = String(meta.cwd).trim();
            }
          }
          const allowedList = loadAllowedWorkspaces(receipt);
          const matchedWorkspace = matchInstalledWorkspace(requestedCwd, receipt);

          if (req.method === "GET") {
            if (!matchedWorkspace) {
              send(200, { ok: true, installed: false, reason: "outside_bound_workspace" });
              return;
            }
            const workspace = requestedCwd;
            if (!workspace) {
              send(200, { ok: true, installed: false, reason: "no_workspace" });
              return;
            }
            const toolScript = join(receipt.runtime_directory, "hooks", "dsh_tool.py");
            try {
              const inputPayload = JSON.stringify({
                home: receipt.home || null,
                workspace,
                panel_bound_workspace: matchedWorkspace,
                panel_status: true,
              });
              const { code, stdout } = await runPythonProcess(
                receipt.python,
                ["-B", toolScript],
                inputPayload,
                workspace,
                undefined,
                15000,
              );
              const parsed = code === 0 && stdout.trim() ? JSON.parse(stdout.trim()) : null;
              if (parsed?.ok && parsed.result?.registered === false) {
                send(200, { ok: true, installed: false, reason: "workspace_not_registered" });
                return;
              }
              if (!parsed?.ok || !parsed.result?.data) throw new Error(parsed?.error || "project status unavailable");
              const data = parsed.result.data;
              const browserStat = await manageBrowser("status", {}, workspace).catch(() => ({ running: false }));
              const projectName = workspace.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || workspace;
              const projectUsage = data.project_usage?.scope === "project" && data.project_usage.project_id === data.project_id ? data.project_usage : null;
              const budgetValues = data.project_budget_policy?.values || {};
              send(200, {
                ok: true,
                installed: true,
                project_id: data.project_id || "unknown",
                project_name: projectName,
                workspace,
                home: data.home || receipt.home || "",
                host_scope: data.host_scope || "dsh:default",
                active_knowledge: data.counts?.active ?? 0,
                pending_problems: data.counts?.pending_problems ?? 0,
                pending_review_cases: data.counts?.pending_review_cases ?? null,
                awaiting_evidence_cases: data.counts?.awaiting_evidence_cases ?? null,
                awaiting_approval_cases: data.counts?.awaiting_approval_cases ?? null,
                historical_pending_units: data.counts?.historical_pending_units ?? null,
                candidates: data.counts?.candidates ?? 0,
                work_memories: data.work_memory?.memories ?? 0,
                file_relations: data.work_memory?.file_relations ?? 0,
                admin_admission_enabled: Boolean(data.admin_admission_enabled ?? data.admin_enabled),
                remaining_calls: projectUsage?.remaining_calls ?? null,
                max_calls_per_day: projectUsage?.max_calls_per_day ?? null,
                max_calls_per_batch: budgetValues.max_calls_per_batch ?? null,
                project_usage: projectUsage,
                global_usage: data.global_usage || null,
                project_budget_policy: data.project_budget_policy || null,
                global_budget_policy: data.global_budget_policy || null,
                panel_snapshot: data.panel_snapshot ? { ...data.panel_snapshot, installation_id: installationId } : null,
                diagnostic: data.panel_snapshot?.diagnostic || null,
                admission: data.admission || null,
                onboarding: data.history_import?.onboarding?.state || "unoffered",
                browser_running: Boolean(browserStat?.running),
              });
            } catch (err) {
              send(500, { ok: false, error: String(err?.message || err) });
            }
            return;
          }

          if (req.method !== "POST") {
            res.writeHead(405).end();
            return;
          }

          try {
            const chunks = [];
            let total = 0;
            for await (const chunk of req) {
              total += chunk.length;
              if (total > 32 * 1024) {
                send(413, { ok: false, error: "payload too large" });
                req.destroy();
                return;
              }
              chunks.push(chunk);
            }
            const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
            const action = body?.action;
            let bodyCwd = typeof body?.cwd === "string" ? body.cwd.trim() : "";
            if (!bodyCwd && typeof body?.sessionId === "string" && body.sessionId.trim()) {
              const meta = await resolveSessionMeta(body.sessionId.trim());
              if (meta?.cwd) bodyCwd = String(meta.cwd).trim();
            }
            const matchedPostWorkspace = matchInstalledWorkspace(bodyCwd, receipt);
            if (!matchedPostWorkspace) {
              send(403, { ok: false, error: "project is not bound to this Agent Learning installation" });
              return;
            }
            const workspace = bodyCwd;
            const toolScript = join(receipt.runtime_directory, "hooks", "dsh_tool.py");
            if (body.installation_id !== installationId) {
              send(409, { ok: false, error: "Agent Learning installation changed; reopen the panel" });
              return;
            }
            // Recheck the logical project in Python before every panel action.
            // Path membership alone cannot resolve configured project aliases.
            if (typeof body.project_id !== "string" || !body.project_id) {
              send(409, { ok: false, error: "panel project unavailable; refresh before continuing" });
              return;
            }
            const checked = await runPythonProcess(receipt.python, ["-B", toolScript], JSON.stringify({
              home: receipt.home || null, workspace, panel_bound_workspace: matchedPostWorkspace,
              panel_project_id: body.project_id, panel_status: true,
            }), workspace, undefined, 15000);
            const checkedResult = checked.code === 0 && checked.stdout.trim() ? JSON.parse(checked.stdout.trim()) : null;
            if (!checkedResult?.ok) {
              send(409, { ok: false, error: "panel project changed or unavailable; refresh before continuing" });
              return;
            }

            if (action === "set_admin_preset") {
              const preset = String(body?.preset || "").toLowerCase();
              const inputPayload = JSON.stringify({
                home: receipt.home || null,
                workspace,
                panel_bound_workspace: matchedPostWorkspace,
                panel_project_id: body.project_id,
                admin_preset: preset,
                panel_snapshot: body.panel_snapshot,
                request_id: body.request_id,
                confirm: body.confirm,
              });
              const { code, stdout, stderr } = await runPythonProcess(
                receipt.python,
                ["-B", toolScript],
                inputPayload,
                workspace,
                undefined,
                20000,
              );
              const parsed = code === 0 && stdout.trim() ? JSON.parse(stdout.trim()) : null;
              if (!parsed?.ok) {
                send(400, { ok: false, error: parsed?.error || (stderr || stdout).trim() || "preset failed" });
                return;
              }
              send(200, { ok: true, ...parsed.result });
              return;
            }

            if (action === "admin_receipt") {
              const checked = await runPythonProcess(receipt.python, ["-B", toolScript], JSON.stringify({
                home: receipt.home || null, workspace, panel_bound_workspace: matchedPostWorkspace,
                panel_project_id: body.project_id, panel_receipt: true, request_id: body.request_id,
              }), workspace, undefined, 15000);
              const result = checked.code === 0 && checked.stdout.trim() ? JSON.parse(checked.stdout.trim()) : null;
              if (!result?.ok) { send(400, { ok: false, error: "request receipt unavailable" }); return; }
              send(200, { ok: true, ...result.result });
              return;
            }

            if (action === "open_browser") {
              const info = await manageBrowser(
                "start",
                { open: true, review: false, adminControl: false },
                workspace,
              );
              send(200, { ok: true, running: Boolean(info.running), url: info.url, mode: "readonly" });
              return;
            }
            if (action === "open_review") {
              const info = await manageBrowser(
                "start",
                { open: true, review: true, adminControl: true },
                workspace,
              );
              send(200, { ok: true, running: Boolean(info.running), url: info.url, mode: "review" });
              return;
            }
            if (action === "stop_browser") {
              const info = await manageBrowser("stop", {}, workspace);
              send(200, { ok: true, running: false, stopped: Boolean(info.stopped) });
              return;
            }
            if (action === "archive_history") {
              const inputPayload = JSON.stringify({
                home: receipt.home || null,
                workspace,
                panel_bound_workspace: matchedPostWorkspace,
                panel_project_id: body.project_id,
                tool: "learning_history",
                args: { action: "auto", confirm: true },
              });
              const { code, stdout, stderr } = await runPythonProcess(
                receipt.python,
                ["-B", toolScript],
                inputPayload,
                workspace,
                undefined,
                120000,
              );
              const parsed = code === 0 && stdout.trim() ? JSON.parse(stdout.trim()) : null;
              if (!parsed?.ok) {
                send(400, { ok: false, error: parsed?.error || (stderr || stdout).trim() || "archive failed" });
                return;
              }
              send(200, {
                ok: true,
                job_id: parsed.result?.prepared_job_id,
                preparation: parsed.result?.preparation,
                changes: parsed.result?.changes,
                cases: parsed.result?.job?.counts?.cases ?? 0,
                sources: parsed.result?.job?.counts?.materialized_sources ?? 0,
              });
              return;
            }
            send(400, { ok: false, error: "unsupported action" });
          } catch (err) {
            send(400, { ok: false, error: String(err?.message || err) });
          }
        },
      };
      try {
        scope.effect(() => scope.webServer.register(route), "agent-learning: project-ui route");
        const clientBundleRoute = {
          name: "agent-learning-client-bundle",
          kind: "exact",
          path: "/plugins/agent-learning/client.js",
          handler: (_req, res) => {
            try {
              const code = readFileSync(join(PLUGIN_ROOT, "client", "index.js"));
              res.writeHead(200, {
                "content-type": "text/javascript; charset=utf-8",
                "cache-control": "no-cache",
              });
              res.end(code);
            } catch {
              res.writeHead(404).end();
            }
          },
        };
        scope.effect(() => scope.webServer.register(clientBundleRoute), "agent-learning: client bundle route");
      } catch {}
    });
    ctx.inject(["clientModules"], (scope) => {
      const cm = scope?.clientModules;
      if (!cm) return;
      const syncClientModule = () => {
        try {
          const clientPath = join(PLUGIN_ROOT, "client", "index.js");
          const rev = String(Date.now()).slice(-12);
          const meta = {
            clientPath,
            external: [],
            immediately: true,
          };
          const entry = {
            id: "agent-learning",
            url: `/plugins/agent-learning/client.js?rev=${rev}`,
            rev,
            immediately: true,
          };
          if (cm.pkgMeta instanceof Map) {
            cm.pkgMeta.set("agent-learning", meta);
          }
          if (cm.table instanceof Map) {
            cm.table.set("agent-learning", { entry, meta });
          }
          if (typeof cm.compose === "function") {
            cm.composed = cm.compose();
            cm.notifyGraphChanged?.();
          }
          if (cm.rebuildListeners instanceof Set) {
            for (const notify of cm.rebuildListeners) {
              try {
                notify("agent-learning", rev);
              } catch {}
            }
          }
          clientModulesDebug = "synced:" + (cm.clientPath?.("agent-learning") || "none");
        } catch (err) {
          clientModulesDebug = "error:" + String(err?.message || err);
        }
      };
      syncClientModule();
      setTimeout(syncClientModule, 50);
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

  onEvent("session/created", (session) => {
    if (!session || !session.header) return;
    sessionMeta.set(session.id, {
      cwd: session.header.cwd,
      parentSession: session.header.parentSession,
      origin: session.header.origin,
      requestContext: typeof session.requestContext === "function" ? session.requestContext() : undefined,
    });
  });

  onEvent("agent/created", (payload) => {
    const agent = payload?.agent;
    if (!agent?.id) return;
    ensureAgentScopedTools(agent);
    const meta = sessionMeta.get(agent.id) || {};
    const eventKind = meta.origin === "subagent" ? "SubagentStart" : "SessionStart";
    const started = dispatchEvent(
      eventKind,
      {
        sessionId: meta.parentSession || agent.id,
        agentId: meta.origin === "subagent" ? agent.id : undefined,
        cwd: meta.cwd,
        source: payload.source || "startup",
        requestContext: meta.requestContext,
      },
      payload.signal,
    ).then((result) => {
      if (sessionStarts.get(agent.id) !== started) return;
      const additional = result?.hookSpecificOutput?.additionalContext;
      if (additional) {
        sessionContexts.set(agent.id, additional);
        captureHandleFromText(agent.id, additional);
      }
    });
    sessionStarts.set(agent.id, started);
    started.finally(() => {
      if (sessionStarts.get(agent.id) === started) sessionStarts.delete(agent.id);
    });
    return started;
  });

  onEvent("agent/pre-step", async (payload, next) => {
    const agent = payload?.agent;
    if (agent?.id) {
      ensureAgentScopedTools(agent);
      // The SDK does not await agent/created listeners. Serialize the pending
      // startup Hook before retrieval so its late reply cannot replace this step.
      await sessionStarts.get(agent.id);
    }
    // systemPrompt assembly and inbox claiming already happened before this
    // waterfall. Its returned messages are the supported way to reach THIS step;
    // agent.inject() or mutating the cached system context would arrive too late.
    const decision = await next();
    if (!agent?.id || decision?.kind !== "enter" || !Array.isArray(decision.messages)
      || payload.signal?.aborted) return decision;
    const cleanedMessages = decision.messages.filter(
      (m) => !(m?.source?.kind === "plugin" && m?.source?.plugin === "agent-learning")
        && m?.source?.kind !== "plugin:agent-learning",
    );
    const baseDecision = cleanedMessages.length === decision.messages.length
      ? decision
      : { ...decision, messages: cleanedMessages };
    const state = captureTurn(agent.id, payload.turn);
    if (!state || state.closed) return baseDecision;
    const humanMessages = [];
    for (const message of baseDecision.messages) {
      if (message?.role !== "user" || message.source?.kind !== "user"
        || typeof message.id !== "string" || !message.id || message.id.length > 200
        || state.seen.has(message.id) || state.seen.size >= CAPTURE_MESSAGE_LIMIT) continue;
      state.seen.add(message.id);
      humanMessages.push(message);
    }
    if (humanMessages.length) {
      const meta = await resolveSessionMeta(agent.id);
      const prompt = boundedPublicText(humanMessages.flatMap((message) =>
        typeof message.content === "string" ? [{ type: "text", text: message.content }]
          : Array.isArray(message.content) ? message.content : []));
      if (!prompt.text.trim()) return baseDecision;
      const result = await dispatchEvent(
        "UserPromptSubmit",
        {
          sessionId: meta.parentSession || agent.id,
          agentId: meta.origin === "subagent" ? agent.id : undefined,
          cwd: meta.cwd,
          turn: payload.turn,
          step: payload.step,
          message_ids: humanMessages.map((message) => message.id),
          prompt: prompt.text,
          prompt_truncated: prompt.truncated,
          prompt_chars: prompt.chars,
          requestContext: meta.requestContext,
        },
        payload.signal,
      );
      const additional = result?.hookSpecificOutput?.additionalContext;
      if (typeof additional === "string" && additional && !payload.signal?.aborted && !state.closed) {
        // The recall now lives in the logged message history. Avoid republishing
        // that same card in the next system-context snapshot as well.
        sessionContexts.delete(agent.id);
        captureHandleFromText(agent.id, additional);
        return { ...baseDecision, messages: [...baseDecision.messages, recallMessage(additional)] };
      }
    }
    return baseDecision;
  });

  const WORKER_TOOL_NAMES = new Set(WORKER_TOOL_SPECS.map((s) => s.name));

  onEvent("tools/pre-execute", async (exec, next) => {
    const agent = exec?.agent;
    if (!agent?.id) return next();
    const meta = sessionMeta.get(agent.id) || {};
    const toolArgs = { ...(exec.arguments || {}) };
    if (WORKER_TOOL_NAMES.has(exec.name) && !toolArgs.context_handle && sessionHandles.has(agent.id)) {
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

  onEvent("tools/post-execute", async (exec, toolResult, next) => {
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

  onEvent("session/event", (session, event) => {
    if (!session?.id || !event) return;
    if (event.type === "request/context" && event.data) {
      const prev = sessionMeta.get(session.id) || { cwd: session.header?.cwd };
      sessionMeta.set(session.id, { ...prev, requestContext: event.data });
    } else if (event.type === "assistant/message" && event.data) {
      const state = captureTurn(session.id, event.data.turn);
      const message = event.data.message;
      if (!state || state.closed || message?.role !== "assistant" || message.source?.kind !== "model"
        || !Number.isSafeInteger(event.data.step) || event.data.step < 0
        || (state.reply && state.reply.step > event.data.step)) return;
      state.reply = {
        ...boundedPublicText(message.content),
        step: event.data.step,
        interrupted: event.data.interrupted === true,
      };
    } else if (event.type === "turn/end" && event.data) {
      const state = captureTurn(session.id, event.data.turn);
      if (!state || state.closed) return;
      state.closed = true;
      const meta = sessionMeta.get(session.id) || { cwd: session.header?.cwd };
      const reasonKind = event.data.reason?.kind;
      const complete = reasonKind === "completed" && state.reply?.interrupted !== true;
      const reply = state.reply;
      state.reply = null;
      // A token ceiling, error, blocked step or canceled prefix is not a completed
      // public delivery. Never promote those into Stop/delivered evidence.
      return dispatchEvent(complete ? "Stop" : "Interrupt", {
        sessionId: meta.parentSession || session.id,
        agentId: meta.origin === "subagent" ? session.id : undefined,
        cwd: meta.cwd,
        turn: event.data.turn,
        ...(complete && reply?.text ? {
          last_assistant_message: reply.text,
          last_assistant_message_truncated: reply.truncated,
          last_assistant_message_chars: reply.chars,
        } : {}),
        requestContext: meta.requestContext,
      });
    }
  });

  onEvent("session/disposed", (session) => {
    if (session?.id) {
      forgetSessionCapture(session.id);
      sessionStarts.delete(session.id);
      for (const key of trajectoryBindings.keys()) {
        const [root, child] = JSON.parse(key);
        if (root === session.id || child === session.id) trajectoryBindings.delete(key);
      }
    }
  });
}
