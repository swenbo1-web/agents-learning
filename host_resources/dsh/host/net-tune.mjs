/**
 * Raise the transport-level body/header timeouts of the Host's global fetch.
 *
 * Node's built-in fetch (undici) defaults to `bodyTimeout: 300000` — five
 * minutes without a single byte on the response body tears the stream down with
 * `TypeError: terminated`. A high-reasoning model holding a very large context
 * routinely stays silent longer than that while it produces its next chunk, so
 * every attempt dies as `TRANSPORT / terminated` even though the provider is
 * still working.
 *
 * `@deepseek-ai/dsh-llm-pi-ai` already guards真 stalls with its own
 * `streamIdleTimeoutMs` watchdog, which is the layer that should own this
 * decision. This plugin lifts only the undici ceiling so the adapter watchdog
 * stays authoritative, and it preserves proxy behaviour by keeping the
 * environment-driven dispatcher class (`EnvHttpProxyAgent`) when available.
 */

import { createRequire } from "node:module";
import { existsSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

/** No-silence ceiling on the response body; the adapter watchdog stays tighter. */
const BODY_TIMEOUT_MS = 30 * 60 * 1000;
/** No-silence ceiling while waiting for the response headers. */
const HEADERS_TIMEOUT_MS = 10 * 60 * 1000;
/** TCP/TLS connect bound, applied per connection attempt. */
const CONNECT_TIMEOUT_MS = 60 * 1000;

const MARK = Symbol.for("alp.net-tune.dispatcher");

function dshHome() {
  const configured = process.env.DSH_HOME;
  return configured && configured.trim() ? configured : join(homedir(), ".dsh");
}

/** Candidate package roots that may carry a resolvable `undici`. */
function candidateRoots() {
  const roots = [];
  const profiles = join(dshHome(), "profiles");
  try {
    for (const name of readdirSync(profiles)) {
      const dir = join(profiles, name);
      if (existsSync(join(dir, "package.json"))) roots.push(dir);
    }
  } catch {}
  roots.push(join(dshHome(), "dsh-runtimes"));
  return roots;
}

function loadUndici() {
  const require = createRequire(import.meta.url);
  for (const root of candidateRoots()) {
    try {
      const resolved = require.resolve("undici", { paths: [root] });
      return require(resolved);
    } catch {}
    try {
      return require("undici");
    } catch {}
  }
  return undefined;
}

function report(facts) {
  try {
    writeFileSync(
      join(process.env.TEMP || process.env.TMP || ".", "alp-net-tune.json"),
      JSON.stringify(facts, null, 2),
      "utf8",
    );
  } catch {}
}

export function apply(ctx) {
  const facts = { applied: false, reason: null, dispatcher: null, bodyTimeoutMs: BODY_TIMEOUT_MS };
  const undici = loadUndici();
  if (!undici || typeof undici.setGlobalDispatcher !== "function") {
    facts.reason = "undici_unavailable";
    globalThis.__alpNetTune = facts;
    report(facts);
    return;
  }

  const { getGlobalDispatcher, setGlobalDispatcher, EnvHttpProxyAgent, Agent } = undici;
  const previous = getGlobalDispatcher();
  if (previous && previous[MARK] === true) {
    facts.applied = true;
    facts.reason = "already_applied";
    facts.dispatcher = previous.constructor?.name ?? "unknown";
    globalThis.__alpNetTune = facts;
    report(facts);
    return;
  }

  // Keep the environment-driven proxy behaviour: HTTP(S)_PROXY are read by the
  // agent itself, so honouring them here means the tuning cannot silently
  // bypass a corporate or local proxy that the deployment depends on.
  const Ctor = typeof EnvHttpProxyAgent === "function" ? EnvHttpProxyAgent : Agent;
  let dispatcher;
  try {
    dispatcher = new Ctor({
      bodyTimeout: BODY_TIMEOUT_MS,
      headersTimeout: HEADERS_TIMEOUT_MS,
      connect: { timeout: CONNECT_TIMEOUT_MS },
    });
  } catch (error) {
    facts.reason = "construct_failed:" + String(error?.message || error);
    globalThis.__alpNetTune = facts;
    report(facts);
    return;
  }

  try {
    Object.defineProperty(dispatcher, MARK, { value: true, enumerable: false });
    setGlobalDispatcher(dispatcher);
  } catch (error) {
    facts.reason = "install_failed:" + String(error?.message || error);
    globalThis.__alpNetTune = facts;
    report(facts);
    return;
  }

  facts.applied = true;
  facts.reason = "installed";
  facts.dispatcher = Ctor.name;
  facts.previousDispatcher = previous?.constructor?.name ?? null;
  globalThis.__alpNetTune = facts;
  report(facts);

  ctx.effect(() => () => {
    const current = getGlobalDispatcher();
    if (current !== dispatcher) return;
    try {
      setGlobalDispatcher(previous && typeof previous.dispatch === "function" ? previous : new Agent());
      globalThis.__alpNetTune = { applied: false, reason: "disposed", dispatcher: null };
    } catch {}
  }, "agent-learning: restore global dispatcher");
}
