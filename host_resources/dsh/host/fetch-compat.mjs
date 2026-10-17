// One reversible fetch wrapper shared by the optional, request-scoped adapters.
let installed;

export function requestUrl(input) {
  try { return new URL(typeof input === "string" || input instanceof URL ? input : input?.url); }
  catch { return null; }
}

export function jsonPost(input, init) {
  return String(init?.method ?? input?.method ?? "GET").toUpperCase() === "POST"
    && typeof init?.body === "string" && init.body.length <= 4 * 1024 * 1024;
}

export function registerFetchTransform(transform) {
  if (typeof globalThis.fetch !== "function") return () => {};
  if (!installed?.transforms.size) {
    const base = globalThis.fetch;
    const state = { transforms: new Map(), base };
    state.wrapper = function fetch(input, init) {
      let effective = init;
      for (const apply of state.transforms.values()) {
        try { effective = apply(input, effective) ?? effective; } catch {}
      }
      return base.call(this, input, effective);
    };
    installed = state;
    globalThis.fetch = state.wrapper;
  }
  const state = installed;
  const key = Symbol();
  state.transforms.set(key, transform);
  return () => {
    state.transforms.delete(key);
    if (!state.transforms.size && globalThis.fetch === state.wrapper) globalThis.fetch = state.base;
  };
}
