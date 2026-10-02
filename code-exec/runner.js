// Per-execution V8 isolate.
//
// Every /execute creates a fresh isolate + context, runs the compiled JS, then
// disposes the isolate. The service is stateless — every request carries the
// compiled JS (stored in the platform's tool_versions). No pool: a timeout
// only kills this run, never a sibling request.
//
// Each run gets a `fetch` shim (SSRF-guarded on the host), a captured
// `console`, a memory limit, and a wall-clock timeout that disposes the
// isolate (the only reliable way to stop a spinning / hung script).
"use strict";

const ivm = require("isolated-vm");
const { hostFetch } = require("./safefetch");

const ISOLATE_MEMORY_MB = parseInt(process.env.ISOLATE_MEMORY_MB || "128", 10);
const DEFAULT_TIMEOUT_MS = 5_000;
const MAX_TIMEOUT_MS = 30_000;
const MAX_LOG_LINES = 100;

// runs once per execution context: bridges console + fetch into the sandbox
// and sets up CommonJS shims for the esbuild-transpiled user code.
const BOOTSTRAP = `
  const __log = $0, __fetch = $1;
  const __fmt = (a) => a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");
  // applySync, not applyIgnored — ignored calls are queued and can land after
  // the run finishes, dropping the lines from the response
  const __emit = (...a) => __log.applySync(undefined, [__fmt(a)], { arguments: { copy: true } });
  globalThis.console = { log: __emit, info: __emit, warn: __emit, error: __emit };
  globalThis.fetch = async (url, opts = {}) => {
    const spec = {
      url: String(url),
      method: opts.method,
      headers: opts.headers,
      body: opts.body == null ? undefined
        : typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body),
    };
    const raw = await __fetch.apply(undefined, [JSON.stringify(spec)], {
      arguments: { copy: true },
      result: { promise: true, copy: true },
    });
    const r = JSON.parse(raw);
    if (r.error) throw new Error("fetch failed: " + r.error);
    return {
      status: r.status,
      ok: r.status >= 200 && r.status < 300,
      headers: r.headers,
      text: async () => r.body,
      json: async () => JSON.parse(r.body),
    };
  };
  globalThis.module = { exports: {} };
  globalThis.exports = globalThis.module.exports;
`;

// resolves the script's handler and invokes it; the returned promise is
// awaited host-side via result.promise
const INVOKE = `
  const m = globalThis.module && globalThis.module.exports;
  const handler = (m && (m.default || m.handler)) || globalThis.handler;
  if (typeof handler !== "function") {
    throw new Error(
      "script must export a function, e.g. 'export default async function handler(input) { ... }'"
    );
  }
  return handler($0);
`;

/**
 * Run compiled JS with a JSON input. Returns
 * {ok, result?, error?, logs, duration_ms} — execution failures are ok:false,
 * never a thrown error (the HTTP layer reserves non-200 for transport faults).
 */
async function execute({ js, input, timeout_ms }) {
  const t0 = Date.now();
  const timeout = Math.min(Math.max(Number(timeout_ms) || DEFAULT_TIMEOUT_MS, 100), MAX_TIMEOUT_MS);

  const logs = [];
  const onLog = (line) => {
    if (logs.length < MAX_LOG_LINES) logs.push(String(line).slice(0, 2000));
  };

  const isolate = new ivm.Isolate({ memoryLimit: ISOLATE_MEMORY_MB });
  let context;
  let timer;
  try {
    context = await isolate.createContext();
    // arguments.reference wraps each plain function in an ivm.Reference the
    // closure sees as $0/$1 (passing pre-made References would double-wrap)
    await context.evalClosure(BOOTSTRAP, [onLog, hostFetch], {
      arguments: { reference: true },
    });

    const script = await isolate.compileScript(String(js || ""), {
      filename: "tool-code.js",
    });

    const run = (async () => {
      await script.run(context, { timeout });
      return context.evalClosure(INVOKE, [input ?? {}], {
        arguments: { copy: true },
        result: { promise: true, copy: true },
        timeout,
      });
    })();

    // wall-clock cap: {timeout} above only stops synchronous V8 work; a hung
    // await (or a sync spin inside an async fn) is killed by disposing this
    // run's isolate — no other request shares it.
    const timedOut = new Promise((_, reject) => {
      timer = setTimeout(() => {
        try {
          isolate.dispose();
        } catch {
          /* already gone */
        }
        reject(new Error(`execution exceeded ${timeout}ms`));
      }, timeout);
    });

    const result = await Promise.race([run, timedOut]);
    return {
      ok: true,
      result: result === undefined ? null : result,
      logs,
      duration_ms: Date.now() - t0,
    };
  } catch (e) {
    return {
      ok: false,
      error: String((e && e.message) || e),
      logs,
      duration_ms: Date.now() - t0,
    };
  } finally {
    clearTimeout(timer);
    try {
      context?.release();
    } catch {
      /* isolate may be disposed */
    }
    if (!isolate.isDisposed) {
      try {
        isolate.dispose();
      } catch {
        /* already gone */
      }
    }
  }
}

module.exports = { execute };
