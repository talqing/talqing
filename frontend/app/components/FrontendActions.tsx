"use client";
import { Panel, Badge, Button } from "./ui";
import type { JsonValue } from "@talqing/react";

// Frontend RPCs. The agent's `frontend_rpc` operation RPCs ONE
// envelope method — `talqing.frontend_rpc` with {method, payload} — and the SDK
// dispatches by envelope.method. `useTalqingFrontendRpcs` is the declarative
// half of that: it registers the wildcard handler and keeps the latest payload
// per method, which is exactly what these cards render.
//
// It is imported rather than reimplemented because there is now one copy of
// `@livekit/components-react` in the tree — see the note in WebCall.tsx.
export type FrontendAction = { payload?: JsonValue; at: number };

export function ActionCards({
  actions,
  onClear,
}: {
  actions: Record<string, FrontendAction>;
  onClear: () => void;
}) {
  const entries = Object.entries(actions);
  if (!entries.length) return null;
  return (
    <Panel data-testid="frontend-actions">
      <div className="mb-2 flex items-center gap-2.5">
        <strong className="font-semibold text-ink">Frontend RPCs</strong>
        <span className="text-[12px] text-muted">live UI calls from the agent</span>
        <div className="flex-1" />
        <Button variant="secondary" size="sm" onClick={onClear}>
          Clear
        </Button>
      </div>
      <div className="flex flex-wrap items-stretch gap-2.5">
        {entries.map(([method, a]) => (
          <div key={method} className="min-w-[220px] flex-1 rounded-lg border border-line bg-subtle p-3">
            <div className="mb-1.5 flex items-center gap-2">
              <Badge>{method}</Badge>
              <span className="text-[11px] text-muted">{new Date(a.at).toLocaleTimeString()}</span>
            </div>
            <pre className="m-0 whitespace-pre-wrap break-words font-mono text-[12px] text-ink-soft">
              {typeof a.payload === "string" ? a.payload : JSON.stringify(a.payload, null, 2)}
            </pre>
          </div>
        ))}
      </div>
    </Panel>
  );
}
