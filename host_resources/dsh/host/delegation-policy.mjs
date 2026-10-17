/** Optional project guidance. It grants no execution or model authority. */
export const name = "agent-learning-delegation-policy";

function routeOf(context) {
  const agent = context?.agent;
  try {
    const route = agent?.session?.requestContext?.();
    if (route?.provider && route?.model) return route;
  } catch {}
  return agent?.options;
}

function delegationText(config, context) {
  const current = routeOf(context);
  if (config.provider && current?.provider === config.provider && current.model === config.model) return "";
  const route = config.provider
    ? `项目明确选择的实现路由为 provider=${config.provider}, model=${config.model}；仅在工具支持这些参数时使用。`
    : "子代理继承当前模型；不自行切换供应商或猜测模型标识。";
  return `## 项目可选委派策略
仅在当前用户任务允许委派、宿主提供相应工具时，将独立的实现或批量检查下派给子代理。
架构判断、接口约定和最终验收由主代理负责。单文件小改动、单次读取或强依赖当前上下文的工作直接处理。
${route}
同一时刻最多 ${config.maxAgents} 个子代理。派发需明确目标、文件边界和验证标准，回传修改摘要及实际验证结果。
工具不可用时由当前代理继续；不得借委派扩大任务授权、绕过执行保护或重复派发同一工作。`;
}

export function apply(ctx, config = {}, allowed = () => false) {
  const systemPrompt = ctx.get?.("systemPrompt");
  if (!systemPrompt?.section) return;
  if (config.delegation?.enabled) {
    const dispose = systemPrompt.section({ name: "agent-learning-delegation", order: 620,
      text: context => allowed(context) ? delegationText(config.delegation, context) : "" });
    if (typeof dispose === "function") ctx.effect(() => dispose);
  }
  if (config.thinkingLanguage === "zh-CN") {
    const dispose = systemPrompt.section({ name: "agent-learning-thinking-language", order: 620.5,
      text: context => {
        if (!allowed(context)) return "";
        const route = routeOf(context);
        if (!route || !/^deepseek(?:-|$)/i.test(route.provider ?? "")
          && !/^deepseek(?:-|$)/i.test(route.model ?? "")) return "";
        return "使用简体中文组织说明与结论；代码、标识符、命令、报错、路径和协议字段保持原文。无需披露内部思考。";
      } });
    if (typeof dispose === "function") ctx.effect(() => dispose);
  }
}
