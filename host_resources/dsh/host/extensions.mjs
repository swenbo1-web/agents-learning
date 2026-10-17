import { AsyncLocalStorage } from "node:async_hooks";
import { apply as delegation } from "./delegation-policy.mjs";
import { apply as googleThinking } from "./google-thinking.mjs";
import { apply as messagesCompat } from "./messages-compat.mjs";
import { apply as netTune } from "./net-tune.mjs";

export function extensionConfig(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).some(key => !["googleThinking", "messagesCompat", "netTune", "delegation", "thinkingLanguage"].includes(key)))
    throw new TypeError("invalid Agent Learning host extensions");
  const result = { googleThinking: false, messagesCompat: false, netTune: false, thinkingLanguage: "off", ...value };
  for (const key of ["googleThinking", "messagesCompat", "netTune"])
    if (typeof result[key] !== "boolean") throw new TypeError(`invalid Agent Learning ${key}`);
  if (!["off", "zh-CN"].includes(result.thinkingLanguage)) throw new TypeError("invalid Agent Learning thinking language");
  const policy = value.delegation === undefined ? {} : value.delegation;
  if (!policy || typeof policy !== "object" || Array.isArray(policy)
    || Object.keys(policy).some(key => !["enabled", "provider", "model", "maxAgents"].includes(key)))
    throw new TypeError("invalid Agent Learning delegation policy");
  result.delegation = { enabled: false, provider: "", model: "", maxAgents: 2, ...policy };
  const d = result.delegation;
  if (typeof d.enabled !== "boolean" || !Number.isInteger(d.maxAgents) || d.maxAgents < 1 || d.maxAgents > 4
    || typeof d.provider !== "string" || typeof d.model !== "string" || Boolean(d.provider) !== Boolean(d.model)
    || d.provider && (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/.test(d.provider) || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(d.model)))
    throw new TypeError("invalid Agent Learning delegation route or limit");
  return result;
}

export function applyExtensions(ctx, config, allowed) {
  const disposers = [];
  let live = true;
  const stop = () => {
    live = false;
    while (disposers.length) { try { disposers.pop()(); } catch {} }
  };
  const scope = new AsyncLocalStorage();
  const local = { baseUrl: ctx.baseUrl, get: key => ctx.get?.(key), effect: factory => {
    const dispose = factory();
    if (typeof dispose === "function") disposers.push(dispose);
    return dispose;
  } };
  const permitted = context => live && allowed(context);
  try {
    delegation(local, config, permitted);
    if (config.googleThinking || config.messagesCompat || config.netTune) {
      if (typeof ctx.on !== "function") return stop;
      const dispose = ctx.on("llm/stream", (options, next) => {
        let agent;
        try { agent = ctx.get?.("agents")?.currentInitiator?.(); } catch {}
        if (!permitted({ agent, sessionId: options?.sessionId })) return next();
        const state = { provider: options.provider, model: options.model };
        return (async function* () {
          const iterator = scope.run(state, () => next()[Symbol.asyncIterator]());
          let finished = false;
          try {
            while (true) {
              const item = await scope.run(state, () => iterator.next());
              if (item.done) { finished = true; return; }
              yield item.value;
            }
          } finally {
            if (!finished && iterator.return) await scope.run(state, () => iterator.return());
          }
        })();
      });
      if (typeof dispose === "function") disposers.push(dispose);
      const requestScope = () => live ? scope.getStore() : null;
      googleThinking(local, { enabled: config.googleThinking, requestScope });
      messagesCompat(local, { enabled: config.messagesCompat, requestScope });
      netTune(local, { enabled: config.netTune, requestScope });
    }
    return stop;
  } catch (error) { stop(); throw error; }
}
