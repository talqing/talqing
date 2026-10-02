# documentation/

The Talqing docs site, published at `docs.talqing.com`.

Built with [Mintlify](https://mintlify.com): `docs.json` is the site config and
navigation, and every page is an `.mdx` file at the path the navigation names.
The per-endpoint API reference is generated from `openapi.json` and is not
checked in as pages.

## Preview it

```bash
npm i -g mint
cd documentation && mint dev --port 3333
```

Opens on `http://localhost:3333`. Editing a page reloads it. Pass the port —
`mint dev` defaults to 3000, which the dashboard's own `npm run dev` already
holds.

## Deploy it

Cloudflare Pages, built from this repo. `mint export` produces a plain static
Next.js bundle — HTML plus `_next/static` — so nothing needs a server or a
container.

| Pages setting | Value |
| --- | --- |
| Root directory | `documentation` |
| Build command | `bash build.sh` |
| Output directory | `dist` |

**The root directory has to be `documentation`, not the repo root.** Cloudflare
inspects the root directory for dependency manifests and installs what it finds
before running the build command. Pointed at the repo root it finds our
`pyproject.toml`, tries to pip-install the monorepo, and setuptools refuses it
("Multiple top-level packages discovered in a flat-layout") — a failure that
happens before the build command runs at all. `documentation/` has no manifest,
so nothing is auto-installed.

`build.sh` pins the CLI version, runs `check.py` first so a broken site cannot
ship, exports, generates `sitemap.xml` and `robots.txt`, and strips the tooling
files `mint export` copies out of this directory. It resolves its own location,
so `bash build.sh` from here and `bash documentation/build.sh` from the repo root
both work — run it locally to see exactly what will be published.

The custom domain is added in the Pages project's **Custom domains** tab.
Because `talqing.com` is a zone on the same Cloudflare account, Pages creates the
`docs` CNAME itself and shows you the record before it does — nothing to write by
hand. A domain on a zone Cloudflare does not hold would need the CNAME added at
whoever runs its DNS.

Three things to know before treating this as self-hosted:

- **The bundle is not fully standalone.** It still references `mintlify.com`
  hosts at runtime, including `ph.mintlify.com` for analytics. It renders and
  navigates without them, but it is not air-gapped despite what the CLI calls the
  command.
- **Search needs a Mintlify account.** `mint dev` says so, and the exported
  bundle carries the search UI without an index behind it. If search matters,
  either sign in for the build or accept that readers navigate by the sidebar.
- **The export ships no `sitemap.xml` or `robots.txt`.** `sitemap.py` generates
  both from the built bundle, so the API-reference pages Mintlify generates from
  the OpenAPI document are covered without reproducing its slug rules. Submit
  `https://docs.talqing.com/sitemap.xml` in Search Console under its own
  property — the single entry in the marketing site's sitemap is a crawl path,
  not a substitute.

Those are arguments for keeping the content as plain MDX, which it is: the pages
port to Docusaurus, Nextra or Astro Starlight without a rewrite if we ever want
off Mintlify.

## Check it before publishing

```bash
python documentation/check.py
```

Verifies that every page in the navigation exists and every page on disk is in
the navigation; that frontmatter carries a title and a description under 160
characters; that every internal link resolves — markdown links and JSX `href`
attributes alike, including cross-page `#anchors` against the target's real
headings; that no page uses a component Mintlify does not have; that code fences
are balanced; that no bare `{` sits in prose, which is how a `{{template}}` token
silently breaks an MDX build; and that `openapi.json` carries its servers. Exit
code 1 on any failure, so it works as a CI or pre-commit gate — `build.sh` runs
it before every deploy.

## Keep the API reference current

`openapi/openapi.json` is exported from the backend and deliberately carries no
`servers` block, because the generated SDKs must refuse to start without an
explicit base URL. The docs site needs one for its API playground, so it gets its
own copy:

```bash
PYTHONPATH=backend python openapi/export.py   # regenerate the spec
python documentation/sync-openapi.py          # copy it here, with servers
```

Run both after any change to a route, a request model or a response model.

## Writing

`STYLE.md` is the writing brief — voice, format, components, code-sample rules,
vocabulary and the accuracy rule. `SITEMAP.md` is the canonical path of every
page and the one thing it is for; a fact belongs to exactly one page, and every
other page links to it.

Both are internal and are not published — Mintlify builds only what `docs.json`
names.
