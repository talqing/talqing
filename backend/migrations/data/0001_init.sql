-- Data plane. Multiple tenants may share one data DB; tenant isolation is by
-- tenant_id on every table plus tenant_id filters in every query.

-- ── agents / published versions ────────────────────────────────────────────
-- name/channel are first-class for list/filter uniqueness; full AgentConfig
-- remains in config JSONB (including copies of name + channel).
CREATE TABLE IF NOT EXISTS agents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL CHECK (btrim(name) <> ''),
    channel           TEXT NOT NULL DEFAULT 'voice'
        CHECK (channel IN ('voice', 'video', 'text')),
    config            JSONB NOT NULL DEFAULT '{}',    -- live draft AgentConfig
    published_version INTEGER,                         -- null = draft-only
    -- control.users.id. Attribution only: nothing authorizes off it and nothing
    -- filters by it. Null where a resource was not created by a signed-in user.
    created_by        UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id         UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_tenant_name
    ON agents (tenant_id, lower(name));

-- The frozen config is the whole definition: its `tools` (and lifecycle hooks)
-- carry {tool_id, version}, so the tool definitions stay in tool_versions rather
-- than being copied in beside it.
CREATE TABLE IF NOT EXISTS agent_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    config       JSONB NOT NULL,                       -- frozen AgentConfig
    published_by UUID NOT NULL,                         -- control.users.id
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    UUID NOT NULL,
    UNIQUE (agent_id, version)
);
CREATE INDEX IF NOT EXISTS idx_agent_versions_tenant_agent_version
    ON agent_versions(tenant_id, agent_id, version);

-- ── agent tasks ───────────────────────────────────────────────────────────
-- An agent nobody talks to: prompt + model + tools + MCP, declared
-- `{{vars.*}}` inputs and a typed structured output, minus every conversational
-- organ (STT, TTS, greeting, turn-taking, handoffs). Its own table rather than
-- a fourth `agents.channel`: a different noun with a different lifecycle, and a
-- `GET /v1/agents` that quietly returned things nobody can call on the phone is
-- an API we would have to explain.
--
-- Same lifecycle as an agent: `config` is the DRAFT, saved freely and validated
-- only for the things a draft can be judged on, and publishing freezes it as a
-- row in `agent_task_versions` that runs and email batches then read. The
-- checks a half-written task cannot pass — a BYOK key for its model, at least
-- one output field — land at publish, on a config its author has finished.
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL CHECK (btrim(name) <> ''),
    config            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- live draft TaskConfig
    published_version INTEGER,                              -- null = draft-only
    created_by        UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id         UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_tenant ON agent_tasks(tenant_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_tasks_tenant_name
    ON agent_tasks (tenant_id, lower(name));

-- The frozen config is the whole definition: its `tools` carry
-- {tool_id, version}, so the tool definitions stay in tool_versions rather than
-- being copied in beside it. `agent_versions` for a task, field for field.
CREATE TABLE IF NOT EXISTS agent_task_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    config       JSONB NOT NULL,                        -- frozen TaskConfig
    published_by UUID NOT NULL,                          -- control.users.id
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    UUID NOT NULL,
    UNIQUE (task_id, version)
);
CREATE INDEX IF NOT EXISTS idx_agent_task_versions_tenant_task_version
    ON agent_task_versions(tenant_id, task_id, version);

-- One execution. NOT a `sessions` row: no counterparty, no conversation, no
-- recording, no duration to bill. Usage and price live here, priced by the same
-- catalog code — which is why task spend is not in the Observability charts.
CREATE TABLE IF NOT EXISTS task_runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- ON DELETE SET NULL, matching sessions.agent_id: a run from last month must
    -- never be the reason `delete_task` starts failing.
    task_id          UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
    -- Snapshot, so a run stays readable after its task is renamed or deleted.
    task_name        TEXT,
    -- Which definition ran, the task twin of `sessions.agent_version_id`. Null
    -- for a run of an unpublished draft (`run_task` with version: "draft"),
    -- which is what makes such a run identifiable in history.
    --
    -- An INTEGER rather than a FK to `agent_task_versions.id`, unlike sessions:
    -- `task_id` is already ON DELETE SET NULL so a run outlives its task, and a
    -- SET NULL on a second column would leave a run claiming a task it can no
    -- longer name. The number is the fact worth keeping — it is what lets an
    -- email batch's review table say which definition drafted a row.
    task_version     INTEGER,
    status           TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    -- {type, message}. `type` is one of eight — missing_vars, no_output,
    -- step_limit, timeout, provider_error, configuration, platform, canceled —
    -- a closed vocabulary, because "whose problem is this" is the question the
    -- editor answers.
    error            JSONB,
    -- What the caller supplied, keyed by var name. `sessions.vars` is the same
    -- fact under the same name for a call, which is the point.
    vars             JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- The validated `submit_result` payload. Null unless status='completed'.
    output           JSONB,
    -- [{step, kind, name, args, result, ms}] — secrets redacted, fields
    -- truncated, and the truncation reported so a short trace is never mistaken
    -- for a complete one.
    trace            JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- [{provider, model, input_tokens, input_cached_tokens, output_tokens,
    --   priority}] — llm_usage's columns, by value.
    usage            JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- What this run actually did, against the cap it ran under. Stored rather
    -- than recomputed from the config, because the config may have moved since:
    -- "used 25 of 25" is only interpretable next to the 25 that was in force at
    -- the time.
    --
    -- `steps_used` is the BUSIEST ATTEMPT's round count, not the total. LiveKit
    -- re-prompts a run that ends without its output (twice, by default) and each
    -- re-prompt starts a fresh speech handle with a fresh step budget — so the
    -- number the cap governs is per attempt, and `attempts` is what says how
    -- many there were. A run with attempts > 1 hit the cap and recovered.
    steps_used       INTEGER NOT NULL DEFAULT 0,
    max_steps        INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    provider_cost    NUMERIC(12, 6),
    -- Same vocabulary as sessions.billing_status, plus 'skipped' for a run that
    -- spent nothing to price (a missing_vars abort never reaches a provider).
    -- 'unpriceable' quarantines a run whose provider is missing from the catalog
    -- rather than billing $0 — the rule sessions already follow.
    billing_status   TEXT NOT NULL DEFAULT 'pending'
        CHECK (billing_status IN ('pending', 'computed', 'unpriceable', 'skipped')),
    pricing_snapshot JSONB,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    duration_ms      INTEGER,
    created_by       UUID,
    tenant_id        UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_runs_tenant_task
    ON task_runs(tenant_id, task_id, started_at DESC);
-- Runs in flight right now, for the dead-run sweep a drafting pass begins with.
-- Tiny by construction: a completed run leaves the index.
CREATE INDEX IF NOT EXISTS idx_task_runs_running
    ON task_runs(started_at) WHERE status = 'running';

-- ── persistent conversations ────────────────────────────────────────────
-- One bounded stretch of talking — for a phone call, the call itself. The
-- external identity it belongs to (this phone number, this Telegram chat, this
-- web key) lives on conversation_refs and outlives every conversation on it:
--
--   conversation_refs 1 ──< N conversations 1 ──< N sessions
--    identity + what we        one thread          one run
--    know about them
--
-- Invariants:
--   1. One conversation_refs row per (tenant, external key). Never deleted by a
--      call — that is what "linked to the phone number" means.
--   2. Every conversation belongs to exactly one identity (NOT NULL below). A
--      conversation with no identity would be unreachable history.
--   3. There is no "current conversation" pointer. The newest conversation on
--      an identity IS the current one; a stored pointer would be a second
--      source of truth able to disagree with the rows.
--
-- What the agent learned about the person lives on conversation_refs.userdata,
-- not here: the last run overwrites it whichever conversation it ran in, so
-- pinning it to one of them is unanswerable. No agent_id here either: the web
-- text entry agent is conversation_refs.bind_id when kind=web; channel/SIP
-- agents come from trigger/dispatch; voice/video agents come from the
-- API/session.
CREATE TABLE IF NOT EXISTS conversations (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    summary               TEXT,
    -- FK added after conversation_refs exists (see below).
    conversation_ref_id   UUID NOT NULL,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- A conversation WE opened (a WhatsApp template, an outbound call) is
    -- `inactive` until the contact takes part: replies, calls, or answers our
    -- call. Then it is `active`, and it never goes back. One the contact opened
    -- is `active` from the start.
    status                TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    -- When it last had a message, so the inbox orders by a column instead of
    -- looking up every conversation's newest item on each page load. Written
    -- beside every insert of a `message` item, in the same transaction.
    last_activity_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id             UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_updated
    ON conversations(tenant_id, updated_at DESC);
-- The inbox: a workspace's active conversations, without walking the outreach.
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_active
    ON conversations(tenant_id, last_activity_at DESC) WHERE status = 'active';
-- "the newest conversation on this identity" is the hottest query in the system:
-- every call that is not `context = none` asks it before the greeting.
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_ref_created
    ON conversations(tenant_id, conversation_ref_id, created_at DESC);

-- ── sessions / usage (unified agent runs across modalities) ─────────────
-- One session = one agent invocation window:
--   voice/video call, or a multi-turn text conversation window (warm until idle).
-- conversation_items is the sole durable timeline/chat-context source.
CREATE TABLE IF NOT EXISTS sessions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  UUID REFERENCES conversations(id) ON DELETE SET NULL,
    agent_id         UUID REFERENCES agents(id) ON DELETE SET NULL,
    agent_version_id UUID REFERENCES agent_versions(id) ON DELETE SET NULL,
    agent_name       TEXT,
    channel          TEXT NOT NULL DEFAULT 'voice'
        CHECK (channel IN ('voice', 'video', 'text')),
    type             TEXT NOT NULL DEFAULT 'WEB'
        CHECK (type IN ('WEB', 'TEXT', 'SIP_INBOUND', 'SIP_OUTBOUND', 'STREAM', 'WHATSAPP_INBOUND')),
        -- WHATSAPP_INBOUND is a call a user placed from their chat with a
        -- WhatsApp sender. The leg reaches us over SIP, but the caller is a
        -- WhatsApp user on an integration, not a phone number on a carrier trunk.
        -- STREAM is one socket from a partner's contact-centre platform
        -- (`stream_connections`). One type, not an inbound/outbound pair: the
        -- partner owns the dialling, so direction is something we are told or
        -- do not know, never something we did.
        -- Customer agent executions only. The CoPilots / KB Builder never write
        -- sessions (unbilled; warm window is in-process only).
        -- Runtime path is derived from type + channel (not stored).
    status           TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'canceled')),
    close_reason     TEXT,
    -- FKs added after integration_triggers / conversation_refs / phone_numbers
    -- exist (see below).
    trigger_id       UUID,
    integration_id   UUID,
    conversation_ref_id UUID,
    -- Telephony (SIP). FK targets created later with phone_numbers.
    phone_number_id      UUID,
    telephony_account_id UUID,
    -- WebSocket media streams. Which partner connection this call arrived on;
    -- NULL on every other type, exactly as the two above are NULL on a stream.
    -- FK target created later with stream_connections.
    stream_connection_id UUID,
    livekit_room         TEXT,
    from_e164            TEXT,
    to_e164              TEXT,
    -- Which outbound batch placed this call, if any. FK target created later
    -- with call_batches. Written once at claim time, never changed — carried so
    -- `GET /v1/calls?batch_id=` is an indexed filter rather than a join against
    -- up to 10 000 recipient ids, and so superseded retries stay findable.
    batch_id             UUID,
    -- What this call was asked to run when it differed from "the entry agent's
    -- published version" — an override, a whole inline agent, or a roster of
    -- them. NULL on an ordinary single-agent call. The PLAN is stored, not the
    -- resolved configs: the base is immutable (`agent_versions.config` is never
    -- rewritten), so re-merging in the worker cannot drift, and the diff a human
    -- wants — "what was different about this call" — IS the override.
    agent_plan           JSONB,
    -- The `{{vars.*}}` values the request that started this session supplied.
    -- Flat, string-to-string: they are substituted textually into a prompt, a
    -- greeting and a tool's URL/headers/body, so a number here would only ever
    -- be read back out as its own text.
    --
    -- NULL on an inbound call, which has no request of ours — its
    -- agents' declared defaults stand alone. Reaches every agent on the
    -- session, merged over each one's own defaults.
    vars                 JSONB,
    -- When the worker finished an ended chat: exit hook, analysis, bill, webhook.
    -- A chat can be asked to end twice at once — its agent calls `end_call` while
    -- the API ends it — and this is the claim that makes the second one a no-op.
    finalized_at         TIMESTAMPTZ,
    -- usage: llm_usage / tts_usage / stt_usage / avatar_usage tables
    -- cost: provider_cost / platform_fee / total_charge + pricing_snapshot
    -- timeline: conversation_items.session_id (and trigger_item_id)
    -- metrics: seal-time bag {usage_reported, e2e_latency ms averages, turns, …};
    --   turn-level LiveKit MetricsReport samples stay on conversation_items.
    metrics          JSONB NOT NULL DEFAULT '{}'::jsonb,
    error            JSONB,
    userdata         JSONB,
    duration_s       INTEGER,
    -- Call recording. One stereo Ogg/Opus object per voice/video session,
    -- left = caller, right = agent. Every status here is a fact somebody wrote,
    -- never arithmetic: nothing ages a row against a window and no bucket
    -- lifecycle rule deletes objects behind Postgres's back, so the two cannot
    -- disagree. 'deleted' is a human through the API, 'expired' is a retention
    -- purge — same outcome, different author, and a tenant needs to be
    -- able to tell them apart.
    recording_status TEXT NOT NULL DEFAULT 'none'
        CHECK (recording_status IN (
            'none', 'pending', 'stored', 'failed', 'consent_withdrawn', 'deleted', 'expired'
        )),
    recording_object_key TEXT,
    -- The deletion receipt: when this call's content was redacted by a
    -- `session.purge` job. The row survives it — the metering an invoice
    -- is built from lives here.
    content_deleted_at TIMESTAMPTZ,
    -- Post-call analysis. One LLM call at the end of a voice/video call
    -- fills these in. Each fact lives here exactly once: `outcome` is never
    -- also in analysis_fields, and analysis_fields holds only tenant-defined
    -- extractions. No 'running' status — analysis is one bounded call inside
    -- finalize, so a worker that dies mid-run leaves 'pending', which already
    -- reads as "never finished".
    analysis_status TEXT NOT NULL DEFAULT 'none'
        CHECK (analysis_status IN ('none', 'pending', 'completed', 'failed', 'skipped')),
    -- Which gate refused the call: too_short / call_failed / produces_nothing.
    -- "we chose not to" and "it ran and found nothing" are different answers,
    -- and only one of them is a configuration problem the reader can fix.
    analysis_skip_reason TEXT,
    summary              TEXT,
    outcome              TEXT CHECK (outcome IN ('success', 'failure', 'unknown')),
    outcome_rationale    TEXT,
    -- {field_name: value}. Deliberately NOT merged into `userdata`: userdata is
    -- what the agent knew (tools wrote it during the call), this is what a
    -- reader inferred afterwards, and merging would let a guess overwrite a
    -- tool-confirmed value with nothing downstream able to tell them apart.
    analysis_fields      JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Call transfer. Set when the agent handed this call to a human:
    -- {mode, transport, destination, outcome, sip_status, at}. `sip_status` is
    -- the RAW carrier verdict ({"code": 486, "phrase": "Busy Here"}), not the
    -- plain-English wording the caller heard — the operator debugging a trunk
    -- needs the code, and the caller needed the sentence. A failed attempt is
    -- recorded too. This is also what explains a transferred call's short
    -- `duration_s` and truncated recording: both measure OUR part of the call.
    transfer             JSONB,
    -- Wall clock at the first recorded frame, from RecorderIO. Also the origin
    -- for transcript-synced seeking: a conversation_item's offset into the file
    -- is created_at - recording_started_at.
    recording_started_at TIMESTAMPTZ,
    recording_duration_s INTEGER,
    recording_bytes      BIGINT,
    -- The screen share, when the agent watched one and the author asked for it
    -- to be kept. A second recording of the same call: 1 fps H.264, its own
    -- object, its own status — rather than a video track muxed into the audio
    -- file, which means exactly one thing (stereo Ogg, caller left, agent right)
    -- to the player, the purge, the duration probe and the size cap alike.
    -- 'not_shared' is the normal outcome of a call where the screen never came
    -- up and is deliberately not 'none', which means the author had screen
    -- recording turned off. There is no 'consent_withdrawn' here:
    -- `stop_recording` governs the audio disclosure, and screen recording has no
    -- consent flow of its own yet.
    screenshare_recording_status TEXT NOT NULL DEFAULT 'none'
        CHECK (screenshare_recording_status IN (
            'none', 'pending', 'stored', 'not_shared', 'failed', 'deleted', 'expired'
        )),
    screenshare_recording_object_key TEXT,
    -- Wall clock at the writer's first tick, which begins with the call rather
    -- than with the share. The video is therefore exactly call-length, and the
    -- call detail lines it up against the audio and the transcript from here.
    screenshare_recording_started_at TIMESTAMPTZ,
    screenshare_recording_duration_s INTEGER,
    screenshare_recording_bytes      BIGINT,
    provider_cost    NUMERIC(12,6),
    platform_fee     NUMERIC(12,6),
    total_charge     NUMERIC(12,6),
    pricing_snapshot JSONB,
    billing_status   TEXT NOT NULL DEFAULT 'pending'
        CHECK (billing_status IN ('pending', 'computed', 'unpriceable')),
    billed_at        TIMESTAMPTZ,
    idempotency_key  TEXT,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id        UUID NOT NULL
);
-- Observability / call list windows filter on COALESCE(started_at, created_at).
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_window
    ON sessions(tenant_id, (COALESCE(started_at, created_at)) DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_agent_window
    ON sessions(tenant_id, agent_id, (COALESCE(started_at, created_at)) DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_conversation
    ON sessions(tenant_id, conversation_id, (COALESCE(started_at, created_at)) DESC);
-- A text chat is one session, from its first message to its explicit end — what
-- a call is for voice. A conversation holds at most one open chat, and a contact
-- at most one open chat per entry agent. A chat started from an inline agent has
-- no agent row, so every such chat on a contact shares the zero UUID's slot.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_open_chat
    ON sessions (tenant_id, conversation_id)
    WHERE channel = 'text' AND status IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_open_chat_per_agent
    ON sessions (
        tenant_id, conversation_ref_id,
        (COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid))
    )
    WHERE channel = 'text' AND status IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_idempotency
    ON sessions(tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- The calls list filters on outcome inside a time window and charts a success
-- rate from it. Partial: a call with no outcome is never what that query wants.
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_outcome
    ON sessions(tenant_id, outcome, (COALESCE(started_at, created_at)) DESC)
    WHERE outcome IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_analysis_fields
    ON sessions USING GIN (analysis_fields);

-- Runtime trace for one session: what the platform did that the transcript
-- cannot show — provider failures and fallbacks, tool timing, connection
-- quality, lifecycle. Deliberately does NOT carry turns, transcripts or
-- per-message metrics: conversation_items already owns those, and a second
-- copy would be a second truth.
--
-- `seq` is assigned by an in-process counter on the worker, not by a row lock.
-- Exactly one worker process owns a session for its whole life, so serializing
-- through the DB would buy nothing. Do not "fix" this into a SELECT ... FOR
-- UPDATE; if sessions ever gain a second writer, that is the moment to revisit.
CREATE TABLE IF NOT EXISTS session_events (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id  UUID NOT NULL
);
-- Timeline order is `seq`; the unique index is also the idempotency guard for a
-- retried batch insert.
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_events_seq
    ON session_events(tenant_id, session_id, seq);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                  BIGSERIAL PRIMARY KEY,
    session_id        UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    -- Only quantities we price (input / cached input / output). LiveKit may
    -- report modality splits; those are not billed separately and are not stored.
    input_tokens        BIGINT NOT NULL DEFAULT 0,
    input_cached_tokens BIGINT NOT NULL DEFAULT 0,
    -- Input tokens spent WRITING the cache rather than reading it. Billed at a
    -- premium where it is billed at all (1.25x input on Anthropic and from
    -- OpenAI's GPT-5.6 family onward), and reported by a provider only where it
    -- charges for it — so a non-zero count with no `cache_write_per_1m` in the
    -- catalog quarantines the session rather than pricing the writes at zero.
    input_cache_write_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens       BIGINT NOT NULL DEFAULT 0,
    -- What the provider itself charged for these requests, where it says so.
    -- OpenRouter does, per request: it picks one of ~106 upstream hosts per call
    -- and bills at that host's price, while its rate card names the cheapest
    -- endpoint's — measured 2.4x apart on two consecutive requests for the same
    -- model. Set means this IS the line cost and no rate block is consulted;
    -- NULL on every other provider, which prices from the catalog.
    reported_cost       NUMERIC(12, 6),
    -- Which LLM spend this is: the conversation, or the single call that
    -- analysed it afterwards. Both price through the same path; without
    -- this they are indistinguishable and the cost breakdown cannot tell a
    -- tenant what post-call analysis actually cost them.
    purpose             TEXT NOT NULL DEFAULT 'conversation'
        CHECK (purpose IN ('conversation', 'analysis')),
    -- Whether this model ran in the provider's priority (low-latency) lane,
    -- which prices off a second rate block on the same catalog entry.
    -- Records what the agent asked for: the tier that actually served a turn is
    -- dropped before metering, and one provider never reports it at all.
    priority            BOOLEAN NOT NULL DEFAULT FALSE,
    tenant_id           UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_session ON llm_usage(tenant_id, session_id);

CREATE TABLE IF NOT EXISTS tts_usage (
    id               BIGSERIAL PRIMARY KEY,
    session_id     UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    characters_count BIGINT NOT NULL DEFAULT 0,
    audio_duration   DOUBLE PRECISION NOT NULL DEFAULT 0,
    input_tokens     BIGINT NOT NULL DEFAULT 0,
    output_tokens    BIGINT NOT NULL DEFAULT 0,
    tenant_id        UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tts_usage_session ON tts_usage(tenant_id, session_id);

CREATE TABLE IF NOT EXISTS stt_usage (
    id             BIGSERIAL PRIMARY KEY,
    session_id   UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    audio_duration DOUBLE PRECISION NOT NULL DEFAULT 0,
    input_tokens   BIGINT NOT NULL DEFAULT 0,
    output_tokens  BIGINT NOT NULL DEFAULT 0,
    -- What the provider itself charged, where it says so — the same column
    -- `llm_usage` has. OpenRouter returns the exact charge on every
    -- transcription and picks the upstream host per request, so its rate card
    -- cannot price it. Set means this IS the line cost; NULL prices from the
    -- catalog.
    reported_cost  NUMERIC(12, 6),
    tenant_id      UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stt_usage_session ON stt_usage(tenant_id, session_id);

-- Speech-to-speech. Unlike llm_usage this DOES store the modality splits,
-- because a realtime model bills them at very different rates (audio input runs
-- 6-8x text input): collapsing them into one input_tokens column would bill the
-- audio at the text rate. `session_seconds` covers the providers that meter the
-- audio stream by the minute instead of by the token (xAI).
CREATE TABLE IF NOT EXISTS realtime_usage (
    id                        BIGSERIAL PRIMARY KEY,
    session_id                UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    provider                  TEXT NOT NULL,
    model                     TEXT NOT NULL,
    input_text_tokens         BIGINT NOT NULL DEFAULT 0,
    input_cached_text_tokens  BIGINT NOT NULL DEFAULT 0,
    input_audio_tokens        BIGINT NOT NULL DEFAULT 0,
    input_cached_audio_tokens BIGINT NOT NULL DEFAULT 0,
    output_text_tokens        BIGINT NOT NULL DEFAULT 0,
    output_audio_tokens       BIGINT NOT NULL DEFAULT 0,
    session_seconds           DOUBLE PRECISION NOT NULL DEFAULT 0,
    tenant_id                 UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_realtime_usage_session ON realtime_usage(tenant_id, session_id);

CREATE TABLE IF NOT EXISTS avatar_usage (
    id                BIGSERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    avatar_id         TEXT,
    avatar_session_id TEXT,
    seconds           DOUBLE PRECISION NOT NULL DEFAULT 0,
    tenant_id         UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_avatar_usage_session ON avatar_usage(tenant_id, session_id);

-- ── credits ────────────────────────────────────────────────────────────────
-- One organization's spendable balance, in USD, against the platform fee and
-- nothing else. Under BYOK the provider cost on a session is the tenant's own
-- spend on their own key, so it never touches this number.
--
-- Here rather than in the control plane because it is written once per call —
-- and that placement is what lets the debit share a transaction with the
-- session's money columns (services/billing/session.py), so a priced call and
-- its debit can never disagree and nothing has to reconcile them.
--
-- Materialized rather than summed from credit_ledger: this is read on the
-- call-setup path, and nothing on that path may scan a growing table.
CREATE TABLE IF NOT EXISTS credit_accounts (
    tenant_id  UUID PRIMARY KEY,
    -- Same precision as sessions.platform_fee, which is what debits it.
    -- MAY GO NEGATIVE: a call already running is never cut off, so the last
    -- call of a balance overdraws it by its own fee. The next top-up pays that
    -- off first, because the balance is a running total of a signed ledger.
    balance    NUMERIC(12,6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- APPEND-ONLY. Every movement of the balance above, with the balance it
-- produced, so a dispute is answered by reading rows rather than by recomputing
-- history against a catalog that has since changed.
--
-- Nothing ever UPDATEs or DELETEs a row here. A correction is a new
-- 'adjustment' row, which is also what makes the ledger a truthful record of
-- what we did rather than of what we currently believe.
CREATE TABLE IF NOT EXISTS credit_ledger (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL CHECK (kind IN (
                   'signup_grant',  -- the grant on an organization created at signup
                   'purchase',      -- a paid top-up
                   'usage',         -- one session's platform fee
                   'refund',        -- a refunded top-up or a lost dispute
                   'adjustment'     -- a human, with a reason
               )),
    -- Signed: positive credits, negative debits. One column rather than
    -- (direction, magnitude), so the balance is a SUM and never a CASE.
    amount     NUMERIC(12,6) NOT NULL,
    balance_after NUMERIC(12,6) NOT NULL,
    -- What caused it. Three kinds point at three different things and two point
    -- at nothing, so these are typed columns rather than a polymorphic
    -- (subject_kind, subject_id) pair — which is also what lets the unique
    -- indexes below exist.
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,  -- 'usage'
    -- A chat is debited as it goes, once per warm window and once at its end, so
    -- a session has many `usage` rows. This names which settlement wrote one;
    -- NULL is a call, which is still debited exactly once.
    segment_id UUID,
    -- The control-plane credit_topups row. No FK is possible across planes —
    -- same as call_batches.created_by_user_id.
    topup_id   UUID,          -- 'purchase' and 'refund'
    note       TEXT,          -- 'adjustment' only; required by the service
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id  UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_tenant
    ON credit_ledger(tenant_id, created_at DESC);

-- A call is debited exactly once, ever, and a chat once per settlement. This is
-- what makes the debit safe to retry, and what makes `backfill_call_analysis` —
-- which re-prices calls through the same bill_session — a no-op on money. (The
-- fee is a function of duration and channel alone, so a re-price cannot change
-- the amount either.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_session
    ON credit_ledger(tenant_id, session_id)
    WHERE session_id IS NOT NULL AND segment_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_session_segment
    ON credit_ledger(tenant_id, session_id, segment_id)
    WHERE segment_id IS NOT NULL;

-- A top-up credits once however many times payment.succeeded is delivered, and
-- reverses once however many times a refund or a lost dispute is.
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_purchase
    ON credit_ledger(tenant_id, topup_id) WHERE kind = 'purchase';
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_refund
    ON credit_ledger(tenant_id, topup_id) WHERE kind = 'refund';

-- One organization, one signup grant. The other half of the rule — an
-- organization a member created themselves is never eligible — is decided at the
-- single call site in api/control/routes/auth.py, not here; this only stops a retry
-- double-granting.
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_signup
    ON credit_ledger(tenant_id) WHERE kind = 'signup_grant';

-- ── webhooks ───────────────────────────────────────────────────────────────
-- Tenant-wide only: one subscription covers every agent. Event payloads still
-- carry agent_id so receivers can filter.
CREATE TABLE IF NOT EXISTS webhooks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url               TEXT NOT NULL,
    secret_encrypted  TEXT NOT NULL,
    subscribed_events TEXT[] NOT NULL DEFAULT '{}',  -- empty = every event type
    status            TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    last_status       TEXT,
    last_delivered_at TIMESTAMPTZ,
    created_by        UUID,                           -- control.users.id (attribution only)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id         UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhooks_tenant ON webhooks(tenant_id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          BIGSERIAL PRIMARY KEY,
    webhook_id  UUID NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    status      TEXT NOT NULL,                       -- delivered | failed
    status_code INTEGER,
    error       TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id   UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_tenant_webhook
    ON webhook_deliveries(tenant_id, webhook_id, created_at DESC);

-- ── tools / secrets ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tools (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    json_schema        JSONB NOT NULL DEFAULT '{}',
    operations         JSONB NOT NULL DEFAULT '[]'::jsonb, -- live draft operation tree
    long_running_task  BOOLEAN NOT NULL DEFAULT false,
    silent             BOOLEAN NOT NULL DEFAULT false,
    disable_interruptions BOOLEAN NOT NULL DEFAULT false,
    published_version  INTEGER,
    created_by         UUID,                          -- control.users.id (attribution only)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id          UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tools_tenant ON tools(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tools_name ON tools(tenant_id, name);

CREATE TABLE IF NOT EXISTS tool_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tool_id      UUID NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    definition   JSONB NOT NULL,
    changelog    TEXT,
    published_by UUID NOT NULL,                        -- control.users.id
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    UUID NOT NULL,
    UNIQUE (tool_id, version)
);
CREATE INDEX IF NOT EXISTS idx_tool_versions_tenant_tool
    ON tool_versions(tenant_id, tool_id);

CREATE TABLE IF NOT EXISTS integrations (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name         TEXT NOT NULL,
    provider             TEXT NOT NULL,
    -- auth_type is not stored: it is fixed per provider and derived from
    -- ProviderSpec (oauth | manual) at response / definition time.
    credentials_ref      TEXT,
    -- Identity bag (not credentials). Uniqueness keys by provider:
    -- telegram: bot_id; oauth: provider_subject.
    -- Display fields may also live here (email, bot_username).
    provider_account_info JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Capabilities are derived from provider in code (never stored).
    mcp_config           JSONB NOT NULL DEFAULT '{}'::jsonb,
    webhook_config            JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Approved MCP tool names. NULL is a real state, not a missing value: it
    -- means "nothing approved yet, so expose everything". A list is the exact
    -- set attached agents may call. Never an empty array — approving no tools is
    -- what disabling the integration means, and an empty list reads as "no
    -- filter" to the MCP client, i.e. the exact opposite of what it says.
    allowed_tools             TEXT[],
    -- The prefix every tool from this server is presented to the model under:
    -- `<tools_namespace>_<tool>`. Empty string is a real value and means
    -- "present the server's own names, unprefixed" — a deliberate escape hatch,
    -- not a missing default. Never NULL: a tri-state here would only ever be
    -- read as ''. Approval still filters on the server's own names; the prefix
    -- is applied after.
    tools_namespace           TEXT NOT NULL DEFAULT '',
    last_webhook_received_at  TIMESTAMPTZ,
    metadata                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                    TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled','needs_reconnect','error')),
    created_by           UUID,                        -- control.users.id (attribution only)
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id            UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integrations_tenant ON integrations(tenant_id);
CREATE INDEX IF NOT EXISTS idx_integrations_tenant_provider
    ON integrations(tenant_id, provider);
CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_display_name
    ON integrations(tenant_id, display_name);
-- Channel identity uniqueness (provider_account_info identity keys).
CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_telegram_bot
    ON integrations (tenant_id, ((provider_account_info ->> 'bot_id')))
    WHERE provider = 'telegram'
      AND COALESCE(provider_account_info ->> 'bot_id', '') <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_oauth_subject
    ON integrations (tenant_id, provider, ((provider_account_info ->> 'provider_subject')))
    WHERE provider IN (
            'google_calendar', 'cal_com', 'calendly', 'asana', 'jira', 'hubspot'
        )
      AND COALESCE(provider_account_info ->> 'provider_subject', '') <> '';
-- One integration per WhatsApp sender NUMBER per workspace, whichever BSP
-- carries it. A number can only live on one BSP at a time, and the conversation
-- key (`whatsapp:{sender}:{peer}`) names the number, not the BSP — two
-- integrations on one number would fight over the same threads.
CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_whatsapp_sender
    ON integrations (tenant_id, ((provider_account_info ->> 'sender_e164')))
    WHERE provider = 'whatsapp';

-- access_token / refresh_token are Fernet-encrypted (same secrets_fernet_key).
CREATE TABLE IF NOT EXISTS integration_oauth_credentials (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_id           UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    provider                 TEXT NOT NULL,
    account_email            TEXT NOT NULL,
    provider_subject         TEXT,
    scopes                   TEXT[] NOT NULL DEFAULT '{}',
    access_token             TEXT NOT NULL,
    refresh_token            TEXT NOT NULL,
    expires_at               TIMESTAMPTZ NOT NULL,
    token_type               TEXT NOT NULL DEFAULT 'Bearer',
    oauth_client_id          TEXT,
    -- Connection-time discovery artifacts for refresh/revoke (token_endpoint, resource, …).
    oauth_metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id                UUID NOT NULL,
    UNIQUE (tenant_id, integration_id)
);
CREATE INDEX IF NOT EXISTS idx_integration_oauth_credentials_tenant
    ON integration_oauth_credentials(tenant_id);
CREATE INDEX IF NOT EXISTS idx_integration_oauth_credentials_provider
    ON integration_oauth_credentials(tenant_id, provider);

CREATE TABLE IF NOT EXISTS integration_triggers (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_id            UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    trigger_type              TEXT NOT NULL,
    agent_id                  UUID REFERENCES agents(id) ON DELETE SET NULL,
    enabled                   BOOLEAN NOT NULL DEFAULT false,
    status                    TEXT NOT NULL DEFAULT 'needs_setup'
        CHECK (status IN ('active','disabled','needs_setup','error')),
    -- public_reply = customer-visible send; internal_note = provider private note
    -- when supported; none = generate only (never send).
    reply_mode                TEXT NOT NULL DEFAULT 'public_reply'
        CHECK (reply_mode IN ('public_reply', 'internal_note', 'none')),
    provider_subscription_ref TEXT,
    metadata                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id                 UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_triggers_integration
    ON integration_triggers(tenant_id, integration_id);
CREATE INDEX IF NOT EXISTS idx_integration_triggers_agent
    ON integration_triggers(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_integration_triggers_type
    ON integration_triggers(tenant_id, trigger_type);
CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_triggers_enabled_type
    ON integration_triggers(tenant_id, integration_id, trigger_type)
    WHERE enabled = true;

-- ── telephony ──────────────────────────────────────────────────────────────
-- Carrier account (Plivo / Exotel / Vobiz / Twilio).
CREATE TABLE IF NOT EXISTS telephony_accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    -- Named so later migrations can drop and re-add it as the provider set
    -- changes; an inline CHECK gets a generated name that differs by table.
    provider        TEXT NOT NULL
        CONSTRAINT telephony_accounts_provider_check
        CHECK (provider IN ('plivo', 'exotel', 'vobiz', 'twilio')),
    display_name    TEXT NOT NULL CHECK (btrim(display_name) <> ''),
    -- Non-secret account identity (auth_id, account_sid, region, …).
    account_info    JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Secret refs only: { "auth_token": "{{secrets.X}}", … }.
    credentials     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Shared provider-side trunk / URI / checklist state (not LiveKit ids).
    provider_state  JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- SIP edge: the URI this account's provider trunk points at, chosen at
    -- creation from livekit.sip.uri and stable thereafter. Stored as the URI
    -- itself rather than an index so reordering the config cannot silently move
    -- a tenant to another name. Which droplet serves a given call is decided at
    -- the edge, per call.
    sip_uri         TEXT NOT NULL CHECK (btrim(sip_uri) <> ''),
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'connected', 'provisioning', 'ready', 'error', 'disabled'
        )),
    status_message  TEXT,
    last_synced_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_telephony_accounts_tenant
    ON telephony_accounts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_telephony_accounts_tenant_provider
    ON telephony_accounts(tenant_id, provider);
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_accounts_display_name
    ON telephony_accounts(tenant_id, lower(display_name));
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_accounts_plivo_auth_id
    ON telephony_accounts (tenant_id, ((account_info ->> 'auth_id')))
    WHERE provider = 'plivo'
      AND COALESCE(account_info ->> 'auth_id', '') <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_accounts_exotel_sid
    ON telephony_accounts (tenant_id, ((account_info ->> 'account_sid')))
    WHERE provider = 'exotel'
      AND COALESCE(account_info ->> 'account_sid', '') <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_accounts_vobiz_auth_id
    ON telephony_accounts (tenant_id, ((account_info ->> 'auth_id')))
    WHERE provider = 'vobiz'
      AND COALESCE(account_info ->> 'auth_id', '') <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_accounts_twilio_sid
    ON telephony_accounts (tenant_id, ((account_info ->> 'account_sid')))
    WHERE provider = 'twilio'
      AND COALESCE(account_info ->> 'account_sid', '') <> '';

-- One E.164 DID under a telephony account. LiveKit trunk/rule ids are per-DID.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL,
    telephony_account_id  UUID NOT NULL
        REFERENCES telephony_accounts(id) ON DELETE CASCADE,
    e164                  TEXT NOT NULL
        CHECK (e164 ~ '^\+[1-9][0-9]{7,14}$'),
    provider              TEXT NOT NULL
        CONSTRAINT phone_numbers_provider_check
        CHECK (provider IN ('plivo', 'exotel', 'vobiz', 'twilio')),
    provider_number_id    TEXT,
    label                 TEXT,
    can_inbound           BOOLEAN NOT NULL DEFAULT true,
    can_outbound          BOOLEAN NOT NULL DEFAULT true,
    -- RESTRICT: the LiveKit dispatch rule is what actually routes the call, so
    -- a bare agent delete that only nulled this would leave every inbound call
    -- dispatching to a deleted agent. `delete_agent` refuses and names the
    -- number; this is the backstop for any path that does not.
    inbound_agent_id      UUID REFERENCES agents(id) ON DELETE RESTRICT,
    livekit_inbound_trunk_id   TEXT,
    livekit_dispatch_rule_id   TEXT,
    provider_state        JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'provisioning', 'active', 'error', 'disabled')),
    status_message        TEXT,
    last_synced_at        TIMESTAMPTZ,
    created_by            UUID,                        -- control.users.id (attribution only)
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One LiveKit project cannot accept the same DID on two trunks.
CREATE UNIQUE INDEX IF NOT EXISTS uq_phone_numbers_e164_live
    ON phone_numbers (e164)
    WHERE status IN ('pending', 'provisioning', 'active', 'error');
CREATE INDEX IF NOT EXISTS idx_phone_numbers_tenant
    ON phone_numbers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_account
    ON phone_numbers(tenant_id, telephony_account_id);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_agent
    ON phone_numbers(tenant_id, inbound_agent_id)
    WHERE inbound_agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_phone_numbers_e164
    ON phone_numbers(tenant_id, e164);

-- ── WebSocket media streams ────────────────────────────────────────────────
--
-- The third way a call reaches an agent, beside a browser room and a SIP trunk:
-- a partner's contact-centre platform terminates the PSTN leg and streams the
-- audio to us over one WebSocket per call. They keep the number, the IVR, the
-- queue and the CRM; we are the AI agent and nothing else.
--
-- A connection is the durable thing a partner is given — a dialect, an agent,
-- and the URL the two make — so it is the analogue of `phone_numbers`: the
-- thing that answers. One socket becomes one `sessions` row of type STREAM.
CREATE TABLE IF NOT EXISTS stream_connections (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- No credential column, and that is a decision taken with its cost stated.
    -- The URL a partner is given is `{dialect}/{tenant_id}/{agent_id}` and
    -- nothing else: two UUIDv4s, 244 bits between them, neither guessable. What
    -- they are NOT is revocable — an agent id is published by design, and the
    -- URL is derived from it, so deleting this row and creating another for the
    -- same agent and dialect hands back the identical URL. A leaked URL is closed
    -- by `status = 'disabled'` or by pointing the connection at another agent;
    -- there is no rotate. Accepted for MVP over a fifth path segment that every
    -- partner has to paste by hand. The day that is not enough, the answer is a
    -- `secret_digest` column here and a fourth segment on the URL.
    name               TEXT NOT NULL CHECK (btrim(name) <> ''),
    -- Also the dialect segment of the connection's URL, and half its identity:
    -- one agent can answer on several platforms, one connection each, and this
    -- is what tells their URLs apart.
    dialect            TEXT NOT NULL
        CONSTRAINT stream_connections_dialect_check
        CHECK (dialect IN ('twilio', 'sparktg', 'plivo', 'exotel', 'vonage')),
    -- Which agent answers, and with `dialect` the URL's identity: one URL, one
    -- agent, so the pair doubles as the lookup key and there is no second public
    -- id to mint. NOT NULL, because a connection whose agent is gone is a URL
    -- that can only ever refuse.
    --
    -- RESTRICT: deleting an agent must not silently take a live partner
    -- integration with it. `delete_agent` refuses and names the connection, and
    -- clears the disabled ones itself; this is the backstop.
    agent_id           UUID NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    -- No audio format columns, and no parameter map. Every protocol announces
    -- its own format per call and carries its own metadata; both would be
    -- configuration that could only ever disagree with the wire.
    --
    -- No `record` column either: `AgentConfig.recording` already says whether
    -- this agent records, and a second switch here would be a second answer to
    -- one question. No IP allowlist and no concurrency cap: the credit gate
    -- already refuses a workspace with nothing left, and the gateway's own
    -- duration cap bounds each call.
    status             TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    created_by         UUID,                        -- control.users.id (attribution only)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id          UUID NOT NULL
);
-- The gateway's hot path: one indexed read per socket, before anything else.
-- Unique per tenant, which is also the "one connection per agent per platform"
-- rule expressed in the schema — and per tenant rather than global because these
-- rows live in the tenant's own database (`postgres.data.tenant_overrides`).
CREATE UNIQUE INDEX IF NOT EXISTS uq_stream_connections_agent_dialect
    ON stream_connections(tenant_id, agent_id, dialect);
CREATE INDEX IF NOT EXISTS idx_stream_connections_tenant
    ON stream_connections(tenant_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_stream_connections_name
    ON stream_connections(tenant_id, lower(name));

-- The durable external identity behind N conversations: a phone number, a
-- Telegram chat, a tenant's own web key, or a CoPilot subject. Also the home of
-- `userdata` — what the agent has learned about this person, replaced by every
-- run at finalize and read at the start of a call only when the agent asks for
-- it (AgentConfig.conversation.initialize_userdata).
--
-- kind classifies the binding; bind_id is the kind-specific resource id
-- (no FK — deleting integration/phone/agent must not wipe history):
--   integration  → integrations.id
--   sip          → phone_numbers.id
--   stream       → stream_connections.id (a partner's WebSocket media stream)
--   web          → agents.id (text entry agent; null for voice-only web)
--   agent_copilot→ agents.id (subject agent being edited)
--   tool_copilot → tools.id (subject tool being edited)
-- conversation_key is opaque and unique per tenant (platform prefixes for
-- channels/copilots; tenant-owned strings for web). metadata is
-- end-customer only for channel/sip/web.
CREATE TABLE IF NOT EXISTS conversation_refs (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_key          TEXT NOT NULL,
    kind                      TEXT NOT NULL
        CONSTRAINT conversation_refs_kind_check CHECK (kind IN (
            'integration', 'sip', 'stream', 'web',
            'agent_copilot', 'tool_copilot', 'task_copilot'
        )),
    bind_id                   UUID,
    userdata                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id                 UUID NOT NULL,
    UNIQUE (tenant_id, conversation_key),
    CONSTRAINT conversation_refs_bind_id_check CHECK (
        kind = 'web'
        OR (
            kind IN (
                'integration', 'sip', 'stream',
                'agent_copilot', 'tool_copilot', 'task_copilot'
            )
            AND bind_id IS NOT NULL
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_conversation_refs_kind_bind
    ON conversation_refs(tenant_id, kind, bind_id)
    WHERE bind_id IS NOT NULL;
-- One chat per CoPilot subject.
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_refs_agent_copilot_bind
    ON conversation_refs(tenant_id, bind_id)
    WHERE kind = 'agent_copilot';
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_refs_tool_copilot_bind
    ON conversation_refs(tenant_id, bind_id)
    WHERE kind = 'tool_copilot';
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_refs_task_copilot_bind
    ON conversation_refs(tenant_id, bind_id)
    WHERE kind = 'task_copilot';

-- Declared here because `conversations` is created long before its parent:
-- invariant 2 (every conversation belongs to exactly one identity) is the FK.
ALTER TABLE conversations
    ADD CONSTRAINT fk_conversations_ref
    FOREIGN KEY (conversation_ref_id) REFERENCES conversation_refs(id) ON DELETE CASCADE;

-- Session links to trigger/integration/ref/telephony (declared after those tables).
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_trigger
    FOREIGN KEY (trigger_id) REFERENCES integration_triggers(id) ON DELETE SET NULL;
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_integration
    FOREIGN KEY (integration_id) REFERENCES integrations(id) ON DELETE SET NULL;
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_conversation_ref
    FOREIGN KEY (conversation_ref_id) REFERENCES conversation_refs(id) ON DELETE SET NULL;
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_phone_number
    FOREIGN KEY (phone_number_id) REFERENCES phone_numbers(id) ON DELETE SET NULL;
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_telephony_account
    FOREIGN KEY (telephony_account_id) REFERENCES telephony_accounts(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_phone_number
    ON sessions(tenant_id, phone_number_id)
    WHERE phone_number_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_telephony_account
    ON sessions(tenant_id, telephony_account_id)
    WHERE telephony_account_id IS NOT NULL;
-- SET NULL rather than CASCADE, unlike the connection's own agent FK: deleting a
-- partner integration must not delete the calls it carried, which are the
-- metering an invoice is built from.
ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_stream_connection
    FOREIGN KEY (stream_connection_id) REFERENCES stream_connections(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_stream_connection
    ON sessions(tenant_id, stream_connection_id)
    WHERE stream_connection_id IS NOT NULL;

-- ── batch outbound calling ────────────────────────────────────────────────
--
-- Two tables, and the split between them is the whole design: `call_batches` is
-- POLICY (schedule, business hours, concurrency, agent, retry rule,
-- paused-or-not, cancelled-or-not — one row, editable at any time), and
-- `call_batch_recipients` is WORK (who has been called and how it went — one row
-- per person). Every user action is a write to policy and touches no work at
-- all; only the dispatcher and the voice worker write work rows.
--
-- A batch's state is therefore entirely re-derivable from these two tables: the
-- dispatcher caches nothing between passes, and every transition it makes is a
-- compare-and-set whose WHERE names the state it expects to find.
--
-- Declared after `sessions`, `agents` and `phone_numbers` because it references
-- all three.
CREATE TABLE IF NOT EXISTS call_batches (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 TEXT NOT NULL,
    -- ON DELETE SET NULL, matching `sessions`: a batch must never be the reason
    -- `delete_agent` starts failing. A live batch whose agent or number
    -- disappears finds NULL here and fails itself with a clear reason.
    agent_id             UUID REFERENCES agents(id) ON DELETE SET NULL,
    from_phone_number_id UUID REFERENCES phone_numbers(id) ON DELETE SET NULL,
    -- Deliberately no agent_version_id and no agent_name / from_e164 snapshot:
    -- every call runs the agent's current published version, and a cache on an
    -- editable policy row goes stale the moment somebody renames the agent.
    --
    -- The campaign's plan, validated once at create and copied to every call it
    -- places. No `agent_name` cache beside it for the same reason: the
    -- plan is the only copy, and the batch page reads the entry name out of it.
    agent_plan           JSONB,
    -- The campaign's `{{vars.*}}` values, copied onto every sessions.vars
    -- it places. Batch-level, not per recipient: per-person data is what
    -- call_batch_recipients.userdata is for.
    vars                 JSONB,
    status               TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','running','paused','completed','canceled','failed')),
    -- What most recently went wrong, in plain English: written beside every
    -- increment of the counter below, cleared by any good outcome, and on a
    -- failed batch it is the circuit breaker's verdict.
    failure_reason       TEXT,
    -- Rolling, not cumulative: reset to 0 by any non-setup outcome. Fires at 10.
    consecutive_setup_failures INTEGER NOT NULL DEFAULT 0,
    start_at             TIMESTAMPTZ,
    timezone             TEXT NOT NULL,
    window_start_local   TIME,
    window_end_local     TIME,
    window_days          INTEGER[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    CONSTRAINT call_batches_window_both_or_neither
        CHECK ((window_start_local IS NULL) = (window_end_local IS NULL)),
    CONSTRAINT call_batches_window_non_empty
        CHECK (window_start_local IS NULL OR window_start_local <> window_end_local),
    CONSTRAINT call_batches_window_days_non_empty
        CHECK (array_length(window_days, 1) >= 1),
    CONSTRAINT call_batches_window_days_iso
        CHECK (window_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]),
    max_concurrency      INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 10),
    max_attempts         INTEGER NOT NULL DEFAULT 1 CHECK (max_attempts BETWEEN 1 AND 5),
    retry_after_minutes  INTEGER NOT NULL DEFAULT 30
        CHECK (retry_after_minutes BETWEEN 5 AND 1440),
    -- Immutable except by append, which bumps it in the same transaction that
    -- inserts the rows. Stored so a progress bar does not need a COUNT(*) per
    -- batch on every list render; the other counts stay derived.
    total_recipients     INTEGER NOT NULL DEFAULT 0,
    -- The pace email has: a gap between STARTING two calls, and a daily limit
    -- (fixed or a ramp) on calls placed, retries included. `last_dial_at` is
    -- when the last call was claimed, which the gap is measured from.
    dial_gap_seconds     INTEGER NOT NULL DEFAULT 0
        CHECK (dial_gap_seconds BETWEEN 0 AND 3600),
    dial_daily_cap       INTEGER CHECK (dial_daily_cap IS NULL OR dial_daily_cap >= 1),
    dial_ramp_start      INTEGER,
    dial_ramp_end        INTEGER,
    dial_ramp_step       INTEGER,
    dial_ramp_interval_days INTEGER,
    -- `dial_days` / `last_dial_day` are the local days on which the batch
    -- placed at least one call; `dial_ramp_base_days` is the completed dialling
    -- days at the moment the ramp was set, so a restart is one write.
    dial_ramp_base_days  INTEGER NOT NULL DEFAULT 0,
    dial_days            INTEGER NOT NULL DEFAULT 0,
    last_dial_day        DATE,
    last_dial_at         TIMESTAMPTZ,
    CONSTRAINT call_batches_dial_ramp CHECK (
        num_nulls(dial_ramp_start, dial_ramp_end, dial_ramp_step, dial_ramp_interval_days) = 4
        OR (
            num_nulls(dial_ramp_start, dial_ramp_end, dial_ramp_step, dial_ramp_interval_days) = 0
            AND dial_daily_cap IS NULL
            AND dial_ramp_start >= 1
            AND dial_ramp_end > dial_ramp_start
            AND dial_ramp_step >= 1
            AND dial_ramp_interval_days BETWEEN 1 AND 30
        )
    ),
    -- Control-plane id, so no FK is possible from here. The dispatcher rebuilds
    -- this batch's Context from it on every pass, and the membership join is the
    -- authorization check — a dangling id (user deleted, or removed from the
    -- org) is not a data error: the join simply fails and the batch stops.
    created_by_user_id   UUID NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at           TIMESTAMPTZ,
    -- "The dispatcher is done with this batch", NOT "the user stopped it".
    ended_at             TIMESTAMPTZ,
    tenant_id            UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_batches_tenant_created
    ON call_batches(tenant_id, created_at DESC);
-- No index on `ended_at IS NULL`: nothing looks for unfinished batches. A batch
-- is a `call.batch.dial` row in `scheduled_jobs`, so the schedule finds it.

CREATE TABLE IF NOT EXISTS call_batch_recipients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id        UUID NOT NULL REFERENCES call_batches(id) ON DELETE CASCADE,
    -- 1-based position in the operator's own file.
    row_number      INTEGER NOT NULL,
    to_e164         TEXT NOT NULL,
    -- Values stay strings: {{userdata.x}} substitution is textual, and
    -- inferring types turns a "007" prefix into 7.
    userdata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Note there is no 'canceled': cancelling is a batch-level fact, computed on
    -- read. `dialing` means "claimed and recorded"; `pending` may mean "never
    -- dialled" or "busy, waiting for next_attempt_at".
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','dialing','completed','failed')),
    -- The most recent attempt's call. Kept (not cleared) when a retryable
    -- outcome returns the row to 'pending', so the UI can still link to the
    -- attempt that came back busy.
    session_id      UUID REFERENCES sessions(id) ON DELETE SET NULL,
    -- The claim and the sessions INSERT commit together, which is what lets this
    -- be a constraint rather than a paragraph.
    CONSTRAINT call_batch_recipients_dialing_has_session
        CHECK (status <> 'dialing' OR session_id IS NOT NULL),
    -- Dials attempted. Incremented exactly once per attempt, inside the claim
    -- transaction — or by the dispatcher's compensating write when that
    -- transaction rolled back. NEVER at reconcile, and never decremented.
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_close_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       UUID NOT NULL
);
-- These columns ARE the claim query's WHERE and ORDER BY. Keep the two in step.
CREATE INDEX IF NOT EXISTS idx_batch_recipients_claim
    ON call_batch_recipients(tenant_id, batch_id, next_attempt_at NULLS FIRST, row_number)
    WHERE status = 'pending';
-- The slots subtraction, the reconcile, "is anything left?", the API's counts,
-- and the modal's status tabs — all scoped to one batch, which is why batch_id
-- sits ahead of status.
CREATE INDEX IF NOT EXISTS idx_batch_recipients_batch_status
    ON call_batch_recipients(tenant_id, batch_id, status);
-- Paginating the modal's recipient list in the operator's own order.
CREATE INDEX IF NOT EXISTS idx_batch_recipients_row
    ON call_batch_recipients(tenant_id, batch_id, row_number);
-- Settlement arrives from the voice worker holding a session id and nothing
-- else. Partial because only claimed rows have one.
CREATE INDEX IF NOT EXISTS idx_batch_recipients_session
    ON call_batch_recipients(tenant_id, session_id)
    WHERE session_id IS NOT NULL;
-- One row per number per batch — what makes appending to a running batch safe.
CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_recipients_number
    ON call_batch_recipients(batch_id, to_e164);

ALTER TABLE sessions
    ADD CONSTRAINT fk_sessions_batch
    FOREIGN KEY (batch_id) REFERENCES call_batches(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_batch
    ON sessions(tenant_id, batch_id, (COALESCE(started_at, created_at)) DESC)
    WHERE batch_id IS NOT NULL;

-- ── email outbound ────────────────────────────────────────────────────────
-- Upload a CSV, run an agent task over every row, review the drafts, send the
-- ones you picked. THREE ROWS, and which one a column belongs on is the whole
-- of the design:
--
--   email_batches          policy for DRAFTING, plus the DEFAULTS a send
--                          inherits, plus the list's identity
--     email_send_runs      policy for ONE send: its schedule, its hours, its
--                          pacing, its sender, its own breaker
--       email_batch_recipients.send_run_id   the rows that one run owns
--
-- Two job kinds with two lifetimes and a human between them, and both work the
-- same way: one job per subject, held by one long-lived coroutine that runs
-- until there is nothing left to do. `email.batch.draft` holds a batch from its
-- first row to its last and then stops existing; `email.send` is created when
-- somebody presses send and holds that send until its rows have gone, sleeping
-- through a shut window or a spent cap. Nothing stays alive across the human
-- gate: a drafted batch with no live run holds no coroutine, no timer and no row
-- in any queue.
--
-- `next_draft_at` and `next_send_at` are how a READER learns
-- when either will next act. `scheduled_jobs` cannot answer it — a waiting loop
-- holds its job `running` the whole time — and it was never the API's table to
-- read.
--
-- A row carries ONE FLAT COLUMN SPACE, filled from two directions — the CSV's
-- cells (`input`), the task's output (`output`), and the human's edits on top
-- (`overrides`). A row is sendable when every field the batch maps (to, subject,
-- body) resolves to a non-empty string in that merged space, whichever direction
-- it came from. That is what makes "the CSV has emails" and "feed it a phone
-- number and let the task find the email" the same feature.
CREATE TABLE IF NOT EXISTS email_batches (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 TEXT NOT NULL CHECK (btrim(name) <> ''),
    -- SET NULL, never RESTRICT: a batch must not be the reason deleting a task
    -- or disconnecting an integration starts failing. A live batch that finds
    -- NULL here fails itself with a clear reason, and `delete_task` refuses
    -- while a live batch still points at it.
    task_id              UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
    integration_id       UUID REFERENCES integrations(id) ON DELETE SET NULL,
    -- Deliberately NO task version pin: a batch follows the task's CURRENT
    -- published version, re-read once per pass, matching `call_batches`. Which
    -- definition drafted a given row is recorded where it belongs — on that
    -- row's own run, as `task_runs.task_version`.

    -- The DEFAULT sender; a send may override all three for that press alone,
    -- on its own `email_send_runs` row, never writing back here.
    from_email           TEXT NOT NULL,
    from_name            TEXT,
    reply_to             TEXT,
    body_format          TEXT NOT NULL DEFAULT 'text'
        CHECK (body_format IN ('text', 'html')),
    -- {to, subject, body} → column names in the merged space. Validated at
    -- create against the task's published output schema, guarded again at the
    -- task's publish and rollback, and re-checked every pass because a publish
    -- mid-batch can still move the ground.
    field_map            JSONB NOT NULL,
    -- The CSV's headers, in the operator's own order. Stored so the review table
    -- can render columns for a batch whose rows are paginated, without reading a
    -- row to find out what the columns are.
    input_columns        TEXT[] NOT NULL DEFAULT '{}',
    -- About DRAFTING and nothing else. Not 'running'/'completed': a batch that
    -- reached "completed" with 4 000 emails unsent would be a word that lies,
    -- and sending is a different job entirely.
    status               TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'drafting', 'paused', 'drafted', 'canceled', 'failed')),
    failure_reason       TEXT,
    -- Rolling, reset by any success, fires at 10. A `configuration` outcome
    -- fails the batch on the first one and never touches this — a rolling
    -- counter is the right shape for probabilistic failure and the wrong shape
    -- for deterministic failure.
    consecutive_draft_failures INTEGER NOT NULL DEFAULT 0,
    -- When DRAFTING may begin. There is no drafting window: nobody receives a
    -- draft, so restricting the hours it is written in gates nothing.
    start_at             TIMESTAMPTZ,
    timezone             TEXT NOT NULL,
    -- WHEN EMAIL MAY LEAVE, and the default every send run inherits. There is
    -- exactly one window in this feature and this is it.
    window_start_local   TIME,
    window_end_local     TIME,
    window_days          INTEGER[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    CONSTRAINT email_batches_window_both_or_neither
        CHECK ((window_start_local IS NULL) = (window_end_local IS NULL)),
    CONSTRAINT email_batches_window_non_empty
        CHECK (window_start_local IS NULL OR window_start_local <> window_end_local),
    CONSTRAINT email_batches_window_days_non_empty
        CHECK (array_length(window_days, 1) >= 1),

    -- Pacing comes in two named groups, and the asymmetry between them is
    -- deliberate. DRAFTING has a concurrency because a task run takes 30-120
    -- seconds and ten at once is genuinely ten at once. SENDING has none:
    -- Resend answers in well under a second and the gap has a 1 s floor, so a
    -- concurrency knob would have no observable effect beyond "how much
    -- provider latency do I tolerate", which is not a question to put in front
    -- of an operator. Sending is strictly serial and keeps no in-flight
    -- bookkeeping of any kind.
    draft_concurrency    INTEGER NOT NULL DEFAULT 5 CHECK (draft_concurrency BETWEEN 1 AND 10),
    draft_attempts       INTEGER NOT NULL DEFAULT 1 CHECK (draft_attempts BETWEEN 1 AND 5),
    draft_retry_after_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (draft_retry_after_minutes BETWEEN 5 AND 1440),
    -- Minimum wait between STARTING two rows, honoured as a period rather than
    -- a bare sleep. 0 by default, meaning "start them as fast as slots free":
    -- this only ever paces the RAMP-UP, because a task run outlasts any legal
    -- gap, so concurrency is the real limit. It stays as a knob because a task
    -- that fans out to a rate-limited MCP server does want its ramp-up spaced.
    draft_gap_seconds    INTEGER NOT NULL DEFAULT 0
        CHECK (draft_gap_seconds BETWEEN 0 AND 3600),

    -- The SENDING defaults. A run copies all four by value when it is created
    -- and edits its own copy afterwards, so changing them here steers the next
    -- send rather than the one already in flight.
    send_attempts        INTEGER NOT NULL DEFAULT 3 CHECK (send_attempts BETWEEN 1 AND 5),
    send_retry_after_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (send_retry_after_minutes BETWEEN 5 AND 1440),
    -- Minimum wait between starting two emails. The 1 s floor keeps Resend's
    -- 10 rps out of reach. The hour ceiling is safe because the loop sleeps it in
    -- chunks of at most `recheck_seconds` — a single hour-long sleep would hold
    -- a claimed job deaf to a pause for that hour.
    send_gap_seconds     INTEGER NOT NULL DEFAULT 1
        CHECK (send_gap_seconds BETWEEN 1 AND 3600),
    -- How many emails this BATCH may send in one local day. NULL is uncapped.
    -- The safeguard that replaced a per-press row cap: a rate, not a count,
    -- because 5 000 rows is a hundred clicks of "select all on page" and that is
    -- not a hundred acts of reading. Enforced per batch, which is an MVP
    -- approximation — the axis a mailbox provider actually judges is the sending
    -- DOMAIN, and two batches on one domain each get their own allowance here.
    send_daily_cap       INTEGER DEFAULT 200 CHECK (send_daily_cap IS NULL OR send_daily_cap >= 1),
    -- A daily limit that ramps up: `start` a day, raised by `step` every
    -- `interval_days` SENDING days, up to `end`. A row holds a fixed limit, a
    -- ramp, or neither.
    send_ramp_start      INTEGER,
    send_ramp_end        INTEGER,
    send_ramp_step       INTEGER,
    send_ramp_interval_days INTEGER,
    -- `send_days` / `last_send_day` are facts about the batch, independent of
    -- any ramp: the local days (in the batch's own timezone) on which it sent
    -- at least once. `send_ramp_base_days` is the completed sending days at the
    -- moment the ramp was set, so a restart is one write and a send run's copy
    -- keeps its progress.
    send_ramp_base_days  INTEGER NOT NULL DEFAULT 0,
    send_days            INTEGER NOT NULL DEFAULT 0,
    last_send_day        DATE,
    CONSTRAINT email_batches_send_ramp CHECK (
        num_nulls(send_ramp_start, send_ramp_end, send_ramp_step, send_ramp_interval_days) = 4
        OR (
            num_nulls(send_ramp_start, send_ramp_end, send_ramp_step, send_ramp_interval_days) = 0
            AND send_daily_cap IS NULL
            AND send_ramp_start >= 1
            AND send_ramp_end > send_ramp_start
            AND send_ramp_step >= 1
            AND send_ramp_interval_days BETWEEN 1 AND 30
        )
    ),
    total_recipients     INTEGER NOT NULL DEFAULT 0,
    created_by_user_id   UUID NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at           TIMESTAMPTZ,
    -- When the last row finished drafting. Purely a record: drafting is a
    -- scheduled job and the scheduler owns "what runs next". It says NOTHING
    -- about sending, which is a different job. `redraft` clears it, which is the
    -- one transition that runs backwards.
    drafted_at           TIMESTAMPTZ,
    -- When drafting will next do something, and why. Written by the drafting
    -- coroutine whenever it waits, cleared while it is working and on the way
    -- out. 'start' is a start time that has not arrived; 'retry' is every
    -- remaining row waiting out its `next_attempt_at`.
    next_draft_at        TIMESTAMPTZ,
    next_draft_reason    TEXT
        CHECK (next_draft_reason IS NULL OR next_draft_reason IN ('start', 'retry')),
    tenant_id            UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_batches_tenant_created
    ON email_batches(tenant_id, created_at DESC);
-- No discovery index here, deliberately. `idx_scheduled_jobs_due` is the only
-- "what needs running" index this feature relies on — there is no loop of ours
-- to serve.

-- ONE SEND, and a send is a SCOPE. `scope = 'all'` is a STANDING send: every row
-- of the batch that reaches `draft`, including rows not drafted yet, with no
-- membership stored. `scope = 'selected'` is a fixed set, recorded in
-- `email_send_members`. A covered `draft` row is REPORTED as `queued` on read;
-- nothing stores it.
--
-- Created when a human picks rows and presses send, and from then on
-- it is the thing that can be scheduled, paused, resumed, cancelled and paced —
-- because it is a row, and every one of those is a plain UPDATE on it.
--
-- Its lifecycle is INDEPENDENT of the batch's. Pausing, cancelling or failing
-- the batch stops DRAFTING and must not touch a run or a row a human has
-- pressed send on: the operator who stops a batch is stopping the spend, not
-- throwing away the fifty drafts they had already read. The only things that may
-- stop a run are its own status, its own breaker, and an account-level refusal
-- from the provider.
CREATE TABLE IF NOT EXISTS email_send_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id            UUID NOT NULL REFERENCES email_batches(id) ON DELETE CASCADE,
    -- Resolved once, by value: a run's sender exists nowhere else and never
    -- writes back to the batch, so the next send starts from the batch's own
    -- default again.
    from_email          TEXT NOT NULL,
    from_name           TEXT,
    reply_to            TEXT,
    scope               TEXT NOT NULL CHECK (scope IN ('all', 'selected')),
    -- `scheduled` waits on its start time, its window and the daily cap;
    -- `sending` has begun; `sent` finished; `paused` has nothing scheduled;
    -- `canceled` and `failed` leave every unsent row as the `draft` it was.
    status              TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'sending', 'paused', 'sent', 'canceled', 'failed')),
    failure_reason      TEXT,
    -- The run's own breaker, and it is NOT optional. `store.defer` resets a
    -- job's `attempts` to 0 by design, so the moment sending works in passes the
    -- job-level MAX_ATTEMPTS stops bounding anything and a run whose every row
    -- 429s would defer for ever. Rolling, reset by any success, and moved only
    -- by failures that are about the ACCOUNT or the provider — a row Resend
    -- refuses by address is a fact about that row's data and never touches it.
    consecutive_send_failures INTEGER NOT NULL DEFAULT 0,
    start_at            TIMESTAMPTZ,
    -- The run's own clock, copied from the batch and editable while scheduled.
    -- Every instant below is interpreted in it, and never in the server's.
    timezone            TEXT NOT NULL,
    window_start_local  TIME,
    window_end_local    TIME,
    window_days         INTEGER[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    CONSTRAINT email_send_runs_window_both_or_neither
        CHECK ((window_start_local IS NULL) = (window_end_local IS NULL)),
    CONSTRAINT email_send_runs_window_non_empty
        CHECK (window_start_local IS NULL OR window_start_local <> window_end_local),
    CONSTRAINT email_send_runs_window_days_non_empty
        CHECK (array_length(window_days, 1) >= 1),
    -- The batch's defaults, copied by value at create. Editable on the run.
    send_attempts       INTEGER NOT NULL CHECK (send_attempts BETWEEN 1 AND 5),
    send_retry_after_minutes INTEGER NOT NULL
        CHECK (send_retry_after_minutes BETWEEN 5 AND 1440),
    send_gap_seconds    INTEGER NOT NULL CHECK (send_gap_seconds BETWEEN 1 AND 3600),
    -- NULL is uncapped. Counted against the BATCH's sends for the local day, so
    -- two runs of one batch share one allowance rather than each spending it.
    send_daily_cap      INTEGER CHECK (send_daily_cap IS NULL OR send_daily_cap >= 1),
    -- The run's copy of the batch's ramp. It counts against the batch's sending
    -- days, so it carries a baseline and no counter of its own.
    send_ramp_start     INTEGER,
    send_ramp_end       INTEGER,
    send_ramp_step      INTEGER,
    send_ramp_interval_days INTEGER,
    send_ramp_base_days INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT email_send_runs_send_ramp CHECK (
        num_nulls(send_ramp_start, send_ramp_end, send_ramp_step, send_ramp_interval_days) = 4
        OR (
            num_nulls(send_ramp_start, send_ramp_end, send_ramp_step, send_ramp_interval_days) = 0
            AND send_daily_cap IS NULL
            AND send_ramp_start >= 1
            AND send_ramp_end > send_ramp_start
            AND send_ramp_step >= 1
            AND send_ramp_interval_days BETWEEN 1 AND 30
        )
    ),
    -- NULL on a standing send, which cannot know its total while drafting runs.
    total_recipients    INTEGER,
    -- When the send loop will next act, and why: its start time, a shut
    -- business-hours window, a spent daily cap, rows waiting out a retry,
    -- `send_gap_seconds` between two emails, or a standing send that has caught
    -- up with drafting (which has no instant to name, so `next_send_at` is NULL).
    next_send_at        TIMESTAMPTZ,
    next_send_reason    TEXT
        CHECK (next_send_reason IS NULL OR next_send_reason IN
               ('start', 'window', 'daily_cap', 'retry', 'gap', 'drafting')),
    created_by_user_id  UUID NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    tenant_id           UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_send_runs_batch
    ON email_send_runs(tenant_id, batch_id, created_at DESC);
-- At most one live standing send per batch. That, plus `create_send` refusing a
-- standing and a scoped send live together, is what lets the standing claim be
-- a plain `batch_id` predicate with no exclusion join.
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_send_runs_one_standing
    ON email_send_runs(batch_id)
    WHERE scope = 'all' AND status IN ('scheduled', 'sending', 'paused');

CREATE TABLE IF NOT EXISTS email_batch_recipients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id        UUID NOT NULL REFERENCES email_batches(id) ON DELETE CASCADE,
    row_number      INTEGER NOT NULL,
    -- The uploaded half — and what is handed to the task as its `vars`.
    input           JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- The generated half: the whole validated `submit_result` payload.
    output          JSONB,
    -- The human's edits. Merged LAST, never written by a job.
    overrides       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'drafting', 'draft', 'draft_failed',
                          'skipped', 'sending', 'sent', 'send_failed')),
    -- Which send delivered this row: written when a send CLAIMS it. A real FK
    -- to a row a person can open — the pointer this replaced named a
    -- `scheduled_jobs` row, which is infrastructure and prunable.
    send_run_id     UUID REFERENCES email_send_runs(id) ON DELETE SET NULL,
    skip_reason     TEXT,   -- 'operator' | 'duplicate_recipient'
    -- The latest drafting attempt, written in the SAME transaction as the claim
    -- — which is what lets the CHECK enforce the rule rather than describe it,
    -- and why there is no orphan sweep.
    task_run_id     UUID REFERENCES task_runs(id) ON DELETE SET NULL,
    CONSTRAINT email_recipients_drafting_has_run
        CHECK (status <> 'drafting' OR task_run_id IS NOT NULL),
    -- The resolved destination, written when the row is claimed for sending,
    -- and the address it actually went FROM. Columns rather than a lookup
    -- through `send_run_id` because the run's sender can be one of several, and
    -- "which domain did this go from" is the first question asked when a domain
    -- gets flagged.
    to_email        TEXT,
    sent_from       TEXT,
    -- Resend's own id for the email, kept so a person can find this exact send
    -- in Resend's dashboard — which is where delivery, bounces and spam
    -- complaints live. Nothing here reads it back.
    provider_message_id TEXT,
    sent_at         TIMESTAMPTZ,
    attempts        INTEGER NOT NULL DEFAULT 0,        -- drafting attempts
    send_attempts   INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       UUID NOT NULL
);
-- These columns ARE the drafting claim's WHERE and ORDER BY. Keep the two in step.
CREATE INDEX IF NOT EXISTS idx_email_recipients_draft
    ON email_batch_recipients(tenant_id, batch_id, next_attempt_at NULLS FIRST, row_number)
    WHERE status = 'pending';
-- ...and these ARE the standing send claim's. Same discipline.
CREATE INDEX IF NOT EXISTS idx_email_recipients_send
    ON email_batch_recipients(tenant_id, batch_id, next_attempt_at NULLS FIRST, row_number)
    WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_email_recipients_batch_status
    ON email_batch_recipients(tenant_id, batch_id, status);
-- The daily cap's count, asked inside EVERY send claim — which is what stops two
-- sends of one batch each spending the whole allowance. Without this index that
-- is a scan of a 5 000-row batch per email.
CREATE INDEX IF NOT EXISTS idx_email_recipients_sent_at
    ON email_batch_recipients(tenant_id, batch_id, sent_at)
    WHERE sent_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_recipients_row
    ON email_batch_recipients(batch_id, row_number);
-- One address per batch, armed at send-claim time because the address may not
-- exist before the task has run. A 23505 here is the guarantee working: the
-- second row settles `skipped` / 'duplicate_recipient' rather than mailing one
-- inbox twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_recipients_to
    ON email_batch_recipients(batch_id, lower(to_email))
    WHERE to_email IS NOT NULL;

-- The membership of a scoped send. Insert-only: three narrow columns, so a
-- 5 000-row send costs 5 000 small inserts rather than 5 000 updates of a wide
-- row, and cancelling it writes nothing here at all.
CREATE TABLE IF NOT EXISTS email_send_members (
    send_run_id  UUID NOT NULL REFERENCES email_send_runs(id) ON DELETE CASCADE,
    recipient_id UUID NOT NULL REFERENCES email_batch_recipients(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL,
    PRIMARY KEY (send_run_id, recipient_id)
);
-- "Is this row in a live send?" — the review table's `queued` label and the
-- check that keeps one row out of two live sends. The scoped claim itself is
-- served by the primary key.
CREATE INDEX IF NOT EXISTS idx_email_send_members_recipient
    ON email_send_members(tenant_id, recipient_id);

-- ── WhatsApp outbound ─────────────────────────────────────────────────────
-- A sender at a BSP (Twilio or Gupshup) is an integration, and an outbound
-- template campaign is a batch of CSV rows sent one template each.
--
-- ONE BATCH = one approved template sent once to every row of a CSV. There is
-- no drafting step (Meta approved the wording; only the variables change per
-- row) and one send per batch, so the policy for that send lives here.
CREATE TABLE IF NOT EXISTS whatsapp_batches (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL,
    name                 TEXT NOT NULL CHECK (btrim(name) <> ''),
    -- SET NULL, never RESTRICT: a batch must not be the reason disconnecting a
    -- sender fails. A live batch that finds NULL here fails itself.
    integration_id       UUID REFERENCES integrations(id) ON DELETE SET NULL,
    -- Twilio `HX…` Content SID or Gupshup's template UUID, plus a snapshot taken
    -- at create: what the rows were rendered and sent against, even after the
    -- template is edited or deleted at the BSP.
    template_id          TEXT NOT NULL,
    template_name        TEXT NOT NULL,
    template_language    TEXT NOT NULL,
    template_category    TEXT NOT NULL,
    template_body        TEXT NOT NULL,
    template_variables   TEXT[] NOT NULL DEFAULT '{}',
    -- The rest of the snapshot. `template_header` is `{type, text, url}`;
    -- `template_buttons` is a list of `{type, text, url, phone_number, code}`.
    template_header      JSONB,
    template_footer      TEXT,
    template_buttons     JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The CSV column holding the phone number, and template variable -> CSV
    -- column for every variable the template has.
    to_column            TEXT NOT NULL,
    variable_map         JSONB NOT NULL,
    input_columns        TEXT[] NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'scheduled', 'sending', 'paused', 'completed',
                          'canceled', 'failed')),
    failure_reason       TEXT,
    -- Rolling, reset by any success; 10 in a row pauses the batch. Only
    -- transient provider failures move it — a bad number is the row's problem.
    consecutive_send_failures INTEGER NOT NULL DEFAULT 0,
    start_at             TIMESTAMPTZ,
    timezone             TEXT NOT NULL DEFAULT 'UTC',
    window_start_local   TIME,
    window_end_local     TIME,
    window_days          INTEGER[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    CONSTRAINT whatsapp_batches_window_both_or_neither
        CHECK ((window_start_local IS NULL) = (window_end_local IS NULL)),
    CONSTRAINT whatsapp_batches_window_non_empty
        CHECK (window_start_local IS NULL OR window_start_local <> window_end_local),
    CONSTRAINT whatsapp_batches_window_days_non_empty
        CHECK (array_length(window_days, 1) >= 1),
    -- 250 is Meta's lowest messaging tier (unique recipients per 24h, shared by
    -- every number in the business portfolio). NULL is uncapped.
    send_daily_cap       INTEGER DEFAULT 250 CHECK (send_daily_cap IS NULL OR send_daily_cap >= 1),
    -- A daily limit that ramps up: `start` a day, raised by `step` every
    -- `interval_days` SENDING days, up to `end`. A row holds a fixed limit, a
    -- ramp, or neither.
    send_ramp_start      INTEGER,
    send_ramp_end        INTEGER,
    send_ramp_step       INTEGER,
    send_ramp_interval_days INTEGER,
    send_ramp_base_days  INTEGER NOT NULL DEFAULT 0,
    send_days            INTEGER NOT NULL DEFAULT 0,
    last_send_day        DATE,
    CONSTRAINT whatsapp_batches_send_ramp CHECK (
        num_nulls(send_ramp_start, send_ramp_end, send_ramp_step, send_ramp_interval_days) = 4
        OR (
            num_nulls(send_ramp_start, send_ramp_end, send_ramp_step, send_ramp_interval_days) = 0
            AND send_daily_cap IS NULL
            AND send_ramp_start >= 1
            AND send_ramp_end > send_ramp_start
            AND send_ramp_step >= 1
            AND send_ramp_interval_days BETWEEN 1 AND 30
        )
    ),
    send_gap_seconds     INTEGER NOT NULL DEFAULT 1 CHECK (send_gap_seconds BETWEEN 1 AND 3600),
    send_attempts        INTEGER NOT NULL DEFAULT 3 CHECK (send_attempts BETWEEN 1 AND 5),
    send_retry_after_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (send_retry_after_minutes BETWEEN 5 AND 1440),
    -- Resending a template Meta held back for its per-person marketing limit
    -- (Twilio 63049, Meta 131049), which can go through later.
    -- `delivery_attempts` counts the first send. The gap has a 24-hour floor
    -- because Meta asks for at least that before resending to someone at their
    -- limit, and warns that sooner retries can suspend delivery.
    delivery_attempts    INTEGER NOT NULL DEFAULT 3
        CHECK (delivery_attempts BETWEEN 1 AND 5),
    delivery_retry_after_hours INTEGER NOT NULL DEFAULT 48
        CHECK (delivery_retry_after_hours BETWEEN 24 AND 168),
    -- When the send loop next acts, and why. Written by the loop whenever it
    -- waits; `scheduled_jobs` is machinery and no API response reads it.
    next_send_at         TIMESTAMPTZ,
    next_send_reason     TEXT
        CHECK (next_send_reason IS NULL OR next_send_reason IN
               ('start', 'window', 'daily_cap', 'retry', 'gap')),
    total_recipients     INTEGER NOT NULL DEFAULT 0,
    created_by_user_id   UUID NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at           TIMESTAMPTZ,
    -- When a person last resumed it: the delivery breaker counts only messages
    -- sent since, so a resume starts it from zero.
    resumed_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_whatsapp_batches_tenant_created
    ON whatsapp_batches(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS whatsapp_batch_recipients (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL,
    batch_id             UUID NOT NULL REFERENCES whatsapp_batches(id) ON DELETE CASCADE,
    row_number           INTEGER NOT NULL,
    input                JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- A person's edits to template-variable cells, merged over `input`.
    overrides            JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- NULL on a row with a blank, invalid or repeated phone: it is kept and
    -- skipped rather than refusing the upload, so it has no number until its
    -- cell is fixed. `uq_whatsapp_recipients_to` ignores NULLs.
    to_e164              TEXT,
    -- `ready` can be sent; `skipped` by a person (`operator`), because a mapped
    -- cell cannot be sent as a WhatsApp variable (`unfillable`), or because the
    -- number repeats (`duplicate_recipient`). From `sending` on, the row moves
    -- forward only.
    status               TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'skipped', 'sending', 'queued', 'sent', 'delivered',
                          'read', 'undelivered', 'failed')),
    skip_reason          TEXT CHECK (skip_reason IS NULL OR skip_reason IN
                                     ('operator', 'unfillable', 'duplicate_recipient')),
    provider_message_id  TEXT,
    error_code           TEXT,
    last_error           TEXT,
    send_attempts        INTEGER NOT NULL DEFAULT 0,
    -- How many times the BSP accepted this row's template. `send_attempts`
    -- counts claims toward one delivery attempt and starts again with each one.
    delivery_attempts    INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TIMESTAMPTZ,
    sending_started_at   TIMESTAMPTZ,
    sent_at              TIMESTAMPTZ,
    delivered_at         TIMESTAMPTZ,
    read_at              TIMESTAMPTZ,
    -- The thread this row's template landed in, set at send, so a reply is
    -- answered by the sender's agent with the row's CSV values and the template
    -- already in context.
    conversation_id      UUID REFERENCES conversations(id) ON DELETE SET NULL,
    replied_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_recipients_row
    ON whatsapp_batch_recipients(batch_id, row_number);
CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_recipients_to
    ON whatsapp_batch_recipients(batch_id, to_e164);
-- These columns ARE the send claim's WHERE and ORDER BY. Keep the two in step.
CREATE INDEX IF NOT EXISTS idx_whatsapp_recipients_claim
    ON whatsapp_batch_recipients(tenant_id, batch_id, next_attempt_at NULLS FIRST, row_number)
    WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS idx_whatsapp_recipients_batch_status
    ON whatsapp_batch_recipients(tenant_id, batch_id, status);
-- The daily cap's count, asked inside every claim.
CREATE INDEX IF NOT EXISTS idx_whatsapp_recipients_sent_at
    ON whatsapp_batch_recipients(tenant_id, batch_id, sent_at)
    WHERE sent_at IS NOT NULL;
-- Status callbacks find their row by the BSP's message id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_recipients_provider_message
    ON whatsapp_batch_recipients(tenant_id, provider_message_id)
    WHERE provider_message_id IS NOT NULL;
-- An inbound message marks the latest row sent to that number from that sender
-- as replied — whether or not an agent is assigned to answer it.
CREATE INDEX IF NOT EXISTS idx_whatsapp_recipients_awaiting_reply
    ON whatsapp_batch_recipients(tenant_id, to_e164)
    WHERE sent_at IS NOT NULL AND replied_at IS NULL;

CREATE TABLE IF NOT EXISTS conversation_items (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id         UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    session_id            UUID REFERENCES sessions(id) ON DELETE SET NULL,
    trigger_item_id         UUID,
    direction               TEXT NOT NULL CHECK (direction IN ('inbound','outbound','internal')),
    -- Transport (web/telegram): join conversation_refs through
    -- conversations.conversation_ref_id, or conversations.metadata /
    -- sessions.channel (modality) via session_id.
    -- Mirrors livekit.agents.llm.chat_context.ChatItem discriminator values.
    type                    TEXT NOT NULL
        CHECK (type IN (
            'message',
            'function_call',
            'function_call_output',
            'agent_handoff',
            'agent_config_update'
        )),
    -- livekit.agents.llm.ChatRole, plus the 'tool' we stamp on a function call
    -- and its output, which LiveKit gives no role of its own. NULL stays legal:
    -- a CHECK passes on NULL, and the column has always been nullable.
    role                    TEXT
        CHECK (role IN ('developer','system','user','assistant','tool')),
    agent_id                UUID REFERENCES agents(id) ON DELETE SET NULL,
    agent_version           INTEGER,
    text                    TEXT,
    attachments             JSONB NOT NULL DEFAULT '[]'::jsonb,
    provider_message_id     TEXT,
    client_message_id       TEXT,
    delivery_status         TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (delivery_status IN (
            'not_applicable','pending','sending','sent','failed','skipped'
        )),
    delivery_error          JSONB,
    -- The lifecycle of the turn this item OPENED, and why it stopped. Set only
    -- on inbound user messages that queue a TextTurnJob; NULL on everything a
    -- turn produces. Mirrors services.messaging.TurnPayload.status, and is what
    -- "is a turn still working?" is read from — never the shape of the
    -- timeline, which cannot tell a live tool call from a dead one.
    turn_status             TEXT
        CHECK (turn_status IN ('running','done','error','canceled')),
    -- Why a turn stopped, kept next to the status rather than written into the
    -- timeline as its own item: an error item would be loaded back into the
    -- model's chat context on the next turn, and a CoPilot reading its own crash
    -- report as conversation history is worse than one that says nothing. As a
    -- column it is invisible to the model, visible to the rail, and survives a
    -- reload — the `turn` SSE frame that first announced the failure does not.
    turn_error              TEXT,
    source                  TEXT,
    -- public: end-customer / web-visible content. internal: tools, platform,
    -- handoffs, undelivered-only is NOT a third state — use delivery_status.
    visibility              TEXT NOT NULL DEFAULT 'internal'
        CHECK (visibility IN ('customer_visible', 'internal')),
    -- LiveKit MetricsReport samples (seconds) for message items; null otherwise.
    metrics                 JSONB,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload             JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id               UUID NOT NULL
);
-- Timeline order is (created_at, id); no separate sequence column.
CREATE INDEX IF NOT EXISTS idx_conversation_items_conversation_created
    ON conversation_items(tenant_id, conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_conversation_items_session
    ON conversation_items(tenant_id, session_id);
CREATE INDEX IF NOT EXISTS idx_conversation_items_tenant_agent_created
    ON conversation_items(tenant_id, agent_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_items_client_message
    ON conversation_items(tenant_id, conversation_id, client_message_id)
    WHERE client_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_items_provider_message
    ON conversation_items(tenant_id, conversation_id, provider_message_id)
    WHERE provider_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_items_livekit_item
    ON conversation_items(tenant_id, session_id, (metadata->>'livekit_item_id'))
    WHERE session_id IS NOT NULL AND metadata->>'livekit_item_id' IS NOT NULL;
ALTER TABLE conversation_items
    ADD CONSTRAINT fk_conversation_items_trigger_item
    FOREIGN KEY (trigger_item_id) REFERENCES conversation_items(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS secrets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    value_encrypted TEXT NOT NULL,
    -- last-4 hint stored at write time so list never decrypts values
    value_hint      TEXT NOT NULL,
    created_by      UUID,                              -- control.users.id (attribution only)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_secrets_tenant ON secrets(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_secrets_name ON secrets(tenant_id, name);

-- BYOK: the tenant's own key for each AI provider in catalog.yaml. Talqing
-- holds no platform key for agent runs, so a provider with no row here cannot
-- be published or run. One row per provider (rotating = updating that row).
-- Every row was proved against its provider before it was written, so a row
-- here means a key that worked, not just a key that was pasted.
CREATE TABLE IF NOT EXISTS provider_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    -- last-4 hint stored at write time so list never decrypts values
    api_key_hint    TEXT NOT NULL,
    -- what the provider called the account when it accepted the key; '' for the
    -- providers that publish no account identity. Display only.
    account_label   TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_keys_tenant ON provider_keys(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_keys_provider
    ON provider_keys(tenant_id, provider);

-- ── FAQs ──────────────────────────────────────────────────────────────────
-- A named list of question/answer pairs a tenant writes and attaches to agents
-- and tasks by id (`config.faqs`). The content is live — a session reads it at
-- start — so there is no versions table, and nothing here is frozen by an agent
-- publish.

CREATE TABLE IF NOT EXISTS faqs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    created_by  UUID,                      -- control.users.id (attribution only)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Bumped by every entry write too: it is "last edited", which is what the
    -- list shows.
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id   UUID NOT NULL
);
-- Case-insensitive, because the name is what an author picks an FAQ by.
CREATE UNIQUE INDEX IF NOT EXISTS uq_faqs_name ON faqs(tenant_id, lower(name));

CREATE TABLE IF NOT EXISTS faq_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    faq_id      UUID NOT NULL REFERENCES faqs(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    -- Also the entry's position: entries read back in the order they were
    -- added, and rows added together are spaced a microsecond apart so that
    -- order survives a bulk insert.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id   UUID NOT NULL
);
-- The read order. It has to be stable: the questions are rendered into the
-- system prompt, and a prompt that reorders between turns misses the
-- provider's prompt cache.
CREATE INDEX IF NOT EXISTS idx_faq_entries_faq ON faq_entries(faq_id, created_at, id);
-- Two entries asking the same thing would give the model two ids for one
-- question and no way to choose between their answers.
CREATE UNIQUE INDEX IF NOT EXISTS uq_faq_entries_question
    ON faq_entries(faq_id, lower(btrim(question)));

-- The CoPilots (AgentCoPilot, ToolCoPilot, TaskCoPilot) use the same
-- conversations / conversation_refs / conversation_items tables as customer
-- product. conversation_refs.kind is 'agent_copilot', 'tool_copilot' or
-- 'task_copilot'; bind_id is the subject agent, tool or task.
-- They never write sessions (unbilled).
-- Customer inbox APIs must filter kind IN ('web','integration','sip').

-- ── deferred work ─────────────────────────────────────────────────────────
-- One row per job that must run at a moment in the future. THE TABLE HOLDS THE
-- SCHEDULE: Kafka only ever carries "this job is due now", because retention
-- outlives any sane Kafka retention and a topic is not a database.
--
-- The `job-scheduler` container finds due rows and publishes them;
-- `background-worker` consumes and claims each with a conditional UPDATE, which
-- is what makes at-least-once delivery and consumer-group rebalances harmless.
--
-- EVERY CLAIM CARRIES A LEASE. This table shipped without one, on the reasoning
-- that an automatic reclaim needs a lease that outlasts the longest job and
-- getting that wrong runs one job twice. That was half right: a graceful stop
-- releases its jobs, but a SIGKILL, an OOM or a lost node leaves the row
-- `running` for ever with nothing to retry it and an operator resetting rows by
-- hand. The lease is safe because of a contract that already existed —
-- **every kind must be safe to re-run from the start** — and each kind pays for
-- it: `email.send` by the 24-hour `Idempotency-Key` every row carries, the two
-- batch passes by their per-row compare-and-set claims. A handler that is still alive renews its lease from a
-- task of its own, so "the lease lapsed" means the holder is gone, not slow.
--
-- `done` rows are kept as the record that the job ran. For `session.purge` the
-- durable receipt is `sessions.content_deleted_at`, so these are redundant once
-- stamped — a tenant with retention accumulates one per call, and pruning them
-- is the first thing to do if this table ever gets big.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         TEXT NOT NULL,
    -- Everything the handler needs, by value. Never a pointer to config that
    -- may have changed by the time it runs.
    args         JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- The row this job acts on — a session for `session.purge`, null for a kind
    -- that acts on the whole workspace. Its own column rather than a key inside
    -- `args` so "is there a job pending for this thing?" is one indexed lookup.
    subject_id   UUID,
    scheduled_at TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    -- When the scheduler last put this on Kafka. What stops every tick
    -- republishing the same due job, and what lets it republish one an executor
    -- consumed and then died before claiming.
    published_at TIMESTAMPTZ,
    -- When an executor claimed it, which container has it, and how long that
    -- claim is good for. The lease is renewed while the handler runs; once it
    -- lapses the row is due again and the next `store.claim` takes it. A job
    -- whose lease is still LIVE and which has been running abnormally long is
    -- reported as stuck — a prompt to look, not a row to reset.
    started_at   TIMESTAMPTZ,
    claimed_by   TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    UUID NOT NULL
);
-- The scheduler's poll: due, unclaimed work across every tenant in this data
-- plane. Deliberately NOT led by tenant_id — that would make it a full scan,
-- and the query is cross-tenant by design: it returns ids and args only, and
-- everything the executor touches afterwards is scoped by the tenant_id it
-- read here.
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
    ON scheduled_jobs(scheduled_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_running
    ON scheduled_jobs(started_at)
    WHERE status = 'running';
-- The other half of "what is due": a claim whose holder died ungracefully. The
-- scheduler's poll reads both halves in one query, so a lapsed lease is found
-- by the mechanism that finds everything else rather than by a sweep of its own.
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_lease
    ON scheduled_jobs(lease_expires_at)
    WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_subject
    ON scheduled_jobs(tenant_id, kind, subject_id)
    WHERE status = 'pending';
