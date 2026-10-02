"use client";

import type { ReactNode } from "react";
import type { JsonObject, RunStepResponse, RunToolResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { localTimezone } from "@/lib/date";
import {
  Badge,
  Button,
  CopyButton,
  Field,
  Input,
  Select,
  SessionStatus,
} from "@/app/components/ui";
import { kindLabel } from "./operationMetadata";
import { KVRows } from "@/app/components/KVRows";
import { paramValueType, type ParamDraft } from "./operationTypes";

/** What the author has typed into the test form. Held by the editor rather than
 *  here, so flipping to the Tree tab and back does not empty it — and nowhere
 *  else: test scaffolding is neither saved with the tool nor kept past a
 *  reload. */
/** `seed` is the session userdata this run starts from; `vars` the
 *  `{{vars.*}}` values, which on a real session come off the agent and the
 *  request that started it. */
export type ToolTestDraft = { args: Record<string, string>; seed: JsonObject; vars: JsonObject };

export const EMPTY_TEST_DRAFT: ToolTestDraft = { args: {}, seed: {}, vars: {} };

/** Whose problem a failure is, in words an author can act on. */
const ERROR_TYPE_LABEL: Record<string, string> = {
  tool_config: "Tool configuration",
  tool_auth: "Missing secret",
  endpoint_error: "Your endpoint",
  endpoint_unreachable: "Endpoint unreachable",
  timeout: "Timed out",
  code_error: "Code error",
  platform: "Talqing",
  unknown: "Unknown",
};

function pretty(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

/** The form holds strings; the API takes the parameter's declared type. An
 *  empty box sends nothing at all rather than `""` — a blank optional number
 *  would otherwise fail the tool's own schema before the run started. */
function toArguments(params: ParamDraft[], args: ToolTestDraft["args"]): JsonObject {
  const out: JsonObject = {};
  for (const param of params) {
    const name = param.name.trim();
    if (!name) continue;
    const raw = (args[name] ?? "").trim();
    if (!raw) continue;
    const type = paramValueType(param);
    if (type === "number" || type === "integer") {
      const n = Number(raw);
      out[name] = Number.isFinite(n) ? n : raw;
    } else if (type === "boolean") {
      out[name] = raw.toLowerCase() === "true";
    } else {
      out[name] = raw;
    }
  }
  return out;
}

export function ToolTestPanel({
  params,
  silent,
  agentNameById,
  running,
  result,
  stale,
  draft,
  onDraftChange,
  onRun,
  className,
}: {
  params: ParamDraft[];
  silent: boolean;
  agentNameById: Record<string, string>;
  running: boolean;
  result: RunToolResponse | null;
  /** The draft moved on since this run, so the trace describes a tool that no
   *  longer exists. */
  stale: boolean;
  draft: ToolTestDraft;
  onDraftChange: (next: ToolTestDraft) => void;
  onRun: (args: JsonObject, userdata: JsonObject, vars: JsonObject) => void;
  className?: string;
}): JSX.Element {
  const update = onDraftChange;
  const named = params.filter((p) => p.name.trim());

  return (
    <aside className={cn("min-w-0 lg:h-full", className)}>
      <section className="flex min-h-[360px] flex-col overflow-hidden rounded-xl border border-line-2 bg-white lg:h-full lg:min-h-0">
        <div className="scroll-thin min-h-0 flex-1 overflow-auto">
          <Band title="Arguments">
            {named.length === 0 ? (
              <p className="text-[13px] leading-5 text-muted">This tool takes no arguments.</p>
            ) : (
              <div className="flex flex-col gap-3">
                {named.map((param, i) => (
                  <ArgumentField
                    key={`${param.name}-${i}`}
                    param={param}
                    value={draft.args[param.name] ?? ""}
                    onChange={(v) =>
                      update({ ...draft, args: { ...draft.args, [param.name]: v } })
                    }
                  />
                ))}
              </div>
            )}
          </Band>

          <Band
            title="User Data"
            action={
              <button
                type="button"
                onClick={() => update({ ...draft, seed: { ...draft.seed, "": "" } })}
                className="text-[12.5px] font-medium leading-5 text-muted transition-colors hover:text-ink"
              >
                + variable
              </button>
            }
          >
            <KVRows
              obj={draft.seed}
              onChange={(seed) => update({ ...draft, seed })}
              empty="None — the session starts empty."
              className=""
              emptyClassName=""
            />
          </Band>

          {/* Beside User Data, not inside it: on a real session these two come
              from different places and mean different things — userdata is about
              the person and any operation may write it, these are the agent's
              declared variables valued by the request that started the session,
              and nothing can write them. */}
          <Band
            title="Variables"
            action={
              <button
                type="button"
                onClick={() => update({ ...draft, vars: { ...draft.vars, "": "" } })}
                className="text-[12.5px] font-medium leading-5 text-muted transition-colors hover:text-ink"
              >
                + variable
              </button>
            }
          >
            <KVRows
              obj={draft.vars}
              onChange={(vars) => update({ ...draft, vars })}
              empty="None — every {{vars.…}} reads as empty."
              className=""
              emptyClassName=""
            />
          </Band>

          <div className="border-b border-line px-4 py-4">
            <div className="flex items-center gap-3">
              <Button
                onClick={() => onRun(toArguments(params, draft.args), draft.seed, draft.vars)}
                disabled={running}
              >
                {running ? "Running…" : "Run tool"}
              </Button>
              {running && <SessionStatus tone="busy">Running</SessionStatus>}
            </div>
            <p className="mt-3 text-[12px] leading-4 text-faint">
              Runs the draft against your real endpoints, with your real secrets. A tool that
              books or charges will do so. There is no call behind a test run, so the phone-call
              variables read as empty; the clock runs on{" "}
              <code className="font-mono">{localTimezone()}</code>, this browser&rsquo;s timezone.
              There is no agent either, so the variables above are the whole{" "}
              <code className="font-mono">{"{{vars.…}}"}</code> bag rather than an override on
              anything declared.
            </p>
          </div>

          {result && (
            <RunResult
              result={result}
              silent={silent}
              stale={stale}
              agentNameById={agentNameById}
            />
          )}
        </div>
      </section>
    </aside>
  );
}

/** One titled band of the panel, hairline-separated like the rest of the editor. */
function Band({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="border-b border-line px-4 py-4">
      <div className="mb-2.5 flex items-center gap-3">
        <h3 className="min-w-0 flex-1 text-[13px] font-semibold leading-5 text-ink">{title}</h3>
        {action}
      </div>
      {children}
    </div>
  );
}

function ArgumentField({
  param,
  value,
  onChange,
}: {
  param: ParamDraft;
  value: string;
  onChange: (value: string) => void;
}): JSX.Element {
  const type = paramValueType(param);
  const label = (
    <span className="flex items-baseline gap-1.5">
      <span className="font-mono text-[13px]">{param.name}</span>
      <span className="text-[12px] font-normal text-faint">
        {param.type === "enum" ? "enum" : type}
      </span>
      {param.required && (
        <span className="text-[12px] font-normal text-warn" title="Required">
          required
        </span>
      )}
    </span>
  );
  const options =
    param.type === "enum"
      ? param.enumValues.map((v) => v.trim()).filter(Boolean)
      : type === "boolean"
        ? ["true", "false"]
        : null;

  return (
    /* No description here: it is written for the model, and repeating it under
       every box turns a form you fill in ten seconds into a wall of prose. */
    <Field label={label}>
      {options ? (
        <Select value={value} onChange={(e) => onChange(e.target.value)} aria-label={param.name}>
          <option value="">—</option>
          {options.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </Select>
      ) : (
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          type={type === "number" || type === "integer" ? "number" : "text"}
          aria-label={param.name}
          className={cn("min-h-9 py-1.5 text-[13px]", type === "string" && "font-mono")}
        />
      )}
    </Field>
  );
}

function RunResult({
  result,
  silent,
  stale,
  agentNameById,
}: {
  result: RunToolResponse;
  silent: boolean;
  stale: boolean;
  agentNameById: Record<string, string>;
}): JSX.Element {
  const steps = result.steps ?? [];
  return (
    <div className={cn("flex flex-col", stale && "opacity-55")}>
      {stale && (
        <div className="border-b border-line bg-warn/[0.06] px-4 py-2 text-[12px] leading-4 text-warn">
          The draft changed after this run.
        </div>
      )}

      <div className="grid grid-cols-3 divide-x divide-line border-b border-line">
        <Cell
          label="Result"
          value={result.ok ? "Passed" : "Failed"}
          tone={result.ok ? "live" : "danger"}
        />
        <Cell label="Duration" value={`${result.duration_ms} ms`} />
        <Cell label="Steps" value={String(steps.length)} />
      </div>

      {!result.ok && result.error && (
        <div className="border-b border-line bg-danger/[0.05] px-4 py-3">
          <div className="mb-1 flex items-center gap-2">
            <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-danger">
              Failed
            </span>
            {result.error_type && (
              <Badge variant="danger">
                {ERROR_TYPE_LABEL[result.error_type] ?? result.error_type}
              </Badge>
            )}
          </div>
          <p className="text-[13px] leading-5 text-danger [overflow-wrap:anywhere]">
            {result.error}
          </p>
        </div>
      )}

      <div className="divide-y divide-line border-b border-line">
        {steps.map((step) => (
          <StepRow key={step.path} step={step} agentNameById={agentNameById} />
        ))}
      </div>

      <Bag label="tooldata" hint="dropped when the tool returns" obj={result.tooldata} />
      <Bag label="userdata" hint="kept for the rest of the session" obj={result.userdata} />

      <div className="px-4 py-4">
        <div className="mb-2 flex items-center gap-3">
          <h3 className="min-w-0 flex-1 text-[13px] font-semibold leading-5 text-ink">
            What the model receives
          </h3>
          {result.llm_response && <CopyButton value={result.llm_response} ariaLabel="Copy" />}
        </div>
        {silent || result.ended_call ? (
          <p className="text-[12.5px] leading-5 text-muted">
            {silent
              ? "Nothing — this tool is silent, so the agent does not reply after it runs."
              : "Nothing — the call ends here, so the agent is stopped rather than handed a result to narrate at someone who has hung up."}
          </p>
        ) : result.llm_response ? (
          <pre className="scroll-thin max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-md bg-ink px-2.5 py-2 font-mono text-[11.5px] leading-5 text-white">
            {pretty(result.llm_response)}
          </pre>
        ) : (
          <p className="text-[12.5px] leading-5 text-muted">
            Nothing — the run failed, so the model would get an error instead.
          </p>
        )}
        {(result.ended_call || result.handed_off) && (
          <p className="mt-2 text-[12.5px] leading-5 text-muted">
            {result.handed_off
              ? "The conversation would hand off to another agent here."
              : "The call would end here."}
          </p>
        )}
      </div>
    </div>
  );
}

function Cell({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "live" | "danger";
}): JSX.Element {
  return (
    <div className="grid content-start gap-1 px-4 py-3">
      <div className="truncate text-[11px] font-medium leading-4 text-muted">{label}</div>
      <div
        className={cn(
          "font-mono text-[14px] font-semibold leading-5 tabular-nums text-ink",
          tone === "live" && "text-live",
          tone === "danger" && "text-danger",
        )}
      >
        {value}
      </div>
    </div>
  );
}

/** What the tool wrote into one of the two stores.
 *
 *  Shown even when empty: "did my publish field work?" is the question this
 *  panel exists to answer, and an absent section reads as "not applicable"
 *  rather than "nothing was written". */
function Bag({
  label,
  hint,
  obj,
}: {
  label: string;
  hint: string;
  obj: JsonObject | undefined;
}): JSX.Element {
  const entries = Object.entries(obj ?? {});
  return (
    <div className="border-b border-line px-4 py-4">
      <div className="mb-2 flex items-baseline gap-2">
        <h3 className="font-mono text-[12.5px] font-semibold leading-5 text-ink">{label}</h3>
        <span className="min-w-0 flex-1 truncate text-[11.5px] leading-4 text-faint">{hint}</span>
        {entries.length > 0 && (
          <CopyButton value={() => JSON.stringify(obj, null, 2)} ariaLabel={`Copy ${label}`} />
        )}
      </div>
      {entries.length === 0 ? (
        <p className="text-[12.5px] leading-5 text-muted">Nothing written.</p>
      ) : (
        <div className="overflow-hidden rounded-md border border-line-2">
          {entries.map(([k, v], i) => (
            <div
              key={k}
              className={cn(
                "grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] items-baseline gap-3 px-2.5 py-2",
                i > 0 && "border-t border-line",
              )}
            >
              <span className="font-mono text-[12px] font-medium leading-5 text-ink">{k}</span>
              <span className="min-w-0 whitespace-pre-wrap break-words font-mono text-[12px] leading-5 text-ink-soft">
                {pretty(v)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function stepSummary(step: RunStepResponse, agentNameById: Record<string, string>): string {
  const d = step.detail ?? {};
  const req = d.request as { method?: string; url?: string } | undefined;
  const res = d.response as { status?: number } | undefined;
  if (step.kind === "http") {
    const line = req ? `${req.method ?? "GET"} ${req.url ?? ""}` : "no request built";
    return res?.status ? `${line} → ${res.status}` : line;
  }
  if (step.kind === "if") {
    return `${JSON.stringify(d.left)} ${d.op} ${JSON.stringify(d.right)} → ${d.branch}`;
  }
  if (step.kind === "set_variable") return `${d.store}.${d.key}`;
  if (step.kind === "say" || step.kind === "add_message") return String(d.text ?? "");
  if (step.kind === "generate_reply") return String(d.instructions ?? "");
  if (step.kind === "handoff") {
    if (d.agent_name) return `to team member “${d.agent_name}”`;
    const id = String(d.target_agent_id ?? "");
    return `to ${agentNameById[id] ?? id}`;
  }
  if (step.kind === "frontend_rpc") return String(d.method ?? "");
  if (step.kind === "end_call") return "the call would end";
  if (step.kind === "code") {
    const logs = Array.isArray(d.logs) ? d.logs.length : 0;
    return logs ? `${logs} console line${logs === 1 ? "" : "s"}` : "ran";
  }
  return "";
}

function StepRow({
  step,
  agentNameById,
}: {
  step: RunStepResponse;
  agentNameById: Record<string, string>;
}): JSX.Element {
  const dot =
    step.status === "failed" ? "bg-danger" : step.status === "simulated" ? "bg-muted" : "bg-live";
  const background = Boolean((step.detail ?? {}).background);

  return (
    <details className="group">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-[13px] transition-colors hover:bg-hover [&::-webkit-details-marker]:hidden">
        <span className={cn("h-1.5 w-1.5 flex-none rounded-full", dot)} aria-hidden />
        <span className="flex-none font-mono text-[11px] text-faint">{step.path}</span>
        <span className="flex-none font-medium text-ink">{kindLabel(step.kind)}</span>
        <span className="min-w-0 flex-1 truncate text-[12px] text-faint">
          {stepSummary(step, agentNameById)}
        </span>
        <span className="flex-none text-[11px] tabular-nums text-faint">{step.duration_ms}ms</span>
      </summary>
      <div className="flex flex-col gap-3 border-t border-line bg-subtle px-4 py-3">
        {step.status === "simulated" && (
          <p className="text-[12px] leading-4 text-muted">
            Recorded, not performed — this needs a live conversation. The text below is what it
            resolved to.
          </p>
        )}
        {background && (
          <p className="text-[12px] leading-4 text-muted">
            Runs in the background on a real call, so it publishes nothing and you never see it
            fail. Run inline here so you can.
          </p>
        )}
        <StepDetail step={step} />
        {(step.published ?? []).length > 0 && (
          <div>
            <div className="mb-1.5 text-[11px] font-medium text-muted">Published</div>
            <div className="overflow-hidden rounded-md border border-line-2 bg-white">
              {(step.published ?? []).map((p, i) => (
                <div
                  key={i}
                  className={cn(
                    "grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] items-baseline gap-3 px-2.5 py-2",
                    i > 0 && "border-t border-line",
                  )}
                >
                  <span className="font-mono text-[12px] font-medium leading-5 text-ink">
                    {p.store}.{p.key}
                  </span>
                  <span className="min-w-0 whitespace-pre-wrap break-words font-mono text-[12px] leading-5 text-ink-soft">
                    {pretty(p.value)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        {step.error && (
          <pre className="scroll-thin max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-md border border-danger/25 bg-danger/[0.05] px-2.5 py-2 font-mono text-[11.5px] leading-5 text-danger">
            {step.error}
          </pre>
        )}
      </div>
    </details>
  );
}

function Block({ label, body }: { label: string; body: string }): JSX.Element {
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-medium text-muted">
        {label}
        {body && <CopyButton value={body} ariaLabel={`Copy ${label}`} />}
      </div>
      <pre className="scroll-thin max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white px-2.5 py-2 font-mono text-[11.5px] leading-5 text-ink">
        {body || "(none)"}
      </pre>
    </div>
  );
}

function StepDetail({ step }: { step: RunStepResponse }): JSX.Element | null {
  const d = step.detail ?? {};
  if (Object.keys(d).length === 0) return null;

  if (step.kind === "http") {
    const req = (d.request ?? {}) as JsonObject;
    const res = d.response as JsonObject | undefined;
    const query = req.query as JsonObject | undefined;
    return (
      <div className="grid gap-3">
        <Block
          label="Request"
          body={pretty({
            method: req.method,
            url: req.url,
            ...(query && Object.keys(query).length ? { query } : {}),
            headers: req.headers,
            ...(req.body == null ? {} : { body: req.body }),
            // Present so a timeout reads as "1s was not enough" rather than
            // "it failed"; harmless noise on a request that succeeded.
            ...(req.timeout == null ? {} : { timeout: req.timeout }),
          })}
        />
        {res ? (
          <Block label={`Response · ${res.status}`} body={pretty(res.body)} />
        ) : (
          <div className="min-w-0">
            <div className="mb-1 text-[11px] font-medium text-muted">Response</div>
            <p className="text-[12.5px] leading-5 text-muted">
              None — the request never reached your endpoint.
            </p>
          </div>
        )}
      </div>
    );
  }

  if (step.kind === "code") {
    const logs = Array.isArray(d.logs) ? (d.logs as unknown[]) : [];
    return (
      <div className="grid gap-3">
        <Block label="Console" body={logs.map(String).join("\n")} />
        <Block label="Returned" body={pretty(d.result)} />
      </div>
    );
  }

  return <Block label="Detail" body={pretty(d)} />;
}
