/** Optional empty-block cleanup on an explicitly scoped DeepSeek route. */
import { jsonPost, registerFetchTransform, requestUrl } from "./fetch-compat.mjs";

function sanitize(body) {
  if (!Array.isArray(body?.messages)) return false;
  let changed = false;
  for (const message of body.messages) {
    if (message?.role !== "assistant" || !Array.isArray(message.content)
      || !message.content.some(block => block?.type === "tool_use")) continue;
    // Nonempty text, reasoning/signatures and tool calls keep their exact order.
    const kept = message.content.filter(block => !(block?.type === "text" && block.text === ""));
    if (kept.length !== message.content.length) {
      message.content = kept;
      changed = true;
    }
  }
  return changed;
}

export function apply(ctx, { enabled = false, requestScope = () => null } = {}) {
  if (!enabled) return;
  const dispose = registerFetchTransform((input, init) => {
    const scope = requestScope(), url = requestUrl(input);
    if (!scope || !/^deepseek(?:-|$)/i.test(scope.provider) || !jsonPost(input, init)
      || !url || !["http:", "https:"].includes(url.protocol) || !/\/messages\/?$/.test(url.pathname)) return init;
    const body = JSON.parse(init.body);
    return sanitize(body) ? { ...init, body: JSON.stringify(body) } : init;
  });
  ctx.effect(() => dispose, "agent-learning: Messages request compatibility");
}
