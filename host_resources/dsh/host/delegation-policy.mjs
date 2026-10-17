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
