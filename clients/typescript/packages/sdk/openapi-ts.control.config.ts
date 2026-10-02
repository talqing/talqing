import { defineConfig } from "@hey-api/openapi-ts";

// The CONTROL API's client, generated into the dashboard rather than into this
// package — because it is published nowhere. `openapi/control.json` is the
// control app's document, and its one consumer is `frontend/`: login, orgs,
// members, invites, tokens and the region list. Everything else a tenant can do
// is regional and comes from `@talqing/sdk`.
//
// **Why generated at all.** The dashboard needs all 17 endpoints, and
// hand-maintaining their request and response types in the frontend is exactly
// the drift `backend/services/user/tenants.py` exists to warn about — the same
// query pasted into seven places, two of which silently diverged.
//
// **Why the output path leaves this package.** The generated tree is
// self-contained — it emits its own `client/` and `core/` and imports nothing
// outside its own directory — so it compiles under the frontend's tsconfig with
// no dependency on `@talqing/sdk`. The toolchain lives here because this is the
// package that already has it; duplicating `@hey-api/openapi-ts` into
// `frontend/` would put a generator in the dependency tree Cloudflare installs
// on every deploy, for a file that is checked in.
//
// Every option below mirrors `openapi-ts.config.ts`; read the comments there
// for what each one is load-bearing for.
export default defineConfig({
  input: "../../../../openapi/control.json",
  output: {
    path: "../../../../frontend/lib/control/gen",
    // Extensionless, unlike the published SDK's config beside this one. That
    // package is ESM under `moduleResolution: NodeNext`, where an extensionless
    // relative import is a hard error. The dashboard is the opposite case: Next
    // bundles it with webpack, which does not map a `.js` specifier onto the
    // `.ts` file next to it — so the `.js` form typechecks (TypeScript's bundler
    // resolution does map it) and then fails to resolve at build time. Two
    // consumers, two correct answers.
    module: { extension: "" },
  },
  plugins: [
    {
      name: "@hey-api/typescript",
      definitions: { case: "preserve" },
      responses: { name: "{{name}}Results", response: "{{name}}Result" },
      errors: { name: "{{name}}Errors", error: "{{name}}Error" },
    },
    {
      name: "@hey-api/sdk",
      paramsStructure: "flat",
      responseStyle: "data",
      includeInEntry: (symbol) => symbol.name === "TalqingControlApi",
      operations: {
        strategy: "single",
        containerName: "TalqingControlApi",
        container: "class",
        methods: "instance",
      },
    },
  ],
});
