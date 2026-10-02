"use client";
import type { CallToolResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { Badge, CopyButton } from "../ui";
import { fmtMs } from "../LatencyStageBar";
import { data, slowTool, type ToolBeat as ToolBeatData, type ToolVerdict } from "./exchanges";

export function pretty(v: unknown): string {
  if (v == null || v === "") return "";
  if (typeof v === "string") {
    try {
      return JSON.stringify(JSON.parse(v), null, 2);
    } catch {
      return v;
    }
  }
  return JSON.stringify(v, null, 2);
}

const VERDICT_LABEL: Record<ToolVerdict, string> = {
  ok: "ok",
  failed: "failed",
  cancelled: "cancelled",
  silent: "returned nothing",
  unfinished: "no result recorded",
};

/* `silent` is not a warning. It is the ordinary shape of a tool whose whole job
   is a side effect — `end_call` is one — so it wears the neutral dot every
   uneventful row on this page wears. */
const VERDICT_DOT: Record<ToolVerdict, string> = {
  ok: "bg-live",
  failed: "bg-danger",
  cancelled: "bg-warn",
  silent: "bg-line-strong",
  unfinished: "bg-warn",
};

/**
 * Required arguments the model left out or sent empty.
 *
 * Worth its own check because it is invisible otherwise: the arguments render
 * as valid JSON either way, and a tool that "ran fine" having been handed an
 * empty required field is the single most common reason a call is judged a
 * failure. `json_schema` has been on the wire all along.
 */
function missingRequired(schema: CallToolResponse["json_schema"] | null, args: unknown): string[] {
  const required = schema?.required;
  if (!Array.isArray(required) || typeof args !== "object" || args === null) return [];
  const bag = args as Record<string, unknown>;
  return required.filter((name): name is string => {
    if (typeof name !== "string") return false;
    const value = bag[name];
    return value === undefined || value === null || value === "";
  });
}

function parseArgs(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function ToolBeat({
  beat,
  tool,
  slowMs,
}: {
  beat: ToolBeatData;
  /* The frozen definition this call ran against. Null when the tool is not
     pinned to the version; undefined where the page has no definitions at all. */
  tool: CallToolResponse | null | undefined;
  slowMs: number;
}) {
  const { call, output, trace, verdict } = beat;
  const name = String(data(call).name ?? "tool");
  const args = pretty(data(call).arguments);
  const failed = verdict === "failed";
  /* Two cases where the persisted output row is not what happened, and the
     trace is — sometimes the ONLY true account:

     - a failure, because LiveKit writes the output row for the model to read,
       which for anything that is not a `ToolError` is the fixed string "An
       internal error occurred";
     - a background tool, whose output row is the dispatch line the model got
       while the work was still running. The real result lands one turn later as
       a synthetic `<call_id>_final` pair that our transcript persistence never
       sees, so `tool.ended.message` is the only place it exists.

     `tool.ended.message` is `str(result)` or `str(exception)` in every case. For
     a `ToolError` the two are the same string and preferring the trace changes
     nothing. */
  const preferTrace = failed || trace?.background === true;
  const result = (() => {
    /* No fallback to the output row on a background tool: that row is the
       dispatch line and never the result, so a run with nothing to report —
       cancelled, or a silent tree — should show its outcome note instead. */
    if (trace?.background) return trace.message;
    if (failed) return trace?.message ?? (output ? pretty(data(output).output) : null);
    return output ? pretty(data(output).output) : null;
  })();
  /* What the model was handed, when that is not what happened. This is the
     line that explains why the agent then said something odd — it was told
     "An internal error occurred", or told the work was under way. */
  const toldTheModel = (() => {
    if (!preferTrace || !output) return null;
    const shown = pretty(data(output).output);
    return shown && shown !== result ? shown : null;
  })();
  const missing = missingRequired(tool?.json_schema ?? null, parseArgs(data(call).arguments));
  /* How long the caller waited on this tool. Deliberately NOT called silence:
     a tool tree can contain `say` and `generate_reply` operations, and with
     `wait_for_playback` the agent's own speech is inside this number — so the
     long ones are sometimes the agent talking, not dead air. The health band
     above is the one that separates them, because only it can compare this
     window against the agent's speech windows. */
  const slow = slowTool(beat, slowMs);

  return (
    <details
      id={beat.id}
      className={cn(
        "group scroll-mt-20 overflow-hidden rounded-lg border bg-subtle",
        failed || missing.length > 0
          ? "border-danger/30"
          : slow
            ? "border-warn/35"
            : "border-line-2",
      )}
    >
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-3 py-2 text-[13px] transition-colors hover:bg-hover [&::-webkit-details-marker]:hidden">
        <span className={cn("h-1.5 w-1.5 flex-none rounded-full", VERDICT_DOT[verdict])} aria-hidden />
        <span className="font-mono font-medium text-ink">{name}</span>
        <span className="truncate text-[12px] text-faint">{VERDICT_LABEL[verdict]}</span>
        {trace?.durationMs != null && (
          <span
            className={cn(
              "font-mono text-[12px] tabular-nums",
              slow ? "font-medium text-warn" : "text-faint",
            )}
            title={
              trace.background
                ? "How long the work took. The agent handed the turn back as soon as this tool started, so the caller waited for none of it"
                : slow
                  ? "The caller waited this long for this tool"
                  : undefined
            }
          >
            {fmtMs(trace.durationMs)}
          </span>
        )}
        {missing.length > 0 && (
          <Badge variant="danger">
            {missing.length} required {missing.length === 1 ? "field" : "fields"} empty
          </Badge>
        )}
      </summary>
      <div className="grid gap-3 border-t border-line px-3 py-3 sm:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-1.5 flex items-center justify-between gap-2 text-[11px] font-medium text-muted">
            Arguments
            {args && <CopyButton value={args} ariaLabel="Copy arguments" />}
          </div>
          <pre className="scroll-thin max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white px-2.5 py-2 font-mono text-[11.5px] leading-5 text-ink">
            {args || "(none)"}
          </pre>
          {missing.length > 0 && (
            <p className="mt-1.5 rounded-md border border-danger/25 bg-danger/[0.05] px-2.5 py-1.5 text-[12px] leading-5 text-danger">
              The tool declares{" "}
              <strong className="font-mono font-medium">{missing.join(", ")}</strong> required, and
              the agent sent {missing.length === 1 ? "it" : "them"} empty.
            </p>
          )}
          {tool ? (
            <ToolSchema tool={tool} />
          ) : tool === undefined ? null : (
            /* No frozen definition to check the arguments against, so the
               "required field left empty" badge cannot fire either. Saying why
               beats a silently thinner card: a built-in, an MCP tool whose
               schema is fetched live, or a tool deleted since the call. */
            <p className="mt-2 text-[11px] leading-4 text-faint">
              Not pinned to this call&rsquo;s version — a built-in, a tool from an integration, or
              one deleted since. Its declared parameters are not available here.
            </p>
          )}
        </div>
        <div className="min-w-0">
          <div className="mb-1.5 flex items-center justify-between gap-2 text-[11px] font-medium text-muted">
            {failed ? "Failure" : "Result"}
            {result && !failed && <CopyButton value={result} ariaLabel="Copy result" />}
          </div>
          {result == null ? (
            <ToolOutcomeNote beat={beat} />
          ) : (
            <pre
              className={cn(
                "scroll-thin max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-md px-2.5 py-2 font-mono text-[11.5px] leading-5",
                failed
                  ? "border border-danger/25 bg-danger/[0.05] text-danger"
                  : "bg-white text-ink",
              )}
            >
              {result || "(empty)"}
            </pre>
          )}
          {toldTheModel && (
            <div className="mt-1.5">
              <div className="mb-1 text-[11px] font-medium text-muted">The model was told</div>
              <pre className="scroll-thin max-h-28 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white px-2.5 py-2 font-mono text-[11.5px] leading-5 text-ink">
                {toldTheModel}
              </pre>
            </div>
          )}
        </div>
      </div>
    </details>
  );
}

/* The declared shape, beside the arguments that were actually sent. Without it
   you cannot tell whether a field the model left blank was optional. */
function ToolSchema({ tool }: { tool: CallToolResponse }) {
  const properties = tool.json_schema?.properties;
  const required = new Set(
    Array.isArray(tool.json_schema?.required) ? tool.json_schema.required.map(String) : [],
  );
  if (typeof properties !== "object" || properties === null) return null;
  const entries = Object.entries(properties as Record<string, { type?: unknown }>);
  if (entries.length === 0) return null;
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-[11px] text-faint hover:text-muted">
        Declared parameters ({entries.length})
      </summary>
      <dl className="mt-1.5 grid gap-1">
        {entries.map(([field, spec]) => (
          <div key={field} className="flex flex-wrap items-baseline gap-1.5">
            <dt className="font-mono text-[11.5px] text-ink-soft">{field}</dt>
            <dd className="font-mono text-[11px] text-faint">
              {typeof spec?.type === "string" ? spec.type : "any"}
              {required.has(field) ? " · required" : ""}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/* The three endings that have no result to print. Each says what happened
   instead, because "(empty)" is the one thing none of them means. */
function ToolOutcomeNote({ beat }: { beat: ToolBeatData }) {
  if (beat.verdict === "unfinished") {
    return (
      <div className="rounded-md border border-warn/25 bg-warn/[0.06] px-2.5 py-2 text-[12.5px] leading-5 text-warn">
        The session ended before this tool returned.
      </div>
    );
  }
  if (beat.verdict === "cancelled") {
    return (
      <div className="rounded-md border border-warn/25 bg-warn/[0.06] px-2.5 py-2 text-[12.5px] leading-5 text-warn">
        {beat.trace?.background
          ? "Cancelled while it was still running in the background — the call ended, or the agent handed over."
          : beat.cancelledBy === "caller"
            ? "Cancelled before it finished — the caller interrupted."
            : "Cancelled before it finished — the call ended."}
      </div>
    );
  }
  return (
    <div className="rounded-md border border-line bg-white px-2.5 py-2 text-[12.5px] leading-5 text-muted">
      Ran and returned nothing to the model. Normal for a tool that ends the call, or whose result
      the agent is not meant to speak.
    </div>
  );
}
