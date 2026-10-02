# Contributing to Talqing

Thanks for helping. Bug reports, fixes and features are all welcome.

For anything larger than a small fix, open an issue first so the design can be agreed before you write it. Security problems go through [`SECURITY.md`](SECURITY.md), not an issue.

## Setup

Follow the [self-hosting quickstart](README.md#quickstart-self-hosting) to get the stack running locally. Then:

```bash
# Backend tooling (Python 3.12, uv)
cd backend && uv sync --all-groups

# Git hooks: secret scan, ruff, mypy, next lint, SDK build and typechecks
pip install pre-commit && pre-commit install
```

Backend code is mounted into the containers, so after editing it run `docker compose restart <service>`. The dashboard hot-reloads under `npm run dev`; do not run `npm run build` in `frontend/` alongside it.

## Before you open a pull request

The pre-commit hooks run these for you. To run them by hand:

```bash
cd backend && uv run ruff check . && uv run ruff format . && .venv/bin/mypy
cd frontend && npx tsc --noEmit && npx next lint
cd clients/typescript && npm run build && npm run typecheck
```

There is no backend unit-test suite. Verify a change by running it: a text agent covers most behaviour, and a web call from the dashboard covers voice.

## Changing the API

The TypeScript SDK, the Python SDK, the MCP tool list and the docs API reference are all generated from the backend's OpenAPI document. If you change a route, a request or a response model, regenerate them in the same pull request:

```bash
PYTHONPATH=backend backend/.venv/bin/python openapi/export.py
python documentation/sync-openapi.py
python clients/python/generate.py
cd clients/typescript && npm run generate
```

Never edit anything under a `gen/` directory by hand. Keep route docstrings and field descriptions to one or two lines: each becomes an MCP tool description that every connected coding agent pays for in tokens.

## Database changes

Schema lives in `backend/migrations/`. Add a new numbered `.sql` file; do not edit one that has shipped. Migrations carry schema only, and must succeed on a database that already holds rows.

Every query against a tenant table must filter by `tenant_id`.

## Pull requests

- Branch from `main`, and keep a pull request to one change.
- Say what the change does and how you verified it.
- Match the code around you: its naming, its comment density and its idiom.

## Licensing of contributions

Talqing is licensed under the [Sustainable Use License](LICENSE.md); the SDKs and the MCP server are MIT. By submitting a contribution you agree that it is licensed under the license of the part of the repository it changes. You also grant Oggnai Technologies Private Limited the right to license your contribution under other terms, which is what lets the project offer a commercial license alongside the public one.
