// SSRF-guarded outbound fetch for sandboxed tool code.
//
// Mirrors the HTTP operation's destination policy (backend/common/tooling.py):
// http(s) only, and every resolved address must be public — private ranges,
// loopback, link-local (cloud metadata), CGNAT, multicast and reserved space
// are rejected. The guard lives in the *connection* path (a custom DNS lookup
// handed to undici), so a rebinding second resolution can't redirect the
// actual socket to an internal address. Redirects are not followed.
"use strict";

const dns = require("node:dns");
const net = require("node:net");
const { Agent, request } = require("undici");

const MAX_RESPONSE_BYTES = 1 * 1024 * 1024; // 1 MB body cap
const FETCH_TIMEOUT_MS = 10_000;

const ALLOW_HOSTS = new Set(
  (process.env.SSRF_ALLOW_HOSTS || "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean)
);

// ── private/internal address checks ─────────────────────────────────────────

function ipv4ToInt(ip) {
  const p = ip.split(".").map(Number);
  return ((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3];
}

function inCidr4(ip, base, bits) {
  const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
  return (ipv4ToInt(ip) & mask) === (ipv4ToInt(base) & mask);
}

const PRIVATE_V4 = [
  ["0.0.0.0", 8],       // "this network"
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],   // CGNAT
  ["127.0.0.0", 8],     // loopback
  ["169.254.0.0", 16],  // link-local incl. cloud metadata 169.254.169.254
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],   // benchmarking
  ["224.0.0.0", 4],     // multicast
  ["240.0.0.0", 4],     // reserved + broadcast
];

function isPrivateIp(addr) {
  const family = net.isIP(addr);
  if (family === 4) return PRIVATE_V4.some(([b, n]) => inCidr4(addr, b, n));
  if (family === 6) {
    const ip = addr.toLowerCase();
    // v4-mapped (::ffff:a.b.c.d) → check the embedded v4
    const m = ip.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
    if (m) return isPrivateIp(m[1]);
    if (ip === "::" || ip === "::1") return true;       // unspecified / loopback
    if (ip.startsWith("fe8") || ip.startsWith("fe9") || ip.startsWith("fea") || ip.startsWith("feb")) return true; // link-local fe80::/10
    if (ip.startsWith("fc") || ip.startsWith("fd")) return true; // ULA fc00::/7
    if (ip.startsWith("ff")) return true;               // multicast
    return false;
  }
  return true; // unparseable → reject
}

// ── guarded connection path ──────────────────────────────────────────────────

// custom lookup used by undici's connector: every DNS answer is vetted before
// the socket connects, which closes the resolve-then-connect (rebinding) TOCTOU
function safeLookup(hostname, options, callback) {
  if (ALLOW_HOSTS.has(String(hostname).toLowerCase())) {
    return dns.lookup(hostname, options, callback);
  }
  dns.lookup(hostname, { all: true, family: options && options.family }, (err, addrs) => {
    if (err) return callback(err);
    const bad = addrs.find((a) => isPrivateIp(a.address));
    if (bad) {
      return callback(new Error(`destination ${bad.address} is not allowed (private/internal)`));
    }
    if (!addrs.length) return callback(new Error(`could not resolve host ${hostname}`));
    if (options && options.all) return callback(null, addrs);
    callback(null, addrs[0].address, addrs[0].family);
  });
}

const dispatcher = new Agent({
  connect: { lookup: safeLookup, timeout: FETCH_TIMEOUT_MS },
  headersTimeout: FETCH_TIMEOUT_MS,
  bodyTimeout: FETCH_TIMEOUT_MS,
  maxRedirections: 0, // never follow — a redirect to an internal address dies here
});

async function readCapped(body) {
  const chunks = [];
  let size = 0;
  for await (const chunk of body) {
    size += chunk.length;
    if (size > MAX_RESPONSE_BYTES) {
      body.destroy?.();
      throw new Error(`response exceeds ${MAX_RESPONSE_BYTES} bytes`);
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

// The host side of the isolate's `fetch` shim. Takes/returns JSON strings so
// the isolated-vm bridge stays a plain string copy. Never throws — errors come
// back as {error} and surface inside the isolate as a thrown Error.
async function hostFetch(specJson) {
  try {
    const spec = JSON.parse(specJson);
    const url = new URL(spec.url);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error("URL must be http(s)");
    }
    const host = url.hostname.replace(/^\[|\]$/g, "");
    // literal-IP hosts bypass DNS lookup entirely — vet them here
    if (net.isIP(host) && !ALLOW_HOSTS.has(host) && isPrivateIp(host)) {
      throw new Error(`destination ${host} is not allowed (private/internal)`);
    }
    const res = await request(url, {
      method: spec.method || "GET",
      headers: spec.headers || undefined,
      body: spec.body ?? undefined,
      dispatcher,
    });
    const body = await readCapped(res.body);
    return JSON.stringify({ status: res.statusCode, headers: res.headers, body });
  } catch (e) {
    return JSON.stringify({ error: String((e && e.message) || e) });
  }
}

module.exports = { hostFetch, isPrivateIp };
