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
  const budget = object(data.admin_budget);
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
  if (typeof budget.admission_status === "string" && budget.admission_status !== "available") {
    reasons.push(text(budget.admission_status));
  }
  if (routing.ready === false && !routing.reason) reasons.push("administrator_route_unavailable");
  const blocking = reasons.length ? [...new Set(reasons)].join(" / ")
    : routing.ready === true && budget.admission_status === "available" ? "无" : "unknown";
  return [
    "### Agent Learning 调试（只读）",
    `- 项目：\`${text(data.project_id)}\``,
    `- 知识目录：\`${text(data.home, 384)}\``,
    `- 宿主：\`${text(data.host_scope)}\``,
    `- 管理员实际准入：${enabled} | 模式：\`${mode}\` | 模型：\`${resolvedModel}\``,
    `- 剩余审核调用：${remaining}（同 ALP_HOME 共享的插件预算，不代表平台额度）`,
    `- 阻塞原因：\`${blocking}\``,
    "- 会话 / Hook：未验证；此命令未绑定当前对话，不能据此确认 Hooks 已连接。",
    "- 详细核对使用 learning-debug 技能；知识库与审批使用 learning-browser / learning-review 技能。",
  ].join("\n");
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

export function apply(ctx) {
  const sessionContexts = new Map();
  const sessionHandles = new Map();
  const sessionMeta = new Map();
  const browserProcesses = new Map();
  const scopedAgentIds = new Set();
  const activeDisposers = [];

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
    const sessionQuery = typeof ctx.get === "function" ? ctx.get("sessionQuery") : undefined;
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
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" },
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
    const workspace = meta.cwd || receipt.workspace || process.cwd();
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
    const workspace = meta.cwd || receipt.workspace || process.cwd();
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
          hint: "[status | on [current|fixed|default] [model_id] | off | once | batch]",
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
        name: "agent-learning-ui",
        kind: "exact",
        path: "/agent-learning/ui",
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
          const workspace = receipt.workspace || process.cwd();
          const toolScript = join(receipt.runtime_directory, "hooks", "dsh_tool.py");

          if (req.method === "GET") {
            try {
              const inputPayload = JSON.stringify({
                home: receipt.home || null,
                workspace,
                slash_command: "learning-status",
                raw_input: "/learning-status",
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
              const data = parsed?.ok ? parsed.result?.data || {} : {};
              const browserStat = await manageBrowser("status", {}, workspace).catch(() => ({ running: false }));
              send(200, {
                ok: true,
                project_id: data.project_id || "unknown",
                workspace,
                home: data.home || receipt.home || "",
                host_scope: data.host_scope || "dsh:default",
                active_knowledge: data.counts?.active ?? 0,
                pending_problems: data.counts?.pending_problems ?? 0,
                candidates: data.counts?.candidates ?? 0,
                work_memories: data.work_memory?.memories ?? 0,
                file_relations: data.work_memory?.file_relations ?? 0,
                admin_admission_enabled: Boolean(data.admin_admission_enabled ?? data.admin_enabled),
                remaining_calls: data.admin_budget?.remaining_calls ?? 0,
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
        const dispose = scope.webServer.register(route);
        if (typeof dispose === "function") {
          activeDisposers.push(dispose);
        }
      } catch {}
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

  const WORKER_TOOL_NAMES = new Set(WORKER_TOOL_SPECS.map((s) => s.name));

  ctx.on("tools/pre-execute", async (exec, next) => {
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
