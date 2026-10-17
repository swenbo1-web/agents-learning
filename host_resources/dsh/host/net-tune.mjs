/** Optional per-request transport ceilings; never replaces the host dispatcher. */
import { createRequire } from "node:module";
import { registerFetchTransform, requestUrl } from "./fetch-compat.mjs";

function loadUndici(ctx) {
  try {
    // Use the actual host profile, never another profile or the evidence cwd.
    const anchor = new URL("package.json", ctx.baseUrl);
    if (anchor.protocol !== "file:") return;
    return createRequire(anchor)("undici");
  } catch { return undefined; }
}

export function apply(ctx, { enabled = false, requestScope = () => null } = {}) {
  if (!enabled) return;
  const undici = loadUndici(ctx);
  if (typeof undici?.getGlobalDispatcher !== "function") return;
  const delegates = new WeakMap();
  const dispose = registerFetchTransform((input, init) => {
    const scope = requestScope(), url = requestUrl(input);
    if (!scope || !url || !["http:", "https:"].includes(url.protocol)
      || String(init?.method ?? input?.method ?? "GET").toUpperCase() !== "POST"
      || !/(?:\/(?:messages|chat\/completions|responses)\/?|:(?:streamGenerateContent|generateContent))$/.test(url.pathname)
      || init?.dispatcher !== undefined) return init;
    const base = undici.getGlobalDispatcher();
    if (!base || typeof base.dispatch !== "function") return init;
    if (!delegates.has(base)) delegates.set(base, {
      dispatch(options, handler) {
        // Preserve connection, proxy and TLS behavior, including custom agents.
        // This delegate owns no sockets and never closes the host's dispatcher.
        return base.dispatch({ ...options, bodyTimeout: 30 * 60 * 1000, headersTimeout: 10 * 60 * 1000 }, handler);
      },
    });
    return { ...init, dispatcher: delegates.get(base) };
  });
  ctx.effect(() => dispose, "agent-learning: per-request transport ceilings");
}
