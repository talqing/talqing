# Talqing docs — writing brief

**Every agent writing a page in `documentation/` must read this file first, and
`documentation/SITEMAP.md` for the page map.**

This is customer-facing documentation for talqing.com, published at
`docs.talqing.com`. It is read by developers and by operators evaluating whether
to move their voice agents onto us. Assume they are comparing us to Vapi and
ElevenLabs Agents while they read.

---

## 1. The one rule

**Never write a sentence you have not verified in the code.**

You are documenting a shipping product, not describing an intention. Before you
document a field, a status value, a default, a limit or an error string, open
the file that defines it and read it. If a number appears in a page, it came out
of `backend/catalog.yaml`, a Pydantic model, a constant or a migration — not out
of your head.

Corollaries:

- **Do not document what is not shipped.** If you cannot find it in the code, it
  does not exist. `product.md` at the repo root is an internal gap analysis; use
  it only to *avoid* documenting things (it marks unshipped work with 🔴 / ❌),
  never as a source of features. Known non-features as of this writing: SMS,
  live call monitoring/barge-in, staged
  draft→test→promote environments, automated agent testing, an audit log,
  concurrency/spend caps, and an embeddable chat widget. Do not
  mention any of them, not even as "coming soon".

  DTMF **has** shipped and is documented at `agents/keypad.mdx` — both
  directions, on phone calls and on media streams. So has a duration cap:
  three hours on every voice and video call, lowered per agent by
  `max_duration_seconds` (`agents/silence-and-call-length.mdx`). So has
  voicemail detection on outbound calls (`agents/voicemail.mdx`). Read those
  pages before writing about any of them.
- **Do not soften a limitation.** If a missed call is billed like an answered
  one, the page says so in bold. Customers forgive a documented limit and never forgive
  an undocumented one.
- **When two sources disagree, the code wins.** Flag the disagreement at the end
  of your report to the orchestrator instead of picking one silently.

## 2. Audience and voice

Write like a senior engineer explaining the system to a peer who has to ship on
it this week.

- Direct, concrete, second person ("you"). Present tense.
- Lead with what the reader is trying to do, then how, then the caveats.
- Short paragraphs. No throat-clearing ("In this guide, we will explore…").
- No marketing adjectives — no *powerful*, *seamless*, *robust*, *simply*,
  *just*, *easy*. If something is hard, say what makes it hard.
- Explain the *why* when it changes what the reader should do, and only then.
- No emoji. No exclamation marks.
- Use "workspace" in prose for the tenant/organization. Use "the dashboard" for
  the web app.

## 3. Format

Mintlify MDX. Every page starts with frontmatter:

```mdx
---
title: "Phone numbers"
description: "Import a number from your carrier and point an agent at it."
---
```

`title` is 1–4 words, sentence case. `description` is one sentence, under 140
characters, and is what search and the page header show.

Available components (use them, sparingly and for a reason):

- `<Note>`, `<Warning>`, `<Tip>`, `<Info>` — a `<Warning>` is for something that
  costs money, reaches a real person, or cannot be undone.
- `<Steps>` with `<Step title="…">` — for ordered procedures.
- `<CodeGroup>` — for the same example in several languages.
- `<Card>` / `<CardGroup cols={2}>` — for next-step links at the end of hub pages.
- `<AccordionGroup>` / `<Accordion>` — for reference detail that would otherwise
  bury the page (per-provider setup steps, long enum tables).
- `<ParamField path="name" type="string" required>` — for parameter reference.
- `<ResponseField name="…" type="…">` — for response shapes.
- Markdown tables — for enums, statuses and comparison matrices. Prefer a table
  over a bulleted list whenever there are three or more columns of fact.

Do not invent components. Do not use raw HTML. Do not use `<Frame>`/images —
there are no screenshots yet; describe the UI in words instead.

## 4. Code samples

Show **cURL, TypeScript and Python** for anything that touches the API, inside a
`<CodeGroup>`, in that order. Dashboard-only workflows get prose steps instead.

```mdx
<CodeGroup>
```bash cURL
curl https://api.in.talqing.com/v1/agents \
  -H "Authorization: Bearer $TALQING_API_KEY"
```

```typescript TypeScript
const agents = await talqing.agents.list();
```

```python Python
agents = talqing.agents.list()
```
</CodeGroup>
```

Rules for samples:

- Use the real SDK call shapes. TypeScript takes one flat object and keys the
  body by its schema name (`talqing.agents.create({ createAgentRequest: { … } })`).
  Python takes path params positionally and everything else as keyword arguments
  (`talqing.agents.create(config={…})`). Verify against
  `clients/typescript/README.md`, `clients/python/README.md` and the generated
  `clients/*/src/**/api.*` before writing a call you have not seen.
- Samples must be complete enough to paste and run, minus the token.
- Use `$TALQING_API_KEY` for the token and `https://api.in.talqing.com` for the
  base URL. Never a placeholder like `YOUR_API_KEY` inline in a header value.
- Identifiers in examples read like real ones, not `xxx`: `agt_…`, `tool_…`.
  Check the actual id prefixes in the code before using one; if ids are bare
  UUIDs, use a realistic UUID.
- JSON examples must validate against the real schema in `openapi/openapi.json`.
- Every `json_schema`, config or operation-tree example must be one you could
  actually POST. No pseudo-fields.

## 5. Hard facts (do not re-derive these)

| Fact | Value |
| --- | --- |
| Dashboard | `https://app.talqing.com` |
| API base URL, India region | `https://api.in.talqing.com` |
| API base URL, US region | `https://api.us.talqing.com` |
| API version prefix | `/v1` |
| Auth header | `Authorization: Bearer <personal access token>` |
| npm packages | `@talqing/sdk` (API), `@talqing/react` and `@talqing/client` (web calls) |
| PyPI package | `talqing` |
| MCP package | `@talqing/mcp` |
| Support | `hello@talqing.com` |

Everything a workspace owns is **regional**: agents, tools, tasks, calls, phone
numbers and the credit balance all belong to the base URL they were created
against. A token is not regional — the same token works in every region. Say
this wherever a reader could get it wrong.

Talqing is **strictly bring-your-own-key**: we never hold provider credentials
of our own, provider spend goes to the customer's own accounts, and Talqing
charges only a per-minute platform fee on voice and video. Text carries no
platform fee. Never imply a bundled or resold model price.

## 6. Vocabulary

Use these words and no synonyms:

| Use | Not |
| --- | --- |
| agent | assistant, bot |
| agent task | workflow, job |
| tool | function, action |
| operation (in a tool's tree) | step, node |
| handoff | transfer, when it is agent → agent |
| transfer | handoff, when it is agent → human |
| conversation | thread |
| call | session, in prose |
| workspace | tenant, org, account |
| published / draft | live / staged |
| provider key (BYOK) | API key, when it means the customer's OpenAI/Deepgram/etc. key |
| personal access token | API key, when it means the Talqing token |

A **conversation** is the thread with one person across surfaces; a **call** is
one voice or video session; a **session** is one agent's participation in a
conversation. Keep those three straight.

## 7. Page shape

Every page:

1. Opens with 1–3 sentences saying what this is and when you need it. No heading
   above it.
2. Gets to the first concrete thing — a table, a snippet or a `<Steps>` — within
   a screen.
3. Ends with a short "Next" `<CardGroup>` of 2–3 links, except reference pages.

Reference sections (every field of a config, every enum value) go in a table
near the bottom, not interleaved with the explanation.

Hub/overview pages are shorter than the pages under them and exist to route the
reader.

Target lengths: overview pages 300–600 words; concept pages 700–1500; reference
pages as long as the reference is; guides 1200–2500 with a working end-to-end
result.

## 8. Cross-linking

Link with root-relative paths that match `docs.json` exactly, no `.mdx`
extension: `[handoffs](/teams/handoffs)`. Check `SITEMAP.md` for the canonical
path of any page you link. It is fine to link a page another agent is writing —
write the link, do not write the content.

Mintlify generates one page per endpoint from `openapi.json`, at
`/api-reference/<tag>/<operation-slug>` — the tag is the OpenAPI tag and the slug
is the operation's title lowercased and hyphenated. So `POST /v1/agents` is
`/api-reference/agents/create-agent`, and `GET /health` is
`/api-reference/service/health`. Verified against the running preview.

Link one directly when you are naming a specific endpoint. If you are unsure of a
slug, link the hub `/api-reference/introduction` and name the endpoint in text
(`POST /v1/agents/{agent_id}/publish`) rather than guessing.

## 9. What never goes in these pages

- Internal file paths, module names, table names, or "we implemented this as…".
- Engineering rationale that only makes sense to us, competitor comparisons,
  roadmap, pricing we have not shipped, or anything from `TODOs.md` /
  `product.md` / `roadmap/`.
- Anything that reveals another customer, an internal hostname, a real
  credential, or an employee's name.
- Apologies for missing features. State the limit and move on.

## 10. Deliverable

Write the `.mdx` files you were assigned, at exactly the paths you were given.
Create nothing else — no README, no index you were not asked for, no edits to
`docs.json` (the orchestrator owns it).

When you finish, report back: the files you wrote, any fact you could not verify
and therefore left out, any place the code contradicted this brief or contradicted
`backend/services/copilot/skill.md`, and any page you think is missing from the
sitemap.
