# Talqing MCP server

Build Talqing AI voice, video and text agents from Claude Code, Codex, or any
other MCP client.

This is the same surface Talqing's own in-product CoPilot works from — the same
operations and the same platform knowledge, generated from one source. Anything
the CoPilot can build, your coding agent can build.

## What you get

One operation for everything a workspace is built out of. Agents and agent
tasks, tools and their operation trees, integrations and triggers, carrier
accounts and phone numbers, secrets, provider keys and webhooks; then placing outbound calls, running call and email batches,
talking to text agents, and reading back transcripts, cost and latency. Each one
maps to a Talqing API endpoint, and its arguments mirror that HTTP call: path and
query parameters at the top level, request body under `body`.

A few request types are large enough that carrying them in every tool that
accepts one would cost more context than the whole rest of the server — an
agent's config, its override, a team, a task's config, a tool's operation tree.
Those arguments name their shape instead of inlining it, and `describe_schema`
serves the real thing when it is needed.

The server also hands the client `SKILL.md`, which explains how agents are put
together on Talqing — draft and publish, the operation tree, hooks, channels and
model selection. Your agent gets that automatically as server instructions.

## Setup

Open the Talqing dashboard. The **Agents** page has your token and a
ready-to-paste command for each client — the snippets below are what it shows.
The token authenticates as you, acts with your current role, and reaches only
your workspace.

**`TALQING_BASE_URL` names a region** — `https://api.in.talqing.com`,
`https://api.us.talqing.com` — and everything this server can reach belongs to
that one: an agent, its calls, its phone numbers and its credit balance are all
the region's. One token reaches every region — it names your workspace, not a
place — so working in another region is the same token and a different base URL.
The dashboard's snippet always carries the region you are currently looking at;
switch region there and copy it again.

**Claude Code**

```bash
claude mcp add talqing \
  -e TALQING_BASE_URL=https://api.in.talqing.com \
  -e TALQING_API_KEY=tq_your_token \
  -- npx -y @talqing/mcp
```

**Codex**

```bash
codex mcp add talqing \
  --env TALQING_BASE_URL=https://api.in.talqing.com \
  --env TALQING_API_KEY=tq_your_token \
  -- npx -y @talqing/mcp
```

**Grok** — `grok mcp add` takes no `--env`, so this one goes in
`~/.grok/config.toml`:

```toml
[mcp_servers.talqing]
command = "npx"
args = ["-y", "@talqing/mcp"]
env = { TALQING_BASE_URL = "https://api.in.talqing.com", TALQING_API_KEY = "tq_your_token" }
```

**Any other MCP client** — a stdio server, configured the usual way:

```json
{
  "mcpServers": {
    "talqing": {
      "command": "npx",
      "args": ["-y", "@talqing/mcp"],
      "env": {
        "TALQING_BASE_URL": "https://api.in.talqing.com",
        "TALQING_API_KEY": "tq_your_token"
      }
    }
  }
}
```

Both variables are required — there is no default host, so the server can never
quietly talk to the wrong workspace.

## Permissions

The server adds no permissions of its own. Every call is your API call: your
workspace, your role. A view-only member gets read operations and a clear 403 on
the rest, exactly as they would through the dashboard.

Operations are annotated so clients can tell reads from writes — `readOnlyHint`
on every GET, `destructiveHint` on every delete.

Four things are deliberately out of reach:

- **Sign-in, organizations, members and roles, and personal access tokens.**
  Those live on a separate control API that this server holds no URL for, so an
  AI builder cannot mint itself a credential, and who is in the workspace stays
  where a human is in the loop.
- **Buying credits.** Reading the balance would be harmless; starting a payment
  spends the workspace's money, and neither belongs on a coding agent's surface.
- **Streaming endpoints**, which are not tool-shaped. Each has a polling
  equivalent here — `list_conversation_items` for a conversation — so nothing is
  unreachable.
- **Deleting a call or its recording.** Every other `delete_*` removes
  configuration you could write again; this one destroys the record of something
  that happened, and can take a contact's memory of past calls with it.

Some operations act on the real world. `create_outbound_call` rings a phone and
costs money, and a message to a connected channel is one somebody receives.
`SKILL.md` tells the model to do neither without being asked for that specific
contact, but a client's own approval prompts are still worth leaving on.

## Running from source

If you have cloned this repo, point the client at your own build instead of
`npx`:

```bash
cd mcp && npm install && npm run build
```

```json
{
  "mcpServers": {
    "talqing": {
      "command": "node",
      "args": ["/absolute/path/to/talqing/mcp/dist/index.js"],
      "env": {
        "TALQING_BASE_URL": "https://api.in.talqing.com",
        "TALQING_API_KEY": "tq_your_token"
      }
    }
  }
}
```

`npm install -g ./mcp` works too, and puts a `talqing-mcp` command on your PATH.

## Development

`tools.json` and `SKILL.md` are generated from the backend, so the MCP surface
cannot drift from the product:

```bash
PYTHONPATH=backend python openapi/export.py
```

`tools.json` comes from `backend/api/dataplane/operations.py`, the same registry
the CoPilot binds its tools from; `SKILL.md` comes from
`backend/services/copilot/skill.md`, the same text the CoPilot is prompted with.
Edit those, re-run the export, and both surfaces move together.

```bash
npm install
npm run build
npm run typecheck
```
