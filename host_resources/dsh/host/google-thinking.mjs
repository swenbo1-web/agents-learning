/** Optional Gemini 4 wire compatibility, only inside a bound model request. */
import { jsonPost, registerFetchTransform, requestUrl } from "./fetch-compat.mjs";

function rewrite(body) {
  const thinking = body?.generationConfig?.thinkingConfig;
  if (!thinking || typeof thinking !== "object" || thinking.thinkingLevel !== undefined
    || typeof thinking.thinkingBudget !== "number" || !Number.isFinite(thinking.thinkingBudget)) return false;
  const budget = thinking.thinkingBudget;
  delete thinking.thinkingBudget;
  // A nonpositive budget leaves the provider default intact; retain other fields.
  if (budget > 0) thinking.thinkingLevel = budget >= 16384 ? "HIGH" : budget >= 8192 ? "MEDIUM"
    : budget >= 2048 ? "LOW" : "MINIMAL";
  if (!Object.keys(thinking).length) delete body.generationConfig.thinkingConfig;
  return true;
}

export function apply(ctx, { enabled = false, requestScope = () => null } = {}) {
  if (!enabled) return;
  const dispose = registerFetchTransform((input, init) => {
    const scope = requestScope(), url = requestUrl(input);
    if (!scope || !["google", "google-vertex"].includes(scope.provider)
      || !/^gemini-4(?:[.-]|$)/i.test(scope.model) || !jsonPost(input, init) || !url
      || !["http:", "https:"].includes(url.protocol)
      || !/:streamGenerateContent$|:generateContent$/.test(url.pathname)) return init;
    const model = /\/models\/([^/:]+):/.exec(url.pathname)?.[1];
    if (!model || decodeURIComponent(model).toLowerCase() !== scope.model.toLowerCase()) return init;
    const body = JSON.parse(init.body);
    return rewrite(body) ? { ...init, body: JSON.stringify(body) } : init;
  });
  ctx.effect(() => dispose, "agent-learning: Google request compatibility");
}
