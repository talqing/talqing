#!/usr/bin/env node
/**
 * Talqing MCP server.
 *
 * Serves the platform's agent-building functions to any MCP client and
 * executes each call against the Talqing API with a personal access token. The
 * tool list is `tools.json` and the instructions are `SKILL.md` — both generated
 * from the same source as our own CoPilot's tools and prompt, so an agent
 * driving Talqing from here can do exactly what the in-product CoPilot can.
 *
 * A token acts as its creator, with that user's live role, inside their
 * workspace only. Nothing here can widen that.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type CallToolResult,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";

type ApiFunction = {
  name: string;
  method: string;
  path: string;
  description: string;
  role: "public" | "read" | "write" | "admin";
  read_only: boolean;
  parameters: Record<string, unknown>;
};

type ToolsFile = { api_version: string; tools: ApiFunction[] };

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

function readPackageFile(name: string): string {
  return readFileSync(join(PACKAGE_ROOT, name), "utf8");
}

/** Required environment. Missing configuration fails at startup, not mid-call. */
function required(name: string, hint: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is not set — ${hint}`);
  return value;
}

const BASE_URL = required(
  "TALQING_BASE_URL",
  "set it to your region's Talqing API URL, e.g. https://api.in.talqing.com",
).replace(/\/+$/, "");
const API_KEY = required(
  "TALQING_API_KEY",
  "create a personal access token in the dashboard under Organization → API Tokens",
);

const { tools: functions }: ToolsFile = JSON.parse(readPackageFile("tools.json"));
const byName = new Map(functions.map((fn) => [fn.name, fn]));

/* Read, not restated. A client shows this version when it reports the server it
   connected to, and a second copy of the number is one `npm version` away from
   being wrong. */
const { version: VERSION }: { version: string } = JSON.parse(readPackageFile("package.json"));

/** The skill, minus its frontmatter — handed to the client as server instructions. */
function instructions(): string {
  const markdown = readPackageFile("SKILL.md");
  const end = markdown.indexOf("\n---", 3);
  return (markdown.startsWith("---") && end !== -1 ? markdown.slice(end + 4) : markdown).trim();
}

function describe(fn: ApiFunction): Tool {
  return {
    name: fn.name,
    description: `${fn.method} ${fn.path}\n\n${fn.description}`,
    inputSchema: fn.parameters as Tool["inputSchema"],
    annotations: {
      readOnlyHint: fn.read_only,
      destructiveHint: fn.method === "DELETE",
      idempotentHint: ["GET", "PUT", "DELETE"].includes(fn.method),
      openWorldHint: false,
    },
  };
}

/**
 * Turn tool arguments into the HTTP request they stand for: `{name}`
 * placeholders in the path are path parameters, `body` is the JSON body, and
 * everything else left over is a query parameter.
 */
function requestFor(fn: ApiFunction, args: Record<string, unknown>): Request {
  const remaining = { ...args };

  const path = fn.path.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    const value = remaining[name];
    if (value === undefined || value === null) throw new Error(`${name} is required`);
    delete remaining[name];
    return encodeURIComponent(String(value));
  });

  const body = remaining.body;
  delete remaining.body;

  const url = new URL(BASE_URL + path);
  for (const [key, value] of Object.entries(remaining)) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }

  const headers: Record<string, string> = { authorization: `Bearer ${API_KEY}` };
  if (body !== undefined) headers["content-type"] = "application/json";

  return new Request(url, {
    method: fn.method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function failure(message: string, errors: string[] = []): CallToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify({ detail: { message, errors } }) }],
    isError: true,
  };
}

async function call(name: string, args: Record<string, unknown>): Promise<CallToolResult> {
  const fn = byName.get(name);
  if (!fn) return failure(`unknown function: ${name}`);

  let response: Response;
  try {
    response = await fetch(requestFor(fn, args));
  } catch (error) {
    // A bad argument (missing path parameter) and an unreachable API both land
    // here; either way the model gets a readable error in the API's own shape.
    return failure(error instanceof Error ? error.message : String(error));
  }

  const text = await response.text();
  if (!response.ok) {
    // Errors already carry {detail: {message, errors}} — pass them through so
    // one error shape reaches the model whichever function failed.
    return { content: [{ type: "text", text: text || response.statusText }], isError: true };
  }
  return { content: [{ type: "text", text: text || "{}" }] };
}

const server = new Server(
  { name: "talqing", version: VERSION },
  { capabilities: { tools: {} }, instructions: instructions() },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: functions.map(describe),
}));

server.setRequestHandler(CallToolRequestSchema, async (request) =>
  call(request.params.name, (request.params.arguments ?? {}) as Record<string, unknown>),
);

await server.connect(new StdioServerTransport());
