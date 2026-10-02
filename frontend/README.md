# talqing dashboard

Next.js 14 App Router, deployed to **Cloudflare Pages as a static export**.

Every page under `app/` is a client component and all data comes from the API at
runtime — there is no SSR, no server data fetching, and no API route. So
`next.config.js` sets `output: "export"` and the build artifact is plain static
files. No adapter, no Workers runtime, and no coupling to a Next.js-version
specific deploy tool.

## Local development

```bash
npm install
npm run dev          # http://localhost:3000
```

Needs the backend running (`docker compose up` at the repo root).

## Environment variables

Two, and both are **required for a production build**. Next inlines
`NEXT_PUBLIC_*` at build time, so a wrong or missing value cannot be detected at
runtime — it would ship a bundle whose every request fails in the user's browser
with nothing pointing at the cause. `lib/control` and `lib/regions.ts` therefore
throw during `next build` when either is unset.

| Variable | What it is |
|---|---|
| `NEXT_PUBLIC_CONTROL_API_URL` | The control plane: sign-in, organizations, members, tokens, and the region list. One host for the whole deployment. |
| `NEXT_PUBLIC_REGIONS` | A JSON array of regions, the same shape `GET /v1/regions` returns. |

**One build serves every region**, which is why the regional API base URL is not
an environment variable at all. The dashboard reads the live region list from the
control plane, caches it in `localStorage`, and builds its regional client from
whichever region this browser is looking at — so adding a region needs no
dashboard deploy. `NEXT_PUBLIC_REGIONS` is only the fallback, so that a
control-plane blip cannot blank the app on a cold browser.

Which region a browser is looking at is a preference **keyed per organization**
(`talqing.region.<orgId>`), falling back to that organization's `home_region` and
then to the region marked `default`. Never a global key: a member who works in
`us` and switches to an organization that has only ever used `in` would otherwise
land on an empty `us`, which is our own preference store manufacturing the
"looks like data loss" failure the region-named empty states exist to prevent.

**"development" here means `NODE_ENV`, not an environment we deploy to.** The
DigitalOcean dev environment is a *production build* — so `.env.development` is
your laptop only, and `.env.production` covers **both** deployed environments.

| File | Loaded by | Applies to | Committed |
|---|---|---|---|
| `.env.development` | `next dev` | your laptop → `http://localhost:8001` (control) and `http://localhost:8000` (the `in` region) | yes |
| `.env.production` | `next build` | dev **and** prod deploys — *deliberately sets nothing* | yes |
| `.env.local` | both, wins over the above | your personal override | **no** (gitignored) |

To run the local dev server against the deployed dev backend rather than a local
one, put both variables in `.env.local` with the `api-control.dev.talqing.com`
and `api-in.dev.talqing.com` hosts.

Committing the first two is safe: `NEXT_PUBLIC_*` values are inlined into the
client bundle and are public by definition. Never put a secret in one.

`.env.production` intentionally sets **neither** variable. Both deployed
environments are production builds, so any value here would apply to both — and
a dev dashboard silently pointing at the prod control plane is the worst
available outcome: real tenants, real calls, real spend, from an environment
nobody treats as live. Each Pages project supplies them instead, and a process
environment variable beats the file (`@next/env` only fills in variables that are
not already defined).

## Cloudflare Pages

Two projects against the same repository, differing only in the two URLs.

| Setting | dev | prod |
|---|---|---|
| Project name | `talqing-dashboard-dev` | `talqing-dashboard` |
| Production branch | `main` | `main` |
| Root directory | `frontend` | `frontend` |
| Build command | `npm run build` | `npm run build` |
| Build output directory | `out` | `out` |
| `NEXT_PUBLIC_CONTROL_API_URL` | `https://api-control.dev.talqing.com` | `https://api.control.talqing.com` |
| `NEXT_PUBLIC_REGIONS` | one region, `api-in.dev.talqing.com` | two regions, `api.in` and `api.us` |
| Custom domain | `app.dev.talqing.com` | `app.talqing.com` |

Dev's hostnames use a dash where prod uses a dot, matching dev's existing
`livekit-1.dev.talqing.com`. Deliberate; do not "fix" it.

Set both environment variables for **both** the Production and Preview scopes in
each project, or preview deployments will fail the build.

**`/cdn-cgi/trace` is why nothing has to be proxied.** The login page reads the
visitor's country off Cloudflare's edge from the Pages origin itself — it answers
on a plain static deployment, with no Pages Function and no proxied hostname —
and passes it to the control plane so a new workspace is created in the right
region. Nothing else depends on it, and login never waits for it.

**Root directory must be `frontend`.** The repo is a monorepo and
`@talqing/sdk` is a `file:../clients/typescript` dependency, so the
build has to run with the repository checked out, not just this directory.

**Custom domains, and the order that matters.** Both halves are needed and
neither implies the other: register the domain on the Pages project, *and*
create a proxied `CNAME` to `<project>.pages.dev`. Doing only the second gives
a 522 rather than a working site. Doing only the first leaves the domain
`pending` with `verification_data.error_message: "CNAME record not set"` —
**Pages did not create the record for you.** Measured while standing prod up on
2026-08-30 for `talqing.com` and `app.talqing.com`; this file previously claimed
the opposite.

Later the same day, adding `docs.talqing.com` to the `talqing-docs` project went
the other way: the flow showed the record it was about to write, and creating it
produced a proxied `CNAME docs → talqing-docs.pages.dev` with no DNS step of
ours. So the behaviour is not uniform — plausibly apex versus subdomain, since
an apex cannot hold a plain CNAME. **Check that the record exists rather than
assuming either way**, and create it by hand if it does not:

```
CNAME  <sub>  <project>.pages.dev  proxied
```

The Pages record is the one **proxied** record in the zone. Every origin
hostname — both APIs, the SFUs, Grafana, the SIP edge — is grey-cloud, because
proxying breaks media and randomises ACME issuance. A Pages custom domain is
served *by* the edge, so orange is correct there and only there.

## The one constraint static export imposes

A static export must enumerate every path at build time, and agent / call / tool
ids are unbounded — so there are **no dynamic route segments**. Detail pages take
the id as a query parameter:

```
/agents/detail?id=<uuid>
/calls/detail?id=<uuid>
/tools/detail?id=<uuid>
```

Each reads it with `useSearchParams()`, which suspends during prerender — so the
exported page component provides a `<Suspense>` boundary and delegates to an
inner component that does the work. Without that boundary `next build` fails
with *"useSearchParams() should be wrapped in a suspense boundary"*.

If you add a new detail page, follow the same shape. Adding an `app/x/[id]/`
directory will break the build.

## Public pages, and why they need guarding

Three routes are public and meant to be indexed — `/`, `/privacy`, `/terms`.
Everything else under `app/` is the authenticated dashboard. Both come out of
**one** static export served by **one** Pages project, so every dashboard route
also exists on the marketing domain, where it renders an empty shell, fails its
first API call on a CORS origin it is not allowed from, and dead-ends. Two files
keep that out of search results and they are the ones to update when routes
change:

| File | Job |
|---|---|
| `app/robots.ts` | Disallows every dashboard route by prefix. **Add a directory to `app/`, add it here.** |
| `app/sitemap.ts` | Lists only the three public URLs. |
| `app/site.ts` | `SITE_URL` — the canonical origin, hard-coded on purpose |

`SITE_URL` is not an environment variable. The same build is served on
`talqing.com`, `app.talqing.com`, `app.dev.talqing.com` and the project's
`.pages.dev` alias, all byte-identical; the canonical link is what tells a
crawler which one is real, so it has to be the same value on all four. Making it
per-build would produce four pages each claiming to be canonical.

`metadataBase` in `app/layout.tsx` is what turns `alternates.canonical:
"/privacy"` into an absolute URL. Public pages set their own canonical; dashboard
pages are client components, cannot export metadata, and are disallowed anyway.

**Keep the root `metadata` free of claims that must stay true** — prices, limits,
counts. It is inherited by every page that does not set its own, and a page can
override a description but nothing overrides a wrong number that got indexed.

### Marketing chrome

`app/marketing-chrome.tsx` holds the header, footer, brand theme and fonts shared
by the landing page and the two legal pages; `app/legal-page.tsx` holds the prose
shell those two use. The legal pages are content files by design — someone
non-technical has to amend them after legal review.

`LEGAL_ENTITY` in `marketing-chrome.tsx` is the operating company — name,
address, governing law, forum — and it feeds the footer, both legal pages and the
governing-law clause from one place. Those five must never disagree: Meta's
business verification compares the name and address on the site against the
documents you upload. `address` is the city today; widen it to the full
registered office when you have Meta's check in front of you.

Two link rules hold across all three public pages, and both exist because the
same build is served on more than one hostname:

- **Nothing links into the dashboard.** `loginHref` (`/login`) is the only
  destination for every call to action. A direct `/agents` link resolves on the
  marketing domain, loads the shell, and then fails its first API call on a CORS
  origin it is not allowed from — a dead end rather than a sign-in. `/login`
  works everywhere, and the callback lands the user on whichever dashboard
  `app.dashboard_public_url` names.
- **Anchors are rooted at `/`** (`/#product`, not `#product`), because the header
  now renders on pages that are not the landing page.

## Conventions

**Colors come from tokens, never from literals.** `tailwind.config.ts` defines the
whole palette — a surface set (`canvas`/`surface`/`subtle`/`hover`), a six-step
text ramp (`ink` → `ink-soft` → `muted` → `faint` → `placeholder`), three border
weights (`line`/`line-2`/`line-strong`), and four status hues. Write `text-muted`,
not `text-[#787881]`; a status wash is an opacity modifier (`bg-live/[0.06]`,
`border-danger/25`), never a separate tint. The landing page (`app/page.tsx`) and
`app/login/page.tsx` carry a separate brand theme and are the only exceptions.

**Errors go through `lib/apiError.ts`.** `apiErrorMessage(error, fallback)` turns
anything thrown into something a user can read; `apiErrorList(error)` pulls out
the API's field-level validation errors. No page handles 401 — `lib/api.ts` gives
the SDK an `onUnauthorized` callback that sends the browser to `/login` once,
centrally.

**Lint runs as part of the build.** `npm run lint` (or `next build`, which lints
first) must be clean. The config is `next/core-web-vitals` with one rule off:
`@next/next/no-img-element`, because a static export has no image optimizer, so
`<img>` is the correct element here and `next/image` is not available. When a
`react-hooks/exhaustive-deps` warning is deliberate — a mount-once load, or a
dependency that would tear down an SSE stream on every render — silence it at
that line with a comment saying why, rather than leaving a standing warning.

**A failed action must never unmount an editor.** Keep load failures (page has
nothing to show) and action failures (save/publish) in separate state; the second
renders as a dismissible banner so unsaved work survives. `useUnsavedChanges`
guards both editors against navigating away from a dirty draft.

## Verifying a production build locally

```bash
NEXT_PUBLIC_CONTROL_API_URL=https://api-control.dev.talqing.com \
NEXT_PUBLIC_REGIONS='[{"slug":"in","name":"India (Bangalore)","api_url":"https://api-in.dev.talqing.com","default":true}]' \
  npm run build
npx serve out
```

`npm run build` alone will fail unless `.env.local` supplies both variables —
that is the guard working, not a broken build.

**Do not run `npm run build` while `npm run dev` is running**; it overwrites the
`.next` directory the dev server is serving from. Use `npx tsc --noEmit` for a
type check.
