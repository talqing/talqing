"use client";

import type { IfConfig, JsonValue, RunStepResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { walkTree } from "./definition";
import { KIND_LABEL } from "./operationMetadata";
import type { IfDraft, OperationDraft } from "./operationTypes";
import { isDataOperation } from "./operationTypes";

type OperationArgument = {
  name: string;
  type: string;
  required?: boolean;
};

/* Steps from the last test run, keyed by the path the backend assigned each
   operation: the node's index, joined to its parent by `.then.`/`.else.`. This
   file builds the same string as it walks, so the two must change together —
   `compiler/operations.py::TraceStep` is the other half. Null when no run has
   happened, or when the draft has moved on since one did. */
type RunSteps = Record<string, RunStepResponse> | null;

type OperationFlowchartProps = {
  operations: OperationDraft[];
  parameters: OperationArgument[];
  agentNameById: Record<string, string>;
  runSteps?: RunSteps;
  className?: string;
};

const STATUS_DOT: Record<string, string> = {
  ok: "bg-live",
  failed: "bg-danger",
  simulated: "bg-muted",
};

export type OperationSummary = {
  primary?: string;
  secondary?: string;
  warning?: boolean;
};

/* Only the untyped corners are left: an `if`'s two sides and a `set_variable`'s
   value are `JsonValue` by design, because an author may compare or store
   anything. Everywhere else the kind narrows the config and the field is already
   a string. */
function displayValue(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value == null) return null;
  return JSON.stringify(value);
}

function countSourceLines(source: string): number {
  return source.trim() ? source.split(/\r\n|\r|\n/).length : 0;
}

function publishKey(path: string, key: string): string {
  const trimmedKey = key.trim();
  if (trimmedKey) return trimmedKey;
  const parts = path.trim().split(".").map((part) => part.trim()).filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

/* The store is the namespace, so `tooldata.x` already says how long x lives —
   no parenthetical scope word needed. */
function publishedVariables(op: OperationDraft): string[] {
  if (!isDataOperation(op)) return [];
  return op.publish_fields
    .map((field) => ({ key: publishKey(field.path, field.key), store: field.store }))
    .filter(({ key }) => key.length > 0)
    .map(({ key, store }) => `${store}.${key}`);
}

function conditionLabel(config: IfConfig): string | null {
  const left = displayValue(config.left);
  const op = displayValue(config.op);
  const right = displayValue(config.right);
  if (!left) return null;
  const operator = op === "eq" ? "==" : op === "neq" ? "!=" : op;
  if (operator === "exists") return `if ${left} exists`;
  return `if ${left} ${operator ?? "=="} ${right ?? "..."}`;
}

/** The one-line reading of an operation, shared with the version diff so the
    same step is described the same way wherever it appears. */
export function operationSummary(
  op: OperationDraft,
  agentNameById: Record<string, string>,
): OperationSummary {
  const saved = publishedVariables(op);
  const secondary = saved.length ? `saves ${saved.join(", ")}` : undefined;

  switch (op.kind) {
    case "http": {
      const url = op.config.url.trim();
      return {
        primary: url ? `${op.config.method} ${url}` : `${op.config.method} URL missing`,
        secondary,
        warning: !url,
      };
    }
    case "code": {
      const lines = countSourceLines(op.config.source_ts);
      return { primary: `${lines} TypeScript line${lines === 1 ? "" : "s"}`, secondary };
    }
    case "set_variable": {
      const key = op.config.key.trim();
      const value = displayValue(op.config.value);
      if (!key) return { primary: "Variable missing", warning: true };
      return { primary: `${op.config.store}.${key}${value ? ` = ${value}` : ""}` };
    }
    case "say": {
      const text = op.config.text.trim();
      return { primary: text || "Text missing", warning: !text };
    }
    case "generate_reply": {
      const instructions = op.config.instructions.trim();
      return { primary: instructions || "Reply instructions missing", warning: !instructions };
    }
    case "add_message": {
      const text = op.config.text.trim();
      return { primary: text || "Message missing", warning: !text };
    }
    case "end_call":
      return {};
    case "handoff": {
      const target = (op.config.target_agent_id ?? "").trim();
      const context = op.config.context ?? "transcript";
      const member = (op.config.agent_name ?? "").trim();
      const agentName = member
        ? `team member “${member}”`
        : target ? (agentNameById[target] ?? target) : null;
      return {
        primary: agentName
          ? `to ${agentName}${context !== "transcript" ? ` (${context})` : ""}`
          : "Target missing",
        warning: !agentName,
      };
    }
    case "transfer": {
      const destination = op.config.destination.trim();
      if (!destination) return { primary: "Destination missing", warning: true };
      /* Only the non-default choices are worth a line on the node: cold and
         "carry on with the agent" are what a reader would have assumed, and
         "briefs them first" is the one that changes what the caller lives
         through. */
      const notes = [
        op.config.mode === "warm" ? "briefs them first" : undefined,
        op.config.on_failure === "end_call" ? "ends the call if it fails" : undefined,
      ].filter(Boolean);
      return { primary: `to ${destination}`, secondary: notes.length ? notes.join(" · ") : undefined };
    }
    case "frontend_rpc": {
      const method = op.config.method.trim();
      return { primary: method || "Method missing", secondary, warning: !method };
    }
    case "send_dtmf": {
      const digits = op.config.digits.trim();
      if (!digits) return { primary: "Digits missing", warning: true };
      /* `w` and `W` are pauses, not keys, and a reader scanning the flowchart
         should see what the caller's line will actually do rather than the raw
         string. */
      const pauses = [...digits].filter((key) => key === "w" || key === "W").length;
      return {
        primary: `press ${digits}`,
        secondary: pauses ? `${pauses} pause${pauses === 1 ? "" : "s"}` : undefined,
      };
    }
    case "if":
      return { primary: conditionLabel(op.config) ?? undefined };
  }
}

export function OperationFlowchart({
  operations,
  parameters,
  agentNameById,
  runSteps = null,
  className,
}: OperationFlowchartProps): JSX.Element {
  const nodes = [...walkTree(operations)];
  const totalOperations = nodes.length;
  const branched = nodes.some((node) => node.kind === "if");

  return (
    <aside className={cn("min-w-0 lg:h-full", className)}>
      <section className="flex min-h-[360px] flex-col overflow-hidden rounded-xl border border-line-2 bg-white lg:h-full lg:min-h-0">
        <div className="border-b border-line px-4 py-3">
          <div className="flex items-center gap-2">
            <h2 className="min-w-0 flex-1 text-[14px] font-semibold leading-5 text-ink">
              Operation tree
            </h2>
            <span className="rounded-full border border-line-2 bg-subtle px-2 py-[3px] text-[12px] font-medium leading-4 text-ink-soft">
              {totalOperations} {totalOperations === 1 ? "op" : "ops"}
            </span>
            {runSteps && (
              <span className="rounded-full border border-line-2 bg-subtle px-2 py-[3px] text-[12px] font-medium leading-4 text-muted">
                last run
              </span>
            )}
          </div>
        </div>
        <div className="scroll-thin min-h-[320px] flex-1 overflow-auto bg-[radial-gradient(circle_at_1px_1px,rgba(161,164,171,0.5)_1px,transparent_0)] p-4 [background-size:22px_22px] lg:min-h-0">
          <div className={cn("mx-auto flex flex-col items-center", branched ? "min-w-[560px]" : "min-w-[240px]")}>
            <StartNode parameters={parameters} />
            {operations.length ? (
              <>
                <Connector />
                <FlowChain nodes={operations} agentNameById={agentNameById} runSteps={runSteps} pathPrefix="" />
              </>
            ) : (
              <>
                <Connector />
                <EmptyNode label="No operations yet" />
              </>
            )}
          </div>
        </div>
      </section>
    </aside>
  );
}

function FlowChain({
  nodes,
  agentNameById,
  runSteps,
  pathPrefix,
}: {
  nodes: OperationDraft[];
  agentNameById: Record<string, string>;
  runSteps: RunSteps;
  pathPrefix: string;
}): JSX.Element {
  return (
    <div className="flex w-full flex-col items-center">
      {nodes.map((op, index) => (
        <div key={op._id} className="flex w-full flex-col items-center">
          <OperationStep
            op={op}
            hasNext={index < nodes.length - 1}
            agentNameById={agentNameById}
            runSteps={runSteps}
            path={`${pathPrefix}${index}`}
          />
        </div>
      ))}
    </div>
  );
}

function OperationStep({
  op,
  hasNext,
  agentNameById,
  runSteps,
  path,
}: {
  op: OperationDraft;
  hasNext: boolean;
  agentNameById: Record<string, string>;
  runSteps: RunSteps;
  path: string;
}): JSX.Element {
  const condition = op.kind === "if" ? conditionLabel(op.config) : null;
  const step = runSteps?.[path] ?? null;
  /* A run happened and this node was not in it: an untaken branch, or a step
     after the one that aborted. Dimming is the whole point — it draws the path
     the run actually took through the tree. */
  const skipped = Boolean(runSteps) && !step;

  if (op.kind === "if") {
    const branch = step?.detail?.branch;
    return (
      <>
        <ConditionPill
          label={condition ?? "if condition missing"}
          warning={!condition}
          skipped={skipped}
        />
        <BranchSplit
          op={op}
          agentNameById={agentNameById}
          runSteps={runSteps}
          path={path}
          taken={branch === "then" || branch === "else" ? branch : null}
        />
      </>
    );
  }

  return (
    <>
      <OperationCard op={op} agentNameById={agentNameById} step={step} skipped={skipped} />
      {hasNext && <Connector />}
    </>
  );
}

function BranchSplit({
  op,
  agentNameById,
  runSteps,
  path,
  taken,
}: {
  op: IfDraft;
  agentNameById: Record<string, string>;
  runSteps: RunSteps;
  path: string;
  taken: "then" | "else" | null;
}): JSX.Element {
  return (
    <div className="flex w-full flex-col items-center pt-1">
      <div className="h-5 w-px bg-line-strong" aria-hidden />
      <div className="relative grid w-full grid-cols-2 gap-4 pt-5">
        <div className="absolute left-1/4 right-1/4 top-0 h-px bg-line-strong" aria-hidden />
        <div className="absolute left-1/4 top-0 h-5 w-px bg-line-strong" aria-hidden />
        <div className="absolute right-1/4 top-0 h-5 w-px bg-line-strong" aria-hidden />
        <BranchLane
          label="then"
          nodes={op.then}
          agentNameById={agentNameById}
          runSteps={runSteps}
          pathPrefix={`${path}.then.`}
          taken={taken}
        />
        <BranchLane
          label="else"
          nodes={op.else}
          agentNameById={agentNameById}
          runSteps={runSteps}
          pathPrefix={`${path}.else.`}
          taken={taken}
        />
      </div>
    </div>
  );
}

function BranchLane({
  label,
  nodes,
  agentNameById,
  runSteps,
  pathPrefix,
  taken,
}: {
  label: "then" | "else";
  nodes: OperationDraft[];
  agentNameById: Record<string, string>;
  runSteps: RunSteps;
  pathPrefix: string;
  taken: "then" | "else" | null;
}): JSX.Element {
  const wasTaken = taken === label;
  return (
    <div className={cn("flex min-w-0 flex-col items-center", taken && !wasTaken && "opacity-40")}>
      <div
        className={cn(
          "mb-2 rounded-full border px-2.5 py-1 text-[10.5px] font-semibold uppercase leading-4",
          wasTaken
            ? "border-live/40 bg-live/[0.08] text-live"
            : "border-line-strong bg-white text-muted",
        )}
      >
        {label}
        {wasTaken && " ✓"}
      </div>
      {nodes.length ? (
        <FlowChain
          nodes={nodes}
          agentNameById={agentNameById}
          runSteps={runSteps}
          pathPrefix={pathPrefix}
        />
      ) : (
        <EmptyNode label="Empty branch" />
      )}
    </div>
  );
}

function StartNode({ parameters }: { parameters: OperationArgument[] }): JSX.Element {
  const visibleParameters = parameters.filter((parameter) => parameter.name.trim());

  return (
    <div className="w-full max-w-[320px] rounded-xl border border-line-2 bg-white px-3.5 py-3 shadow-[0_1px_2px_rgba(12,13,15,0.04)]">
      <div className="flex items-center gap-2 text-[13px] font-semibold leading-5 text-ink">
        <OperationIcon kind="start" className="h-4 w-4" />
        Start
      </div>
      {visibleParameters.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {visibleParameters.map((parameter) => (
            <span
              key={parameter.name}
              className="inline-flex max-w-full items-center gap-1 rounded-md border border-line-2 bg-subtle px-1.5 py-0.5 text-[11px] leading-4 text-ink-soft"
              title={`${parameter.name}: ${parameter.type}${parameter.required ? " required" : ""}`}
            >
              <span className="min-w-0 truncate font-mono text-ink">{parameter.name}</span>
              <span className="flex-none text-muted">: {parameter.type}</span>
              {parameter.required && <span className="flex-none text-warn">*</span>}
            </span>
          ))}
        </div>
      ) : (
        <div className="mt-1.5 text-[12px] leading-4 text-muted">No arguments</div>
      )}
    </div>
  );
}

function EmptyNode({ label }: { label: string }): JSX.Element {
  return (
    <div className="w-full max-w-[260px] rounded-xl border border-dashed border-line-strong bg-white/85 px-3.5 py-3 text-center text-[12.5px] leading-5 text-muted">
      {label}
    </div>
  );
}

function Connector(): JSX.Element {
  return (
    <div className="flex h-8 flex-col items-center" aria-hidden>
      <div className="w-px flex-1 bg-line-strong" />
      <svg className="-mt-[6px] h-3 w-3 text-line-strong" viewBox="0 0 12 12" fill="none">
        <path d="m2.5 4.5 3.5 3.5 3.5-3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function ConditionPill({
  label,
  warning,
  skipped,
}: {
  label: string;
  warning?: boolean;
  skipped?: boolean;
}): JSX.Element {
  return (
    <div
      className={cn(
        "inline-flex max-w-[260px] items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-semibold leading-4 text-white shadow-[0_1px_2px_rgba(12,13,15,0.18)]",
        warning ? "bg-warn" : "bg-ink",
        skipped && "opacity-40",
      )}
    >
      <svg className="h-3.5 w-3.5 flex-none" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="m4 2 3 3 3-3" />
        <path d="m4 7 3 3 3-3" />
      </svg>
      <span className="min-w-0 truncate">{label}</span>
    </div>
  );
}

/* The handset, shared by `transfer` and `end_call` so the two read as the same
   object doing two different things. */
const HANDSET =
  "M13.8 16.6a1 1 0 0 0 1.2-.3l.4-.5a2 2 0 0 1 1.6-.8h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.5.4a1 1 0 0 0-.3 1.2 14 14 0 0 0 6.4 6.4";

export function OperationIcon({
  kind,
  className,
}: {
  kind: string;
  className?: string;
}): JSX.Element {
  const common = {
    className: cn("flex-none", className),
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "1.8",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (kind === "start") {
    return (
      <svg {...common}>
        <path d="M5 21V5" />
        <path d="M5 5c4-2 6 2 10 0 1-.5 2-.9 4-.7v9c-2-.2-3.1.2-4 .7-4 2-6-2-10 0" />
      </svg>
    );
  }
  if (kind === "http") {
    /* A request out and a response back, which is what the operation is. The
       link glyph this replaced said "a URL is involved" and nothing else. */
    return (
      <svg {...common}>
        <path d="M3 9h14" />
        <path d="m14 6 3 3-3 3" />
        <path d="M21 15H7" />
        <path d="m10 12-3 3 3 3" />
      </svg>
    );
  }
  if (kind === "code") {
    return (
      <svg {...common}>
        <path d="m8 9-3 3 3 3" />
        <path d="m16 9 3 3-3 3" />
        <path d="m14 5-4 14" />
      </svg>
    );
  }
  if (kind === "set_variable") {
    /* A tag: a name tied to a value. The database cylinder this replaced
       promised a store the author never sees. */
    return (
      <svg {...common}>
        <path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4Z" />
        <circle cx="7.5" cy="7.5" r="1.4" />
      </svg>
    );
  }
  /* The three speech kinds get three silhouettes, not one bubble with three
     things inside it — at 20px those were indistinguishable, and they are three
     genuinely different behaviours: `say` puts audio out, `generate_reply` has
     the model write the line, `add_message` writes to the context and stays
     silent, which is why it is the one with no bubble. */
  if (kind === "say") {
    return (
      <svg {...common}>
        <path d="M11 5 6 9H3v6h3l5 4Z" />
        <path d="M15.5 8.5a5 5 0 0 1 0 7" />
        <path d="M18.7 5.3a9 9 0 0 1 0 13.4" />
      </svg>
    );
  }
  if (kind === "generate_reply") {
    return (
      <svg {...common}>
        <path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6a8 8 0 1 1 18-5Z" />
        <path d="m12 7.5 1.35 3.15L16.5 12l-3.15 1.35L12 16.5l-1.35-3.15L7.5 12l3.15-1.35Z" />
      </svg>
    );
  }
  if (kind === "add_message") {
    return (
      <svg {...common}>
        <path d="M4 5h16" />
        <path d="M4 10h16" />
        <path d="M4 15h6" />
        <path d="M16.5 13.5v7" />
        <path d="M13 17h7" />
      </svg>
    );
  }
  if (kind === "if") {
    /* The flowchart diamond, which is what a condition has looked like since
       long before this editor. */
    return (
      <svg {...common}>
        <path d="M12 2.5 21.5 12 12 21.5 2.5 12Z" />
      </svg>
    );
  }
  if (kind === "handoff") {
    return (
      <svg {...common}>
        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
        <circle cx="9" cy="7" r="4" />
        <path d="M17 8h5" />
        <path d="m19 5 3 3-3 3" />
      </svg>
    );
  }
  if (kind === "transfer") {
    /* A handset with the call moving on, so it reads next to `handoff` (two
       people) and `end_call` (a struck-through handset) without being either. */
    return (
      <svg {...common}>
        <path d={HANDSET} />
        <path d="M14 6h8" />
        <path d="m18 2 4 4-4 4" />
      </svg>
    );
  }
  if (kind === "send_dtmf") {
    /* A dial pad: nine keys. Not a handset — this operation does not place or
       end a call, it presses buttons on one that is already up, and every other
       handset glyph on this list means something about the call itself. */
    return (
      <svg {...common} strokeWidth="1.6">
        <circle cx="7" cy="6" r="1.1" />
        <circle cx="12" cy="6" r="1.1" />
        <circle cx="17" cy="6" r="1.1" />
        <circle cx="7" cy="12" r="1.1" />
        <circle cx="12" cy="12" r="1.1" />
        <circle cx="17" cy="12" r="1.1" />
        <circle cx="7" cy="18" r="1.1" />
        <circle cx="12" cy="18" r="1.1" />
        <circle cx="17" cy="18" r="1.1" />
      </svg>
    );
  }
  if (kind === "frontend_rpc") {
    return (
      <svg {...common}>
        <rect x="3" y="4" width="18" height="12" rx="2" />
        <path d="M8 20h8" />
        <path d="M12 16v4" />
      </svg>
    );
  }
  if (kind === "end_call") {
    return (
      <svg {...common}>
        <path d={HANDSET} />
        {/* Top-right to bottom-left. The other diagonal runs along the handset's
            own curve and vanished into it. */}
        <path d="M21 3 3 21" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 8v4l2.5 2.5" />
    </svg>
  );
}

function OperationCard({
  op,
  agentNameById,
  step,
  skipped,
}: {
  op: OperationDraft;
  agentNameById: Record<string, string>;
  step: RunStepResponse | null;
  skipped: boolean;
}): JSX.Element {
  const summary = operationSummary(op, agentNameById);
  const failed = step?.status === "failed";

  return (
    <article
      className={cn(
        "w-full max-w-[280px] rounded-xl border bg-white px-3.5 py-3 text-left shadow-[0_1px_2px_rgba(12,13,15,0.04)]",
        failed || summary.warning ? "border-danger/75" : "border-line-2",
        skipped && "opacity-40",
      )}
    >
      <div className="flex items-center gap-2.5">
        <span className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-white text-ink">
          <OperationIcon kind={op.kind} className="h-5 w-5" />
        </span>
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-semibold leading-5 text-ink">
          {KIND_LABEL[op.kind] ?? op.kind}
        </h3>
        {step && (
          <span
            className="flex flex-none items-center gap-1.5"
            title={
              step.error ??
              (step.status === "simulated"
                ? "Recorded, not performed — this needs a live conversation"
                : `${step.status} · ${step.duration_ms}ms`)
            }
          >
            <span className="text-[11px] tabular-nums text-faint">{step.duration_ms}ms</span>
            <span
              className={cn("h-1.5 w-1.5 rounded-full", STATUS_DOT[step.status] ?? "bg-muted")}
              aria-hidden
            />
          </span>
        )}
      </div>
      {summary.primary && (
        <p
          className={cn(
            "mt-2 truncate text-[12.5px] leading-5 text-muted",
            summary.warning && "text-warn",
          )}
          title={summary.primary}
        >
          {summary.primary}
        </p>
      )}
      {summary.secondary && (
        <p className="mt-0.5 truncate text-[11.5px] leading-4 text-placeholder" title={summary.secondary}>
          {summary.secondary}
        </p>
      )}
    </article>
  );
}
