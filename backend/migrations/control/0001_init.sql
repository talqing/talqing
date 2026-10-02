-- Control plane (shared): organizations, users, their membership of an
-- organization, pending invites, and personal access tokens.
--
-- "tenant" is the internal name for an organization; every user-facing string
-- says organization. Renaming 30 data-plane tables would buy nothing.

CREATE TABLE IF NOT EXISTS tenants (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,              -- user-settable; shown in the org switcher
    -- The region this organization was created in, and the only thing that fact
    -- decides: which region the dashboard opens in when a member has no stored
    -- preference. It grants no privilege and blocks nothing -- every
    -- organization exists in every region, and the switcher is one click away.
    --
    -- Not redundant with the configured default region, and the reason is
    -- present tense: CHANGING THE DEFAULT MUST NOT MOVE EXISTING ORGANIZATIONS.
    -- The day `in` becomes the default, every workspace created before that must
    -- still open in `us`, where its agents and call history actually are.
    --
    -- A region slug, and plain TEXT: there is no regions table, because the
    -- region list is control's config (settings.RegionConfig). An FK would guard
    -- against a bug in our own code -- control writes this itself, from the
    -- country map -- and its other job, refusing to retire a region rows still
    -- point at, is not something we want.
    --
    -- There is deliberately no `db_dsn` here any more. With every organization
    -- in every region, a region routes every tenant to its own default database,
    -- and a tenant that outgrows it is named in THAT REGION's config -- never
    -- here, where it would be one region holding a fact about another.
    home_region TEXT NOT NULL,
    -- How long this organization's call content is kept. NULL means keep
    -- forever, and that is the default — ElevenLabs spells unlimited as -1, and
    -- null needs no explanation, so we differ deliberately.
    --
    -- Here rather than beside the calls it governs because `Tenant` is validated
    -- straight off this row and carried into every worker, so session finalize
    -- reads the policy with no extra query, on every call.
    --
    -- Per organization, not per agent: a workspace has one data policy, and it
    -- is the workspace that signs the DPA.
    retention_days INTEGER
        CHECK (retention_days IS NULL OR retention_days > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A person, independent of any organization. `email` is globally unique because
-- it is the identity an invite is keyed to (see org_invites).
CREATE TABLE IF NOT EXISTS users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email          TEXT NOT NULL UNIQUE,
    google_sub     TEXT,                   -- Google OpenID subject
    name           TEXT,                   -- from Google userinfo; shown in members lists
    picture_url    TEXT,                   -- ditto
    -- Which org to sign into when a user belongs to several. Written on login
    -- and on every switch; ON DELETE SET NULL so losing that org falls back to
    -- the oldest membership rather than dangling.
    last_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Membership IS the authorization model: no row, no access. Deleting one takes
-- effect on the member's very next request, because api.core.deps re-reads this
-- table for every authenticated call (the JWT is only a signature check).
CREATE TABLE IF NOT EXISTS memberships (
    user_id    UUID NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('ADMIN', 'EDITOR', 'VIEWER')),
    invited_by UUID REFERENCES users(id) ON DELETE SET NULL,  -- null for a personal org's founder
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_tenant ON memberships(tenant_id);

-- An invite is resolved by email identity at login, not by a link — there is no
-- token to hash and none to leak. A row for an email that already has an
-- account is written with accepted_at already set, purely as the record of who
-- invited whom.
CREATE TABLE IF NOT EXISTS org_invites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('ADMIN', 'EDITOR', 'VIEWER')),
    invited_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);
-- One live invite per email per org; revoking or accepting frees the slot.
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_invites_pending
    ON org_invites (tenant_id, lower(email))
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
-- Login looks up every pending invite for the address that just signed in.
CREATE INDEX IF NOT EXISTS idx_org_invites_pending_email
    ON org_invites (lower(email))
    WHERE accepted_at IS NULL AND revoked_at IS NULL;

-- Personal access tokens: no-expiry JWTs for driving the API headlessly.
-- The row's id IS the JWT's jti; deleting the row revokes the token. A token
-- names one org and acts with its owner's live role there, so removing the
-- owner from that org kills the token too.
--
-- `kind` marks the one token issued automatically at signup. It is the only one
-- whose plaintext can be read back (GET /v1/mcp-token) — a PAT is a signed
-- pointer to this row, so the value is re-derived rather than stored. Tokens
-- created by hand stay 'manual' and are shown once and never again.
CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual', 'mcp')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pat_user ON personal_access_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_pat_tenant ON personal_access_tokens(tenant_id);
-- One MCP token per user per org: the dashboard shows "your token", singular.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pat_one_mcp_per_user
    ON personal_access_tokens(user_id, tenant_id) WHERE kind = 'mcp';

-- Deployment-wide DCR client cache (see data-plane note in services.integrations.oauth).
CREATE TABLE IF NOT EXISTS oauth_dcr_clients (
    provider      TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, redirect_uri)
);

-- One attempt to buy credits. Written BEFORE the buyer is redirected, which is
-- what makes it the authority on the credited amount: the webhook confirms a row
-- we already own, and a payment whose checkout session we did not create credits
-- nobody.
--
-- Control plane rather than beside the balance it tops up: one row per purchase,
-- never per call, and it carries the payment identifiers and who paid — the
-- sensitive half. It is also what ROUTES an incoming webhook, which a per-tenant
-- table could not do: the callback arrives with no tenant context, and
-- `tenant_id` + `region` here are what find the balance to credit. Dodo has one
-- business and a payment carries no notion of our regions, so per-region webhook
-- endpoints are not merely unnecessary — they are not expressible.
CREATE TABLE IF NOT EXISTS credit_topups (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Which region's balance this buys. Written BEFORE the buyer ever reaches
    -- Dodo, from the credential of the region whose API started the checkout —
    -- never from a request body someone could set to the other region, where the
    -- money would then be stuck, and never from Dodo's `metadata` (that copy
    -- exists for a support reader looking at Dodo's dashboard, and is never read
    -- back). A slug, no FK: see tenants.home_region.
    region     TEXT NOT NULL,
    -- Who pressed buy. ON DELETE SET NULL: a departed admin must not be the
    -- reason a payment record disappears.
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    -- The credits this buys, in USD, taken from the pack the caller chose.
    -- THE ONLY SOURCE OF THE CREDITED AMOUNT. Never read back off the payment:
    -- `total_amount` there includes tax and, under adaptive currency, is
    -- denominated in whatever the buyer paid.
    amount     NUMERIC(12,6) NOT NULL CHECK (amount > 0),
    -- 'cancelled' is the buyer backing out; 'failed' is the payment being
    -- refused. Separate on purpose — a support reader seeing `failed` on a row
    -- where someone simply pressed cancel would go looking for a declined card
    -- that never existed. An ABANDONED checkout is neither: it stays `pending`
    -- for ever and costs nothing, which is why Dodo's abandoned-cart events are
    -- still not subscribed.
    status     TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')),
    dodo_product_id          TEXT NOT NULL,
    dodo_checkout_session_id TEXT NOT NULL UNIQUE,
    dodo_payment_id          TEXT UNIQUE,
    -- What the buyer actually paid and in what currency, copied off the payment
    -- for support. Reporting only — it never decides the credit.
    paid_amount_minor  INTEGER,
    paid_currency      TEXT,
    -- TWO stamps, because both directions are a push to the region and both can
    -- fail independently: `applied_at` when the region has written the purchase
    -- into its ledger, `reversed_at` when it has written the refund or lost
    -- dispute. NULL on a terminal status is what the billing page's reconcile
    -- looks for, and what an operator's "money we owe a customer" query reads.
    applied_at  TIMESTAMPTZ,
    reversed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_credit_topups_tenant
    ON credit_topups(tenant_id, created_at DESC);
-- What the reconcile reads on a billing-page load. Keyed (tenant_id, region)
-- because it answers "what is outstanding for THIS tenant HERE" — not "what is
-- outstanding anywhere", which is the sweep this design deliberately does not
-- have. There is no timer and no queue: `applied_at IS NULL` is an operator's
-- list, the same trade `scheduled_jobs` already makes with no lease and no
-- reclaim loop.
CREATE INDEX IF NOT EXISTS idx_credit_topups_unapplied ON credit_topups (tenant_id, region)
    WHERE (status = 'paid'     AND applied_at  IS NULL)
       OR (status = 'refunded' AND reversed_at IS NULL);

-- Credit the platform OWES a tenant in one region that was not bought: the
-- signup grant today, promotional credit later. Purchases are credit_topups.
-- Both are applied by CONTROL pushing them to the region, because the balance
-- itself is regional — the debit shares a transaction with `sessions.platform_fee`
-- on the per-call settlement path, and moving it here would make one shared
-- database a synchronous dependency of every region's call settlement.
CREATE TABLE IF NOT EXISTS credit_grants (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- A region slug. No FK — there is no regions table; the list is control's
    -- config. Control writes this itself, so it is never user input, and a slug
    -- naming a region that has been retired fails loudly on the push rather than
    -- silently at write time.
    region     TEXT NOT NULL,
    -- One value, and the CHECK is what keeps this table to rows a region can
    -- actually apply. A grant nothing can apply is a POISON PILL, not a stub for
    -- later: `pending_for` returns it on every billing-page load, the region
    -- refuses it, and the tenant's own page logs a traceback for ever while
    -- `applied_at` stays NULL.
    --
    -- So a new kind is added HERE and to `credit_ledger`'s indexes in the same
    -- change, never here alone. Promotional credit is the one on the horizon,
    -- and what it needs is a `grant_id` column on `credit_ledger` with a unique
    -- index on it — because idempotency is what the push relies on, and the
    -- existing indexes cover a signup grant, a purchase and a refund and nothing
    -- else. That is a data-plane migration, and it is not owed until there is a
    -- promotion to run.
    kind       TEXT NOT NULL CHECK (kind = 'signup_grant'),
    amount     NUMERIC(12,6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ
);
-- There is deliberately no `note` and no `created_by`. Both would be columns
-- only the second kind could ever fill, and while the CHECK admits one kind the
-- kind IS the explanation and nobody presses a button to issue it. They arrive
-- with the change that adds that kind, which is the same change that has to add
-- `credit_ledger.grant_id` anyway — a column nothing can write is not
-- groundwork, it is a reader wondering which code path fills it.
-- One signup grant per organization PER REGION — every region grants, so the
-- cost of a signup is `signup_grant_usd` times the number of regions. A region
-- added later does NOT retroactively grant to existing organizations; that would
-- be a deliberate backfill, not a rule.
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_grants_signup
    ON credit_grants (tenant_id, region) WHERE kind = 'signup_grant';
-- What the billing page's reconcile reads: one tenant, one region.
CREATE INDEX IF NOT EXISTS idx_credit_grants_pending
    ON credit_grants (tenant_id, region) WHERE applied_at IS NULL;
