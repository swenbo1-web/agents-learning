/**
 * Cross-provider assistant-history compatibility for the DeepSeek Messages route.
 *
 * `google-vertex` (pi-ai) appends a trailing empty `{ type: "text", text: "" }`
 * block after its tool-call blocks. DeepSeek's Anthropic-compatible `/messages`
 * endpoint rejects that shape with HTTP 400
 *   "… `tool_use` ids were found without `tool_result` blocks immediately after …"
 * because a text block sits between the tool_use and the following tool_result.
 *
 * This wrapper drops empty text blocks from tool-bearing assistant turns and
 * keeps `tool_use` last, so the history is representable on either provider.
 */

const MARK = "__alpMessagesSanitizer";
const ORIGINAL = "__alpOriginalFetch";

function sanitizeBody(body) {
  let modified = false;
  for (const message of body.messages) {
    if (!message || message.role !== "assistant" || !Array.isArray(message.content)) continue;
    if (message.content.length < 2) continue;
    if (!message.content.some((block) => block?.type === "tool_use")) continue;

    const kept = [];
    const toolUses = [];
    for (const block of message.content) {
      if (block?.type === "tool_use") {
        toolUses.push(block);
      } else if (block?.type === "text" && (typeof block.text !== "string" || block.text.length === 0)) {
        modified = true;
      } else {
        kept.push(block);
      }
    }
    const reordered = [...kept, ...toolUses];
    if (
      reordered.length !== message.content.length ||
      reordered.some((block, index) => block !== message.content[index])
    ) {
      message.content = reordered;
      modified = true;
    }
  }
  return modified;
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
        /\/messages(?:\?|$)/.test(url)
      ) {
        const body = JSON.parse(init.body);
        if (body && Array.isArray(body.messages) && sanitizeBody(body)) {
          init = { ...init, body: JSON.stringify(body) };
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
  }, "agent-learning: restore global fetch");
}
