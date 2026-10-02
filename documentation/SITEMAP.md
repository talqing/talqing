# Talqing docs — sitemap

The canonical path of every page, and the one thing it is for. Link by these
paths (root-relative, no extension). If a subject is listed against another
page, link there instead of covering it again — one fact has one home.

File on disk = `documentation/<path>.mdx`.

## Tab: Documentation

### Get started
| Path | Covers |
| --- | --- |
| `/get-started/introduction` | What Talqing is, the three channels, BYOK, what you can build, where to go next |
| `/get-started/core-concepts` | agent, tool, agent task, conversation / call / session, contact, workspace, region, draft vs published |
| `/get-started/quickstart-voice-agent` | Dashboard: build a voice agent and test it in the browser |
| `/get-started/quickstart-phone-number` | Dashboard: carrier account → number → assign → take a real call |
| `/get-started/quickstart-text-agent` | API/SDK: create, publish and talk to a text agent in one script |
| `/get-started/quickstart-coding-agent` | Point Claude Code / Codex at the MCP server and build from the terminal |
| `/get-started/provider-keys` | BYOK: which providers, adding a key, what fails without one |
| `/get-started/authentication` | Personal access tokens, the Bearer header, dashboard sessions, revocation |
| `/get-started/regions` | Regional base URLs, what is regional vs global, control plane vs regional API |
| `/get-started/pricing-and-credits` | Platform fee per channel, credits, packs, checkout, ledger, zero balance |
| `/get-started/workspaces-and-roles` | Workspaces, invites, members, the ADMIN/EDITOR/VIEWER matrix |

### Agents
| Path | Covers |
| --- | --- |
| `/agents/overview` | Anatomy of an agent config, every top-level field with a one-line meaning, how an update merges |
| `/agents/channels` | voice vs video vs text, what each channel has and normalizes away |
| `/agents/prompting` | Writing the system prompt; voice-specific prompt craft; tool-use instructions |
| `/agents/models` | The catalog as the source of truth; cascade vs realtime; picking llm/stt/tts; the full model table |
| `/agents/realtime` | Speech-to-speech models: what the pipeline gives up, scripted speech, the hooks and settings it refuses |
| `/agents/model-fallbacks` | `fallback` on stt/llm/tts, the rules, what a failover costs and bills |
| `/agents/reasoning-and-builtin-tools` | `reasoning_effort` on a live call; provider builtin tools (web search etc.) and their cost |
| `/agents/priority-lane` | `llm.priority`: what the premium lane buys, what it multiplies the bill by, and where it is refused |
| `/agents/voices` | Choosing a voice, speed, the ElevenLabs voice-settings step, adding a library voice |
| `/agents/expressive-delivery` | `tts.expressive`, the tag dialects, the fallback and handoff constraints |
| `/agents/speech-to-text-and-turn-detection` | Streaming vs batch STT, what ends a turn, endpointing windows, the fourteen languages |
| `/agents/language` | `config.language`, Auto, per-model language behaviour, `language_required` |
| `/agents/variables` | `vars` declarations vs supplied values, `{{system_vars.*}}`, `timezone`, templating rules and errors |
| `/agents/userdata` | `{{userdata.*}}`, seeding it per session, tools writing it, the contact record |
| `/agents/conversation-memory` | `conversation.context` across calls, `summary_limit`, `initialize_userdata`, text always transcript |
| `/agents/turn-handling` | Interruptions, `resume_false_interruption`, preemptive generation, endpointing knobs |
| `/agents/silence-and-call-length` | `silence` check-ins and hang-up, `max_duration_seconds`, the 3-hour cap on every call |
| `/agents/audio-processing` | Noise cancellation and background audio |
| `/agents/vision-and-images` | Images people send; `vision_input.screenshare`; the `vision` catalog flag |
| `/agents/keypad` | `keypad_input` and the `send_dtmf` operation; out-of-band only; which channels can do which |
| `/agents/voicemail` | `voicemail_detection`: the model-called `voicemail_detected` tool on outbound calls, the message, the `voicemail` close reason, batch retries |
| `/agents/video-avatars` | `channel: video`, Anam avatars, picking a face, what avatar time costs |
| `/agents/lifecycle-hooks` | `on_enter`, `on_exit`, `on_user_turn_completed` |
| `/agents/faqs` | FAQs: written question/answer pairs, attaching by id or inline, `get_faq_answers`, live edits, limits |
| `/agents/versions` | Draft vs published, publishing, version history, diff, rollback, tool pinning |
| `/agents/validation` | `validate`, errors vs warnings, the common refusals and how to fix them |
| `/agents/per-call-configuration` | `agent_version`, `agent_override`, inline `agent`, and when to reach for each |
| `/agents/copilot` | AgentCoPilot in the agent editor: what it can and cannot do |

### Tools
| Path | Covers |
| --- | --- |
| `/tools/overview` | What a tool is, the tool-level flags (`long_running_task`, `silent`, `disable_interruptions`), one-capability-per-tool |
| `/tools/schema-and-description` | `name`, `description`, `json_schema`; writing descriptions the model can act on |
| `/tools/operation-tree` | The ordered list, the eleven kinds, `on_error`, terminal operations, branches |
| `/tools/templating-and-data` | The six template roots, `tooldata` vs `userdata`, `publish_fields`, `set_variable`, the error/warning rules |
| `/tools/http` | The `http` operation end to end |
| `/tools/code` | The `code` operation: the TypeScript sandbox, `input`, no template syntax, `fetch` |
| `/tools/conditionals` | The `if` operation, comparisons, why time comparisons fail |
| `/tools/speech-operations` | `say`, `generate_reply`, `add_message`, `wait_for_playback`, derived silence |
| `/tools/end-call` | The `end_call` operation and how to word the goodbye |
| `/tools/transfer` | Transfer to a human: cold vs warm, hold music, decline, `on_failure`, one tool per destination |
| `/tools/handoff-operation` | The `handoff` operation for conditional routing; how it differs from `handoffs` on the agent |
| `/tools/frontend-rpc` | `frontend_rpc`: calling back into the customer's web page |
| `/tools/testing-and-publishing` | Test runs, validate, publish, versions, rollback, and the pin-at-agent-publish rule |
| `/tools/failures` | What the caller hears when a tool fails, `on_error` in practice, and the detached-failure trap |
| `/tools/copilot` | ToolCoPilot in the tool editor |
| `/tools/patterns` | Recipes: lookup-then-answer, book-then-confirm, escalation, guard-and-continue |

### Agent teams
| Path | Covers |
| --- | --- |
| `/teams/overview` | Handoffs vs teams vs the handoff operation; when to split one agent into several |
| `/teams/handoffs` | The `handoffs` field, `description` as the router, targets and publishing |
| `/teams/context-policies` | `transcript` / `summary` / `none`, `recent_turns`, `summary_prompt`, and the "summary is not privacy" rule |
| `/teams/agent-teams-on-a-call` | `agent_team` on a request, `members[0]` answers, inline definitions |
| `/teams/conditional-handoffs` | Routing decided by a tool tree rather than by the model |
| `/teams/patterns` | Triage → specialists, returning to the front desk, expressive/voice constraints across a team |

### Agent tasks
| Path | Covers |
| --- | --- |
| `/tasks/overview` | What an agent task is, how it differs from an agent, when to use one |
| `/tasks/inputs-and-output` | `vars` as inputs, `output` fields, the generated `submit_result` tool, required-and-nullable |
| `/tasks/prompting` | Writing a task prompt that finishes: carrying the work, and letting the ending be generated |
| `/tasks/tools-and-mcps` | Attaching tools and MCP servers; tasks never pin a tool version |
| `/tasks/running-and-runs` | `POST /runs`, reading a run, the trace, `attempts`, `steps_used`, cost |
| `/tasks/errors-and-limits` | The `error.type` vocabulary, `max_steps`, `timeout_seconds`, no versions |
| `/tasks/copilot` | TaskCoPilot |

### Telephony
| Path | Covers |
| --- | --- |
| `/telephony/overview` | The three things that must line up; how a call reaches an agent |
| `/telephony/carrier-accounts` | Supported carriers, credentials, provisioning the trunk, `setup_steps` you must do in the carrier console |
| `/telephony/phone-numbers` | Import, provision, assign/unassign, per-number settings, the `readiness` vocabulary |
| `/telephony/inbound-calls` | Answering calls, what an inbound session knows, inbound-specific constraints |
| `/telephony/outbound-calls` | Placing one call, `userdata`/`vars`, caller ID, what comes back |
| `/telephony/batch-calling` | Campaigns: policy, recipients, scheduling windows, concurrency, retries, pause/resume/cancel, watching |
| `/telephony/transfers` | Transfer from the operator's point of view; links to `/tools/transfer` for building it |
| `/telephony/recording` | Recording defaults, where files go, retention, screen recordings, deletion |
| `/telephony/troubleshooting` | Number not live, no inbound audio, calls failing at setup, credential expiry |

### Web & chat
| Path | Covers |
| --- | --- |
| `/channels/web-calls` | Web voice/video calls: the call token, the browser SDK, a minimal React integration |
| `/channels/web-call-features` | Images, screen share, userdata, frontend actions, the avatar track, audio defaults |
| `/channels/text-conversations` | Text agents over the API: `contact_key`, messages, the event stream, items, window lifecycle |
| `/channels/media-streams` | A partner's contact centre streaming call audio over a WebSocket: the five platforms, the URL, metadata as `vars`, what a stream agent cannot do |
| `/channels/telegram` | Deploying a text agent to Telegram through a trigger |
| `/channels/whatsapp` | A text agent on a Twilio or Gupshup WhatsApp number: connecting, the trigger, what the agent knows |
| `/channels/whatsapp-calls` | A voice agent answering calls to a Twilio WhatsApp number: prerequisites, one thread per person, no transfer, cost |

### Integrations
| Path | Covers |
| --- | --- |
| `/integrations/overview` | MCP integrations vs channel integrations; where each is attached |
| `/integrations/catalog` | Every provider we support, what each needs, which are OAuth-only |
| `/integrations/connecting` | Connecting from the dashboard vs the API; credentials and secrets |
| `/integrations/mcp-servers` | Attaching to an agent, `allowed_tools` approval, namespacing, `exposed_name` |
| `/integrations/custom-mcp` | Pointing at your own MCP server |
| `/integrations/triggers` | Binding an inbound event to a text agent |
| `/integrations/google-calendar` | The native Google Calendar tools |

### Email outbound
| Path | Covers |
| --- | --- |
| `/email/overview` | What an email batch is, the task-drafts-each-row model, and the acceptable-use rules |
| `/email/setup` | Connecting Resend, verified domains, senders |
| `/email/creating-a-batch` | The CSV, the merged column space, `field_map`, what is refused at create |
| `/email/reviewing-and-sending` | Reviewing drafts, editing cells, the 50-row send cap, skip/restore/retry, dedup |
| `/email/monitoring` | Batch statuses, counts, `next_draft_at`, self-stopping, cost |

### WhatsApp outbound
| Path | Covers |
| --- | --- |
| `/whatsapp/setup` | What a batch needs: a connected number, an approved template, opted-in recipients |
| `/whatsapp/creating-a-batch` | The CSV, the sender and template, mapping columns onto variables |
| `/whatsapp/sending-and-monitoring` | Sending now or scheduled, pacing, row statuses through delivered, read and replied |

### Observability
| Path | Covers |
| --- | --- |
| `/observability/overview` | The observability dashboard: what is charted and over what window |
| `/observability/calls` | The call record and call detail: transcript, tool calls, latency stages, cost |
| `/observability/conversations` | Conversations, sessions and items; finding a person's history |
| `/observability/call-analysis` | Post-call summary, outcome and structured fields; preview and backfill |
| `/observability/recordings` | Playing, downloading and deleting recordings |
| `/observability/webhooks` | Every event type, the signature scheme, retries, deliveries, testing |
| `/observability/costs` | What a run costs, provider spend vs platform fee, reading the ledger |
| `/observability/close-reasons` | Reference: every reason a call can end and what to do about it |
| `/observability/debugging-agents` | A playbook: the agent said something odd / was slow / hung up / never answered |

### Guides
| Path | Covers |
| --- | --- |
| `/guides/inbound-support-line` | End to end: a prompt + tools + a number answering real calls |
| `/guides/outbound-lead-qualification` | End to end: an outbound agent, a CSV, a scheduled campaign, reading results |
| `/guides/appointment-booking` | End to end: Google Calendar, timezone handling, confirmation, transfer on failure |
| `/guides/website-chat` | End to end: a text agent in your own React app with the browser SDK |
| `/guides/multi-agent-triage` | End to end: a front desk that routes to three specialists |
| `/guides/research-and-outreach` | End to end: an agent task that researches a lead, feeding an email batch |
| `/guides/migrating-from-vapi` | Concept-by-concept mapping for a team moving an existing voice agent onto Talqing |

### Platform
| Path | Covers |
| --- | --- |
| `/platform/secrets` | Workspace secrets, naming, where `{{secrets.NAME}}` resolves, write-only |
| `/platform/data-retention-and-privacy` | What we store, for how long, what you can turn off, deletion |
| `/platform/limits` | Every real limit: sizes, counts, timeouts, batch caps |
| `/platform/glossary` | One-line definition of every term in the docs |

## Tab: API Reference
| Path | Covers |
| --- | --- |
| `/api-reference/introduction` | Base URLs, `/v1`, request/response conventions, the regional model |
| `/api-reference/authentication` | Bearer tokens, cookie sessions, roles and 403s |
| `/api-reference/errors` | **Canonical.** The error envelope, every status code, retrying, and how each SDK raises |
| `/api-reference/pagination` | The `Page<T>` shape, the limit/offset convention, and why there is no cursor or total |
| `/api-reference/streaming` | The SSE endpoints and every frame they emit |
| `/api-reference/webhook-events` | Payload reference for every event type |
| `/api-reference/schemas` | `GET /v1/schemas/{name}` and the five deferred schemas |
| Endpoints | Auto-generated by Mintlify from `documentation/openapi.json` |

## Tab: SDKs
| Path | Covers |
| --- | --- |
| `/sdks/overview` | Which SDK to use, install, configure, the shared conventions |
| `/sdks/typescript` | `@talqing/sdk`: client options, resources, errors, pagination, streams |
| `/sdks/react` | `@talqing/react` and `@talqing/client`: every hook and helper, with signatures |
| `/sdks/python` | `talqing`: client, resources, errors, pagination, streams, typing |

## Tab: MCP
| Path | Covers |
| --- | --- |
| `/mcp/overview` | What the MCP server is and why you would point a coding agent at it |
| `/mcp/setup` | Install and configure for Claude Code, Codex, Cursor, Grok and generic clients |
| `/mcp/tools` | What is exposed, how arguments map to HTTP, deferred schemas |
| `/mcp/permissions` | Role scoping, read/write annotations, what is deliberately out of reach |
| `/mcp/recipes` | Prompts that build a working agent end to end from the terminal |
