/**
 * Delegation policy: split analysis from execution, and keep the main session's
 * context small by handing indivisible, self-contained execution batches to
 * subagents that run on the cheap coding route.
 *
 * Registered as an ordered system-prompt section so it applies to every session
 * in this profile regardless of workspace, and survives restarts without
 * touching any project's AGENTS.md.
 */

/** The route subagents use for implementation work (DSH shows it as V4.1 Flash). */
const CODING_PROVIDER = "deepseek-account";
const CODING_MODEL = "deepseek-flash";

const TEXT = `## 委派与分工策略（本机全局约定）

你的角色：分析、判断、接口约定与最终验收。**可并行拆分的执行工作应下派给子代理**，以保持本会话上下文精简。

### 何时下派给 subagent（满足任一即下派）
- 需要读取或检索 **3 个以上文件**，或需要跨目录搜索；
- 需要 **2 次以上**同类探针、脚本试跑、构建或测试循环；
- 有 **两个以上互不依赖** 的改动目标，可以并行推进；
- 大段输出（日志、转储、批量比对）只需结论、不需要原文留在本会话。

### 代码实现交给 V4.1 Flash
- 写实现代码的子代理目标路由固定为 \`provider=${CODING_PROVIDER}\`、\`model=${CODING_MODEL}\`（界面显示为 V4.1 Flash）。按工具能力择一：
  1. **\`subagent\` 带 \`provider\` / \`model\` 参数**（新会话才有这两个参数）→ 直接传入；
  2. **\`workflow\` 的 \`agent(prompt, { provider, model })\`**（任何会话都可用，不受子代理路由限制）→ 旧会话请用这条；
  3. 两者都不可用时**不要臆造参数**：自己执行，或用 \`subagent_fork\` 委派（会继承本会话模型）；
- 你保留：接口与文件边界约定、关键片段的审阅、失败原因分析与最终验收；
- 若本会话本身已是该路由，则**不要把代码任务再下派一遍**，直接自己做。

### 何时不要下派
- 单文件小改动、单次 \`read\`、一次 \`edit\`；
- 需要完整推理链的判断（架构取舍、疑难排查的因果推演）；
- 子任务强依赖本对话的已有上下文时，改用 \`subagent_fork\`（会继承上下文），否则一律用 \`subagent\`。

### 并行上限与汇报契约
- 同一时刻最多并行 **2 个** 子代理；禁止为同一个问题扇出 4 个以上实例；
- 派发时一次性写全：目标、允许改动的文件、验收标准、禁止改动范围；
- 子代理必须回传：**改了哪些文件、关键改动摘要、如何验证（命令与结果）**；
- 收到汇报后由你复核 diff 与验证结果，再向用户陈述结论。`;

/**
 * Thinking-language policy, scoped to DeepSeek routes only. Other models keep
 * their native reasoning language: forcing Chinese onto an English-dominant
 * code/math model costs attention for no benefit.
 */
const THINKING_TEXT = `## 思考语言

- 内部思考（reasoning）与对用户的说明使用 **简体中文**；
- 以下内容保持 **原样英文/原文**，不要翻译：代码、标识符、函数与变量名、文件路径、命令行、报错原文、协议字段、日志、数字与公式；
- 不要为了"写中文"去改代码注释、字符串字面量或正则；技术术语没有通行中文译法时直接保留英文；
- 中文用于组织逻辑与结论，不追求措辞完整，避免为凑句子而重复已知信息。`;

/** True only for DeepSeek routes (`deepseek-account`, `deepseek-*`). */
function isDeepSeekRoute(route) {
  if (!route) return false;
  const provider = typeof route.provider === "string" ? route.provider.toLowerCase() : "";
  const model = typeof route.model === "string" ? route.model.toLowerCase() : "";
  return provider.startsWith("deepseek") || model.startsWith("deepseek");
}

/**
 * Resolve the route this assembly belongs to: the session's latest logged
 * request header first (authoritative once a request has run), then explicit
 * agent options, then the deployment default.
 */
function resolveRoute(context, ctx) {
  const agent = context?.agent;
  try {
    const config = agent?.session?.requestHeader?.()?.config;
    if (config && (config.provider || config.model)) return config;
  } catch {}
  try {
    const options = agent?.options;
    if (options && (options.provider || options.model)) return options;
  } catch {}
  try {
    const selection = ctx.get("agentDefaultModel")?.currentSelection?.();
    if (selection) return selection;
  } catch {}
  return undefined;
}

export const name = "agent-learning-delegation-policy";

export function apply(ctx) {
  const systemPrompt = typeof ctx.get === "function" ? ctx.get("systemPrompt") : undefined;
  if (!systemPrompt || typeof systemPrompt.section !== "function") return;

  const order = typeof systemPrompt.getSectionOrder === "function"
    ? safeOrder(systemPrompt)
    : 620;

  const dispose = systemPrompt.section({
    name: "agent-learning-delegation",
    order,
    text: TEXT,
  });
  if (typeof dispose === "function") ctx.effect(() => dispose);

  const thinkingDispose = systemPrompt.section({
    name: "agent-learning-thinking-language",
    order: order + 0.5,
    text: (context) => (isDeepSeekRoute(resolveRoute(context, ctx)) ? THINKING_TEXT : ""),
  });
  if (typeof thinkingDispose === "function") ctx.effect(() => thinkingDispose);
}

/** Place just after the repository prompt sections, before volatile context. */
function safeOrder(systemPrompt) {
  try {
    const base = systemPrompt.getSectionOrder("tools");
    return Number.isFinite(base) ? base + 1 : 620;
  } catch {
    return 620;
  }
}
