import { defineConfig } from "@hey-api/openapi-ts";

// The generated half of this SDK. `src/gen` is checked in and never edited by
// hand — regenerate with `npm run generate` after re-running openapi/export.py.
//
// Every option below is load-bearing; the comments say what breaks without it.
export default defineConfig({
  input: "../../../../openapi/openapi.json",
  output: {
    path: "./src/gen",
    // This package is ESM under `moduleResolution: NodeNext`, where an
    // extensionless relative import is a hard error. Without this the generated
    // tree does not compile at all.
    module: { extension: ".js" },
  },
  plugins: [
    {
      name: "@hey-api/typescript",
      // Our schema names are already the names we publish. The default
      // PascalCases them, which would rename `LLMSpec` to `LlmSpec` and
      // `STTSpec` to `SttSpec` for no benefit.
      definitions: { case: "preserve" },
      // Per-operation types default to `{{name}}Response`, which collides with
      // schemas we actually have of that name (`HealthResponse`) — and the
      // schema is what silently gets renamed. Moving operation types out of the
      // `…Response` namespace ends the collision class rather than patching it
      // case by case.
      responses: { name: "{{name}}Results", response: "{{name}}Result" },
      errors: { name: "{{name}}Errors", error: "{{name}}Error" },
    },
    {
      name: "@hey-api/sdk",
      // One flat object per call — `{ agent_id }`, not `{ path: { agent_id } }`.
      paramsStructure: "flat",
      // Methods resolve to the payload; failures throw (see TalqingApiError).
      responseStyle: "data",
      // Nested resources legitimately share leaf names (`calls.batches` and
      // `email.batches`, `agents.versions` and `tools.versions`), and the
      // generator de-duplicates the CLASS names as `Batches2`, `Versions2`.
      // Property access is unaffected, but those names must not be part of the
      // published surface, so only the root class is exported.
      includeInEntry: (symbol) => symbol.name === "TalqingApi",
      operations: {
        strategy: "single",
        containerName: "TalqingApi",
        container: "class",
        methods: "instance",
        // `nesting` is left at its default, 'operationId', which splits on dots.
        // That is why api/sdk_surface.py publishes dotted ids: a custom nesting
        // function here would work, but it makes the generator re-derive every
        // operation id from method+path, degrading `AgentsGetData` into
        // `GetV1AgentsByAgentIdData`.
      },
    },
  ],
});
