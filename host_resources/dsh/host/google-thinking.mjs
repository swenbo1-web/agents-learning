/**
 * Google wire-format compatibility: budget-based thinking → level-based thinking.
 *
 * The bundled `@earendil-works/pi-ai` chooses the Google thinking wire format by
 * matching the model id against a fixed list (`google-shared.js`):
 *
 *   /gemini-3(?:\.\d+)?-(?:pro|flash)/ | gemini-flash-latest |
 *   gemini-flash-lite-latest | /gemma-?4/
 *
 * `gemini-4-argon` matches none of them, so the adapter sends
 * `generationConfig.thinkingConfig.thinkingBudget`, which Gemini 4 rejects:
 *
 *   400 INVALID_ARGUMENT "Thinking budget is not supported for this model.
 *   Use thinking level instead."
 *
 * This wrapper rewrites only that case — a `gemini-4*` request that carries a
 * thinking budget and no thinking level — into the equivalent discrete level
 * (`MINIMAL` / `LOW` / `MEDIUM` / `HIGH`). Everything else passes untouched, so
 * budget-based Gemini 2.5 models keep working exactly as before.
 */

const MARK = "__alpGoogleThinkingCompat";
const ORIGINAL = "__alpGoogleThinkingOriginalFetch";
const LEVEL_MODEL = /gemini-3(?:\.\d+)?-(?:pro|flash)|gemini-flash-latest|gemini-flash-lite-latest|gemma-?4/;

function toLevel(budget) {
  if (typeof budget !== "number" || !Number.isFinite(budget)) return undefined;
  if (budget >= 16384) return "HIGH";
  if (budget >= 8192) return "MEDIUM";
  if (budget >= 2048) return "LOW";
  return "MINIMAL";
}

function modelIdOf(url) {
  const match = /models\/([^:/?]+)/.exec(url);
  return match ? match[1].toLowerCase() : "";
}

function rewrite(body, modelId) {
  const config = body?.generationConfig;
  const thinking = config?.thinkingConfig;
  if (!thinking || typeof thinking !== "object") return false;
  if (thinking.thinkingLevel !== undefined) return false;
  if (thinking.thinkingBudget === undefined) return false;

  if (thinking.thinkingBudget <= 0) {
    delete config.thinkingConfig;
    return true;
  }
  const level = toLevel(thinking.thinkingBudget);
  if (level === undefined) return false;
  delete thinking.thinkingBudget;
  thinking.thinkingLevel = level;
  return true;
}

export function apply(ctx) {
  const current = globalThis.fetch;
  if (typeof current !== "function" || current[MARK] === true) return;

  const base = current[ORIGINAL] || current;

  const wrapped = function fetch(input, init) {
    try {
      const url = typeof input === "string" ? input : input?.url ? String(input.url) : "";
      if (
        init?.method === "POST" &&
        typeof init?.body === "string" &&
        /:streamGenerateContent|:generateContent/.test(url)
      ) {
        const modelId = modelIdOf(url);
        if (modelId.startsWith("gemini-4") && !LEVEL_MODEL.test(modelId)) {
          const body = JSON.parse(init.body);
          if (body && rewrite(body, modelId)) {
            init = { ...init, body: JSON.stringify(body) };
            globalThis.__alpGoogleThinkingCompat = { applied: true, modelId, at: Date.now() };
          }
        }
      }
    } catch {}
    return base.call(this, input, init);
  };

  wrapped[MARK] = true;
  wrapped[ORIGINAL] = base;
  globalThis.fetch = wrapped;

  ctx.effect(() => () => {
    if (globalThis.fetch === wrapped) globalThis.fetch = base;
  }, "agent-learning: restore global fetch for google thinking");
}
