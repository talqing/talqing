// talqing code-execution service.
//
// A separate, credential-free container the worker calls like any other
// operation backend. Stateless JSON-in / JSON-out:
//   POST /transpile  {source}                      → {js} | 400 {error}
//   POST /execute    {js, input, timeout_ms}       → {ok, result|error, logs, duration_ms}
//   GET  /healthz                                  → {ok}
//
// Each /execute creates a fresh V8 isolate and disposes it after the run.
// Execution failures (script error / timeout / memory cap) return HTTP 200
// with ok:false so the worker can tell a script fault (graceful spoken
// fallback) from a transport fault. The service holds no platform secrets and
// its outbound network blocks private/metadata ranges (safefetch.js).
"use strict";

const http = require("node:http");
const esbuild = require("esbuild");
const { execute } = require("./runner");

const PORT = parseInt(process.env.PORT || "8090", 10);
const MAX_BODY_BYTES = 2 * 1024 * 1024;

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error(`request body exceeds ${MAX_BODY_BYTES} bytes`));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(new Error("request body must be JSON"));
      }
    });
    req.on("error", reject);
  });
}

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(data);
}

async function transpile(body) {
  const source = String(body.source || "");
  if (!source.trim()) {
    const err = new Error("source is empty");
    err.status = 400;
    throw err;
  }
  try {
    // transform = transpile only (strip types), no type-check, no bundling —
    // the authoring contract is a single self-contained handler
    const out = await esbuild.transform(source, {
      loader: "ts",
      format: "cjs",
      target: "es2022",
    });
    return { js: out.code };
  } catch (e) {
    const msgs = (e.errors || [])
      .map((m) => `${m.text}${m.location ? ` (line ${m.location.line})` : ""}`)
      .join("; ");
    const err = new Error(msgs || String(e.message || e));
    err.status = 400;
    throw err;
  }
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/healthz") {
      return send(res, 200, { ok: true });
    }
    if (req.method === "POST" && req.url === "/transpile") {
      return send(res, 200, await transpile(await readJson(req)));
    }
    if (req.method === "POST" && req.url === "/execute") {
      const body = await readJson(req);
      if (typeof body.js !== "string" || !body.js.trim()) {
        return send(res, 400, { error: "js (compiled script) is required" });
      }
      return send(res, 200, await execute(body));
    }
    send(res, 404, { error: "not found" });
  } catch (e) {
    send(res, e.status || 500, { error: String((e && e.message) || e) });
  }
});

server.listen(PORT, () => {
  console.log(`talqing code-exec listening on :${PORT}`);
});
