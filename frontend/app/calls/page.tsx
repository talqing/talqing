"use client";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { CallDetailResponse, CallSummaryResponse, CallOutcome, RecordingState, CallType } from "@talqing/sdk";
import { AppShell } from "../components/AppShell";
import {
  Badge,
  Button,
  Container,
  EmptyState,
  Input,
  ListSkeleton,
  PageHead,
  Select,
} from "../components/ui";
import { CallDetail, CallDetailSkeleton, TYPE_LABEL, callerLabel } from "./CallDetail";
import { OUTCOME_LABEL, OutcomeBadge } from "./CallAnalysis";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { DatePicker } from "../components/DatePicker";
import { toISODate, today } from "@/lib/date";

const PAGE_SIZE = 50;

/* Recordings are kept for a limited window, so an older call legitimately has
   nothing to play. Saying so in the row is what stops that reading as a bug when
   someone scrolls back through last month and finds every player empty. Nothing
   is shown for the ordinary cases — audio present, or an agent that never
   recorded — because a note on every row is a note nobody reads. */
function recordingNote(state: RecordingState): string {
  if (state === "available") return " · \u25b6 audio";
  if (state === "expired") return " · audio expired";
  return "";
}

const PhoneIcon = (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 6.5C4 5 5 4 6.5 4H8l1.5 4L8 9.5a11 11 0 0 0 6.5 6.5L16 14.5 20 16v1.5c0 1.5-1 2.5-2.5 2.5A13.5 13.5 0 0 1 4 6.5Z" />
  </svg>
);

/** Why this call ran something other than its agent's published version.
 *
 *  Nothing at all on an ordinary call, which is almost every row — the badge has
 *  to earn its place, and "ran exactly as published" is not news. */
function planBadge(plan: CallSummaryResponse["plan"]): React.ReactNode {
  if (!plan) return null;
  if (plan.members > 1) return <Badge variant="info">Team · {plan.members}</Badge>;
  /* "Pinned" and "Overridden" are different claims about the config, and a
     version pin changes nothing about it — saying the wrong one is worse than
     saying less. */
  const label = plan.inline
    ? "Inline"
    : plan.draft
      ? "Draft"
      : plan.overridden
        ? "Overridden"
        : "Pinned";
  return <Badge variant="info">{label}</Badge>;
}

function statusVariant(s: string): "default" | "live" | "danger" {
  if (s === "in_progress" || s === "queued") return "live";
  if (s === "failed") return "danger";
  return "default";
}

function money(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v === 0) return "$0";
  return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function shortDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  return m > 0 ? `${m}m ${seconds % 60}s` : `${seconds}s`;
}

function relative(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(iso).toLocaleDateString();
}

// Send the day's full span so the boundary days are inclusive on the server.
const startIso = (d: string) => (d ? `${d}T00:00:00` : undefined);
const endIso = (d: string) => (d ? `${d}T23:59:59` : undefined);

function daysAgo(n: number): Date {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

function CallsPageInner() {
  const region = useActiveRegion();
  // Query param, not a path segment: the dashboard ships as a static export,
  // which cannot prerender an unbounded set of ids.
  const params = useSearchParams();
  const initialId = params.get("id") ?? "";
  // Arriving from a call's header: every call with this one person. Read once
  // as the initial value so clearing the chip is not undone on the next render.
  const initialContactKey = params.get("contact_key") ?? "";
  // Arriving from an observability row — "show me the 41 calls the carrier
  // dropped". Every filter below seeds from the query string for the same
  // reason: a drill-down that landed on an unfiltered list would make the
  // reader rebuild by hand the question they had already asked.
  const initialCloseReason = params.get("close_reason") ?? "";

  const [calls, setCalls] = useState<CallSummaryResponse[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [selectedId, setSelectedId] = useState(initialId);
  const [detail, setDetail] = useState<CallDetailResponse | null>(null);
  const [detailErr, setDetailErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const [start, setStart] = useState(params.get("start") || toISODate(daysAgo(30)));
  const [end, setEnd] = useState(params.get("end") || toISODate(new Date()));
  const [agentId, setAgentId] = useState(params.get("agent") ?? "");
  const [type, setType] = useState<CallType | "">((params.get("type") ?? "") as CallType | "");
  const [outcome, setOutcome] = useState<CallOutcome | "">(
    (params.get("outcome") ?? "") as CallOutcome | "",
  );
  const [contactKey, setContactKey] = useState(initialContactKey);
  const [closeReason, setCloseReason] = useState(initialCloseReason);

  const filters = useMemo(
    () => ({
      start: startIso(start),
      end: endIso(end),
      agent_id: agentId || undefined,
      type: type || undefined,
      outcome: outcome || undefined,
      contact_key: contactKey || undefined,
      closeReason: closeReason || undefined,
    }),
    [start, end, agentId, type, outcome, contactKey, closeReason],
  );

  useEffect(() => {
    api
      .listAllAgents()
      .then((list: { id: string; config?: { name?: string } }[]) =>
        setAgents(
          list.map((a) => ({ id: String(a.id), name: a.config?.name || String(a.id).slice(0, 8) })),
        ),
      )
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    api
      .listCalls({ ...filters, limit: PAGE_SIZE })
      .then((page) => {
        setCalls(page.items);
        setHasMore(page.has_more);
        setErr("");
        // Only pick a call when nothing is open. A selection that falls outside
        // the current page STAYS open: `getCall` fetches by id and does not care
        // about the list's filters, and swapping it for the newest match is how
        // a shared `?id=…` link — the whole point of this page's URL — quietly
        // opened somebody else's call whenever the target was older than the
        // default thirty days or past the first fifty rows. The pane says so
        // rather than pretending the list is complete.
        setSelectedId((current) => current || (page.items[0]?.id ?? ""));
      })
      .catch((error) => setErr(apiErrorMessage(error)))
      .finally(() => setLoading(false));
  }, [filters]);

  useEffect(() => load(), [load]);

  /* Deep-linkable without a route segment: the whole view rides in the query
     string. Every filter, not just the three that used to be written back —
     each one is READ from the URL on mount, so writing only some of them left a
     drill-down's `?agent=…&type=…` sitting in the address bar describing a
     screen the reader had since filtered differently. Sharing that link handed
     somebody else a different set of calls than the sender was looking at. */
  useEffect(() => {
    const url = new URL(window.location.href);
    for (const [key, value] of [
      ["id", selectedId],
      ["contact_key", contactKey],
      ["close_reason", closeReason],
      ["agent", agentId],
      ["type", type],
      ["outcome", outcome],
      ["start", start],
      ["end", end],
    ] as const) {
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    window.history.replaceState(null, "", url);
  }, [selectedId, contactKey, closeReason, agentId, type, outcome, start, end]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailErr("");
      return;
    }
    let stale = false;
    setDetail(null);
    setDetailErr("");
    api
      .getCall(selectedId)
      .then((d) => {
        if (!stale) setDetail(d);
      })
      .catch((error) => {
        if (!stale) setDetailErr(apiErrorMessage(error, "Could not load this call."));
      });
    return () => {
      stale = true;
    };
  }, [selectedId]);

  const reloadDetail = useCallback(() => {
    if (!selectedId) return;
    api
      .getCall(selectedId)
      .then(setDetail)
      .catch((error) => setDetailErr(apiErrorMessage(error, "Could not load this call.")));
  }, [selectedId]);

  // A running call keeps changing; refresh the open one until it ends.
  useEffect(() => {
    if (!selectedId || detail?.session.status !== "in_progress") return;
    const timer = window.setInterval(() => {
      api
        .getCall(selectedId)
        .then((next) => setDetail((current) => (current?.session.id === next.session.id ? next : current)))
        // Keep the last good snapshot on screen; the retry button handles the rest.
        .catch(() => {});
    }, 5000);
    return () => window.clearInterval(timer);
  }, [selectedId, detail?.session.status]);

  async function loadMore() {
    if (!hasMore || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.listCalls({ ...filters, limit: PAGE_SIZE, offset: calls.length });
      setCalls((current) => [...current, ...page.items]);
      setHasMore(page.has_more);
    } catch (error) {
      setErr(apiErrorMessage(error, "Could not load more calls."));
    } finally {
      setLoadingMore(false);
    }
  }

  const agentNames = useMemo(
    () => Object.fromEntries(agents.map((a) => [a.id, a.name])),
    [agents],
  );

  function preset(days: number) {
    setStart(toISODate(daysAgo(days)));
    setEnd(toISODate(new Date()));
  }

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Calls"
          sub="Every voice and video call, with its transcript, per-turn latency, tool calls and computed cost."
        />

        <div className="mb-5 flex flex-wrap items-center gap-3">
          <div className="inline-flex items-center gap-1 rounded-[10px] border border-line-2 bg-white p-1">
            {([7, 30, 90] as const).map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => preset(d)}
                className={cn(
                  "min-h-[28px] rounded-md px-2.5 text-[13px] font-medium transition-colors",
                  start === toISODate(daysAgo(d))
                    ? "bg-ink text-white"
                    : "text-ink-soft hover:bg-hover hover:text-ink-hover",
                )}
              >
                {d}d
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            {/* Each end bounds the other, so the range cannot be inverted by
                picking rather than by validating after the fact. Not clearable:
                the list is always over some window. */}
            <DatePicker
              value={start}
              max={end}
              clearable={false}
              ariaLabel="Start date"
              onChange={(d) => d && setStart(d)}
              className="w-[150px]"
            />
            <span className="text-muted">→</span>
            <DatePicker
              value={end}
              min={start}
              max={today()}
              clearable={false}
              ariaLabel="End date"
              onChange={(d) => d && setEnd(d)}
              className="w-[150px]"
            />
          </div>
          <Select value={agentId} onChange={(e) => setAgentId(e.target.value)} className="w-auto min-w-[180px]" aria-label="Filter by agent">
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </Select>
          <Select value={type} onChange={(e) => setType(e.target.value as CallType | "")} className="w-auto min-w-[150px]" aria-label="Filter by call type">
            <option value="">All types</option>
            {(Object.keys(TYPE_LABEL) as CallType[]).map((t) => (
              <option key={t} value={t}>{TYPE_LABEL[t]}</option>
            ))}
          </Select>
          <Select value={outcome} onChange={(e) => setOutcome(e.target.value as CallOutcome | "")} className="w-auto min-w-[150px]" aria-label="Filter by outcome">
            <option value="">Any outcome</option>
            {(Object.keys(OUTCOME_LABEL) as CallOutcome[]).map((o) => (
              <option key={o} value={o}>{OUTCOME_LABEL[o]}</option>
            ))}
          </Select>
          {/* Set by following the caller link on a call, never by typing —
              a contact key is an opaque id, so a text box for it would be a
              worse control than the link that already knows the value. */}
          {contactKey && (
            <button
              type="button"
              onClick={() => setContactKey("")}
              title="Show all callers again"
              className="inline-flex min-h-[30px] items-center gap-1.5 rounded-full border border-ink bg-ink px-2.5 text-[12.5px] font-medium text-white"
            >
              <span className="max-w-[220px] truncate font-mono">{contactKey}</span>
              <span aria-hidden>×</span>
              <span className="sr-only">Clear the caller filter</span>
            </button>
          )}
          {/* Same shape, same reason: arrived here from an observability
              endings row, and is cleared rather than typed. */}
          {closeReason && (
            <button
              type="button"
              onClick={() => setCloseReason("")}
              title="Show every ending again"
              className="inline-flex min-h-[30px] items-center gap-1.5 rounded-full border border-ink bg-ink px-2.5 text-[12.5px] font-medium text-white"
            >
              <span className="max-w-[220px] truncate font-mono">{closeReason}</span>
              <span aria-hidden>×</span>
              <span className="sr-only">Clear the ending filter</span>
            </button>
          )}
        </div>

        {err && (
          <div className="mb-4 flex items-center gap-3 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            <span className="min-w-0 flex-1">{err}</span>
            <Button size="sm" onClick={load}>Retry</Button>
          </div>
        )}

        {/* No range-wide totals here. Calls / talk time / spend / avg cost /
            success rate are a workspace roll-up, and they belong on
            /observability — this page is for finding and reading ONE call.
            `call_stats` stays in the API and on /agents, which is where a
            per-agent roll-up is the point. */}

        {loading ? (
          <ListSkeleton rows={5} />
        ) : calls.length === 0 ? (
          <EmptyState
            icon={PhoneIcon}
            title={`No calls in this range in ${region.name}.`}
            body="Adjust the date range or agent filter — or open an agent, publish it, and place a test call."
          />
        ) : (
          /* The page owns the viewport height from lg up, so the list and the
             detail pane scroll independently instead of as one long column. */
          <section
            className="overflow-hidden rounded-xl border border-line-2 bg-white lg:grid lg:h-[calc(100vh-120px)] lg:min-h-[560px] lg:grid-cols-[340px_minmax(0,1fr)]"
            aria-label="Call history"
          >
            <aside className="border-b border-line lg:flex lg:min-h-0 lg:flex-col lg:border-b-0 lg:border-r">
              <div className="flex flex-none items-center justify-between border-b border-line px-4 py-2.5">
                <span className="text-[13px] font-medium text-ink">
                  {calls.length} {calls.length === 1 ? "call" : "calls"}
                </span>
                <span className="text-[11px] text-faint">Newest first</span>
              </div>
              <div className="scroll-thin max-h-[380px] overflow-y-auto lg:max-h-none lg:min-h-0 lg:flex-1">
                {calls.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => setSelectedId(c.id)}
                    className={cn(
                      "grid w-full gap-1.5 border-b border-line px-4 py-3 text-left transition-colors",
                      selectedId === c.id ? "bg-subtle" : "bg-white hover:bg-hover",
                    )}
                  >
                    <span className="flex min-w-0 items-start justify-between gap-2">
                      <span className="min-w-0">
                        <strong
                          className={cn(
                            "block truncate text-[13.5px] font-semibold text-ink",
                            /* An inline agent genuinely has no row to open. The
                               italic says the name is this call's own, not a
                               link somebody forgot to make. */
                            c.plan?.inline && "italic",
                          )}
                        >
                          {c.agent.name || "Unknown agent"}
                        </strong>
                        <small className="mt-0.5 block truncate font-mono text-[11.5px] text-muted">
                          {c.from_e164 || c.to_e164
                            ? [callerLabel(c.type, c.from_e164), c.to_e164].filter(Boolean).join(" → ")
                            : c.id.slice(0, 18)}
                        </small>
                      </span>
                      <span className="flex flex-none items-center gap-1.5">
                        {planBadge(c.plan)}
                        {c.outcome && <OutcomeBadge outcome={c.outcome} />}
                        <Badge variant={statusVariant(c.status)} dot={statusVariant(c.status) === "live"}>
                          {c.status}
                        </Badge>
                      </span>
                    </span>
                    {c.summary && (
                      <span className="line-clamp-2 text-[12px] leading-[1.45] text-muted">
                        {c.summary}
                      </span>
                    )}
                    <span className="flex min-w-0 items-center justify-between gap-2 text-[11px] text-faint">
                      <span className="truncate">
                        {TYPE_LABEL[c.type as CallType] ?? c.type} · {shortDuration(c.duration_s)} ·{" "}
                        {c.message_count} msg{c.cost ? ` · ${money(c.cost.total_charge)}` : ""}
                        {recordingNote(c.recording.state)}
                      </span>
                      <time className="flex-none">{relative(c.started_at)}</time>
                    </span>
                  </button>
                ))}
                {hasMore && (
                  <div className="px-4 py-3">
                    <Button size="sm" onClick={() => void loadMore()} disabled={loadingMore}>
                      {loadingMore ? "Loading…" : "Load more calls"}
                    </Button>
                  </div>
                )}
              </div>
            </aside>

            <div className="scroll-thin min-w-0 bg-canvas lg:min-h-0 lg:overflow-y-auto">
              {/* The open call is not in the list beside it — a deep link to an
                  older call, or a filter change that dropped it. Saying so is
                  what makes keeping it open legible instead of looking like the
                  list failed to highlight a row. */}
              {selectedId && !loading && !calls.some((c) => c.id === selectedId) && (
                <div className="border-b border-line bg-subtle px-5 py-2.5 text-[12.5px] leading-5 text-muted">
                  This call is outside the current filters, so it is not in the list.{" "}
                  <button
                    type="button"
                    onClick={() => setSelectedId(calls[0]?.id ?? "")}
                    className="font-medium text-ink underline underline-offset-2"
                  >
                    Open the newest matching call
                  </button>
                </div>
              )}
              {detailErr ? (
                <div className="px-5 py-6">
                  <div className="rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
                    {detailErr}
                  </div>
                </div>
              ) : !detail ? (
                <CallDetailSkeleton />
              ) : (
                <CallDetail
                  call={detail}
                  agentNames={agentNames}
                  onRecordingDeleted={reloadDetail}
                  onReload={reloadDetail}
                  /* A re-run rewrites the summary and outcome the list row
                     shows too, so both halves refresh. */
                  onAnalysisChanged={() => {
                    reloadDetail();
                    load();
                  }}
                />
              )}
            </div>
          </section>
        )}
      </Container>
    </AppShell>
  );
}

// useSearchParams() suspends during prerender, so the exported page provides the
// boundary and CallsPageInner does the work. Without this, `next build` fails
// with "useSearchParams() should be wrapped in a suspense boundary".
export default function CallsPage() {
  return (
    <Suspense fallback={null}>
      <CallsPageInner />
    </Suspense>
  );
}
