"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import type { AgentConfig, AgentResponse, CallStatsResponse, CatalogResponse, PhoneNumberResponse, CatalogEntry } from "@talqing/sdk";
import type { MemberResponse } from "@/lib/control";
import { AppShell } from "../components/AppShell";
import { Creator, useOrgMembers } from "../components/Creator";
import { McpSetup } from "../components/McpSetup";
import type { CardAccent } from "../components/ui";
import {
  Badge,
  Button,
  EmptyState,
  Field,
  Figure,
  Input,
  Menu,
  Modal,
  STAGE_HUE,
  Segment,
  Skeleton,
} from "../components/ui";
import { timeAgo } from "../components/diff";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";

/* Every traffic figure on this page is measured over one window, and the page
   names it in the strip. Always pass the window explicitly: /v1/calls answers
   for the last 30 days when asked for none, so a total that looks like "all
   time" silently is not. */
const WINDOW_DAYS = 7;

/* The most calls /v1/calls returns in one page. Per-agent figures are bucketed
   out of that page, so a busier week than this reports a floor rather than a
   total, and the column head says as much. Workspace totals come from
   /v1/calls/stats and are exact either way. */
const CALLS_PAGE = 200;

/* Rows and their header share one template so the columns actually line up.
   Phone only earns its width on a wide screen; the traffic pair drops next.
   The meta columns are fractional so a wide window spreads the slack across all
   of them, instead of pooling it into one canyon beside the agent's name. */
const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(84px,0.22fr)_minmax(112px,0.24fr)_36px] lg:grid-cols-[minmax(0,1fr)_minmax(132px,0.28fr)_minmax(84px,0.2fr)_minmax(112px,0.22fr)_36px]";

type Channel = "voice" | "video" | "text";

const CHANNEL: Record<Channel, { label: string; glyph: JSX.Element }> = {
  voice: {
    label: "Voice",
    glyph: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" aria-hidden>
        {[3, 7, 11, 15, 19].map((x, i) => {
          const h = [6, 14, 20, 12, 8][i];
          return <rect key={x} x={x} y={(22 - h) / 2} width="2.4" height={h} rx="1.2" fill="currentColor" />;
        })}
      </svg>
    ),
  },
  video: {
    label: "Video",
    glyph: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <rect x="3" y="6" width="12" height="12" rx="2.5" />
        <path d="m15 11 5-3v8l-5-3" />
      </svg>
    ),
  },
  text: {
    label: "Text",
    glyph: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H9l-4 4V5.5Z" />
      </svg>
    ),
  },
};

const Icon = {
  plus: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  search: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  ),
};

function money(amount: number, digits = 2): string {
  return `$${amount.toFixed(digits)}`;
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/* ── what an agent is built from ─────────────────────────────────────────────
   One stage per thing the call is billed for, in pipeline order, each carrying
   the same hue it has on the editor's stage headings and in the cost meter. The
   hue is the whole point: a teal row is a realtime agent, a blue/orange/green
   row is a cascade, and that reads before any of the words do. */
type Stage = { accent: CardAccent; label: string };

/** Catalog labels carry a trailing "(OpenAI)" / "(Streaming)" qualifier that is
    useful in a picker and noise in a list of three. */
function shortLabel(entry: CatalogEntry | undefined, model: string | undefined): string {
  const label = entry?.label?.replace(/\s*\([^()]*\)\s*$/, "").trim();
  return label || model || "";
}

function pipelineStages(config: AgentConfig, catalog: CatalogResponse | null): Stage[] {
  const look = (entries: CatalogEntry[] | undefined, spec: { provider?: string; model?: string } | null | undefined) => {
    if (!spec?.model) return "";
    return shortLabel(
      entries?.find((e) => e.provider === spec.provider && e.model === spec.model),
      spec.model,
    );
  };

  const channel = (config.channel || "voice") as Channel;
  const stages: Stage[] = [];

  if (channel === "text") {
    stages.push({ accent: "llm", label: look(catalog?.llm, config.llm) });
  } else if (config.realtime?.model) {
    stages.push({ accent: "realtime", label: look(catalog?.realtime, config.realtime) });
  } else {
    stages.push({ accent: "stt", label: look(catalog?.stt, config.stt) });
    stages.push({ accent: "llm", label: look(catalog?.llm, config.llm) });
    stages.push({ accent: "tts", label: look(catalog?.tts, config.tts) });
  }
  if (channel === "video") {
    stages.push({ accent: "avatar", label: look(catalog?.avatar, config.avatar) });
  }
  return stages.filter((stage) => stage.label);
}

/** Tools, MCP servers and FAQs, counted rather than listed — the names
    belong to the editor, the fact that there are any belongs here. */
function attachments(agent: AgentResponse): string {
  const parts: string[] = [];
  const tools = agent.config.tools?.length ?? 0;
  const mcps = agent.config.mcps?.length ?? 0;
  if (tools) parts.push(plural(tools, "tool"));
  if (mcps) parts.push(plural(mcps, "integration"));
  const faqs = agent.config.faqs?.length ?? 0;
  if (faqs) parts.push(plural(faqs, "FAQ"));
  return parts.join(" · ");
}

/* ── page furniture ─────────────────────────────────────────────────────── */

type Filter = "all" | "live" | "draft";

function AgentRow({
  agent,
  catalog,
  numbers,
  traffic,
  members,
  onCopyId,
  onDuplicate,
  onDelete,
}: {
  agent: AgentResponse;
  catalog: CatalogResponse | null;
  numbers: PhoneNumberResponse[];
  traffic: { calls: number; spend: number } | null;
  members: Map<string, MemberResponse>;
  onCopyId: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const channel = (agent.config.channel || "voice") as Channel;
  const name = agent.config.name || "Untitled agent";
  const stages = pipelineStages(agent.config, catalog);
  const attached = attachments(agent);
  const published = agent.published_version;

  return (
    /* The row is a div with a stretched link rather than a link wrapping
       everything, so the overflow menu inside it is not a button inside an
       anchor. The link is absolutely positioned, which puts it above the
       static cells for hit-testing without any z-index of its own. */
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2.5 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      <Link
        href={`/agents/detail?id=${agent.id}`}
        aria-label={`${name} (${CHANNEL[channel].label.toLowerCase()} agent)`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />

      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          {CHANNEL[channel].glyph}
        </span>
        <div className="min-w-0">
          <div className="truncate font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
            {name}
          </div>
          {/* Models never shrink — they are what tells two agents apart. The
              attachment counts give way first when the column is narrow. */}
          <div className="mt-0.5 flex min-w-0 items-center gap-2 overflow-hidden text-[12px] leading-4 text-muted">
            {stages.length === 0 ? (
              <span className="text-faint">Not configured yet</span>
            ) : (
              stages.map((stage) => (
                <span key={stage.accent} className="flex flex-none items-center gap-1.5">
                  <span aria-hidden className={cn("h-[7px] w-[7px] rounded-[2px]", STAGE_HUE[stage.accent])} />
                  {stage.label}
                </span>
              ))
            )}
            {attached && (
              <>
                <span aria-hidden className="flex-none text-line-strong">|</span>
                <span className="min-w-0 truncate text-faint">{attached}</span>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="hidden min-w-0 lg:block">
        {numbers.length === 0 ? (
          <span className="text-[13px] leading-5 text-faint">—</span>
        ) : (
          <>
            <div className="truncate font-mono text-[12.5px] leading-5 text-ink tabular-nums">{numbers[0].e164}</div>
            {/* Never "+N more": next to an E.164 number a leading + reads as
                part of the dialling code. */}
            {numbers.length > 1 && (
              <div className="text-[11.5px] leading-4 text-faint">
                and {plural(numbers.length - 1, "other")}
              </div>
            )}
          </>
        )}
      </div>

      <div className="hidden sm:block">
        {!traffic ? (
          <span className="text-[13px] leading-5 text-faint">—</span>
        ) : (
          <>
            <div className="text-[13px] font-medium leading-5 text-ink tabular-nums">{traffic.calls}</div>
            <div className="text-[11.5px] leading-4 text-faint tabular-nums">{money(traffic.spend, 2)}</div>
          </>
        )}
      </div>

      {/* Who and when are one fact, so they share a line — on its own the avatar
          reads as a stray dot and pushes the row half a line taller. */}
      <div className="flex flex-col items-start gap-1.5">
        {published ? <Badge variant="live" dot>Live v{published}</Badge> : <Badge>Draft</Badge>}
        <span className="flex items-center gap-1.5 text-[11.5px] leading-4 text-faint">
          <Creator userId={agent.created_by} members={members} className="relative z-10" />
          Edited {timeAgo(agent.updated_at)}
        </span>
      </div>

      <div className="relative z-10 flex justify-end">
        <Menu
          label={`More actions for ${name}`}
          items={[
            { label: "Copy agent ID", onSelect: onCopyId },
            { label: "Duplicate agent", onSelect: onDuplicate },
            { label: "Delete agent", onSelect: onDelete, danger: true },
          ]}
        />
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const region = useActiveRegion();
  const router = useRouter();
  const [agents, setAgents] = useState<AgentResponse[] | null>(null);
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [stats, setStats] = useState<CallStatsResponse | null>(null);
  const [traffic, setTraffic] = useState<Record<string, { calls: number; spend: number }>>({});
  const [trafficCapped, setTrafficCapped] = useState(false);
  const [numbers, setNumbers] = useState<Record<string, PhoneNumberResponse[]>>({});
  const members = useOrgMembers();
  const [err, setErr] = useState("");

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  /* One name dialog, two ways in: a fresh agent (`source: null`) or a copy of
     an existing one's saved draft. */
  const [naming, setNaming] = useState<{ source: AgentResponse | null } | null>(null);
  const [name, setName] = useState("");
  const [createErr, setCreateErr] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<AgentResponse | null>(null);
  const [deleteErr, setDeleteErr] = useState<{ message: string; errors: string[] } | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function loadAgents() {
    try {
      setAgents(await api.listAllAgents());
    } catch (error) {
      setErr(apiErrorMessage(error));
      setAgents([]);
    }
  }

  useEffect(() => {
    loadAgents();

    // Everything below decorates the list. A failure leaves its column showing
    // "—" rather than taking the page down with it.
    const start = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString();

    api.catalog().then(setCatalog).catch(() => {});
    api.callStats({ start }).then(setStats).catch(() => {});
    api
      .listCalls({ start, limit: CALLS_PAGE })
      .then((page) => {
        const byAgent: Record<string, { calls: number; spend: number }> = {};
        for (const call of page.items) {
          const id = call.agent?.agent_id;
          if (!id) continue;
          const bucket = (byAgent[id] ??= { calls: 0, spend: 0 });
          bucket.calls += 1;
          bucket.spend += call.cost?.total_charge ?? 0;
        }
        setTraffic(byAgent);
        setTrafficCapped(page.has_more);
      })
      .catch(() => {});
    api
      .listPhoneNumbers()
      .then((page) => {
        const byAgent: Record<string, PhoneNumberResponse[]> = {};
        for (const number of page.items) {
          if (!number.inbound_agent_id) continue;
          (byAgent[number.inbound_agent_id] ??= []).push(number);
        }
        setNumbers(byAgent);
      })
      .catch(() => {});
  }, []);

  async function create() {
    const trimmed = name.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setCreateErr("");
    try {
      const source = naming?.source;
      const agent = source
        ? await api.createAgentFromConfig({ ...source.config, name: trimmed })
        : await api.createAgent(trimmed);
      router.push(`/agents/detail?id=${agent.id}`);
    } catch (error) {
      setCreateErr(apiErrorMessage(error));
      setSubmitting(false);
    }
  }

  function closeNaming() {
    if (submitting) return;
    setNaming(null);
    setName("");
    setCreateErr("");
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteErr(null);
    try {
      await api.deleteAgent(deleteTarget.id);
      setDeleteTarget(null);
      await loadAgents();
    } catch (error) {
      setDeleteErr({ message: apiErrorMessage(error), errors: apiErrorList(error) });
    } finally {
      setDeleting(false);
    }
  }

  const liveCount = agents?.filter((agent) => agent.published_version).length ?? 0;
  const draftCount = (agents?.length ?? 0) - liveCount;

  /* Searching and filtering a list you can take in at a glance is chrome, not
     help. The controls appear at the size where scanning starts to cost
     something. */
  const searchable = (agents?.length ?? 0) > 4;

  const visible = useMemo(() => {
    if (!searchable) return agents ?? [];
    const needle = query.trim().toLowerCase();
    return (agents ?? []).filter((agent) => {
      if (filter === "live" && !agent.published_version) return false;
      if (filter === "draft" && agent.published_version) return false;
      if (!needle) return true;
      return (agent.config.name || "").toLowerCase().includes(needle);
    });
  }, [agents, filter, query, searchable]);

  const loading = agents === null;
  const empty = agents !== null && agents.length === 0;

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line py-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="font-display text-[26px] font-semibold leading-8 tracking-tight text-ink">Agents</h1>
            <p className="mt-1.5 text-[14px] leading-5 text-muted">
              Open an agent to edit it, test it, or publish a new version.
            </p>
          </div>
          <Button onClick={() => setNaming({ source: null })} className="min-h-[38px] self-start sm:self-auto">
            {Icon.plus}
            New agent
          </Button>
        </header>

        {err && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {/* The workspace pulse, on one line. Figures are exact for the window;
            the window is named in the strip so nobody has to guess it. Hidden
            before there is anything to have traffic — a row of zeroes above
            "no agents yet" only states the obvious twice. */}
        <section className={cn("mb-5 flex flex-col rounded-xl border border-line-2 bg-white sm:flex-row", empty && "hidden")}>
          <div className="flex flex-none items-center border-b border-line px-4 py-3 sm:border-b-0 sm:border-r sm:px-5">
            <span className="text-[12px] font-semibold uppercase tracking-[0.07em] text-faint">
              Last {WINDOW_DAYS} days
            </span>
          </div>
          <div
            className={cn(
              "grid flex-1 grid-cols-2",
              stats && stats.judged_calls > 0 ? "sm:grid-cols-5" : "sm:grid-cols-4",
            )}
          >
            <Figure label="Calls" value={stats ? stats.total_calls : "—"} />
            <Figure label="Minutes" value={stats ? Math.round(stats.total_minutes) : "—"} />
            <Figure label="Spend" value={stats ? money(stats.total_spend) : "—"} />
            <Figure label="Avg per call" value={stats ? money(stats.avg_cost_per_call, 3) : "—"} />
            {stats && stats.judged_calls > 0 && (
              <Figure
                label="Success rate"
                value={`${Math.round((stats.successful_calls / stats.judged_calls) * 100)}%`}
              />
            )}
          </div>
        </section>

        {loading ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            {[0, 1, 2, 3].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <Skeleton className="h-9 w-9 rounded-[10px]" />
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[26%]" />
                  <Skeleton className="mt-2 h-3 w-[38%]" />
                </div>
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : empty ? (
          <EmptyState
            icon={CHANNEL.voice.glyph}
            title={`No agents in ${region.name}`}
            body="An agent is a prompt, the models that speak it, and the tools it can call. Name one now — everything else is editable afterwards."
            cta={
              <Button onClick={() => setNaming({ source: null })}>
                {Icon.plus}
                Create your first agent
              </Button>
            }
          />
        ) : (
          <>
            {searchable && (
              <div className="mb-3 flex flex-wrap items-center gap-2.5">
                <div className="relative min-w-[180px] flex-1 sm:max-w-[280px]">
                  <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint">
                    {Icon.search}
                  </span>
                  <Input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search agents"
                    aria-label="Search agents by name"
                    className="pl-9"
                  />
                </div>
                <Segment
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "All", count: agents.length },
                    { value: "live", label: "Live", count: liveCount },
                    { value: "draft", label: "Drafts", count: draftCount },
                  ]}
                />
              </div>
            )}

            <section className="min-w-0 overflow-hidden rounded-xl border border-line-2 bg-white">
              {visible.length === 0 ? (
                <div className="px-4 py-12 text-center">
                  <p className="text-[14px] font-medium leading-5 text-ink">No agents match that</p>
                  <p className="mt-1 text-[13px] leading-5 text-muted">
                    Try a different name, or switch the filter back to All.
                  </p>
                </div>
              ) : (
                <>
                  <div
                    className={cn(
                      "hidden gap-x-4 border-b border-line px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
                      ROW_GRID,
                    )}
                  >
                    <span>Agent</span>
                    <span className="hidden lg:block">Phone</span>
                    <span title={trafficCapped ? `Counted from the ${CALLS_PAGE} most recent calls` : undefined}>
                      Calls{trafficCapped && "+"}
                    </span>
                    <span>Status</span>
                    <span className="sr-only">Actions</span>
                  </div>
                  {visible.map((agent) => (
                    <AgentRow
                      key={agent.id}
                      agent={agent}
                      catalog={catalog}
                      numbers={numbers[agent.id] ?? []}
                      traffic={traffic[agent.id] ?? null}
                      members={members}
                      onCopyId={() => void navigator.clipboard.writeText(agent.id)}
                      onDuplicate={() => {
                        setName(`${agent.config.name} (copy)`);
                        setCreateErr("");
                        setNaming({ source: agent });
                      }}
                      onDelete={() => {
                        setDeleteErr(null);
                        setDeleteTarget(agent);
                      }}
                    />
                  ))}
                </>
              )}
            </section>
          </>
        )}

        <div className="mt-5">
          <McpSetup />
        </div>
      </div>

      {naming && (
        <Modal
          title={naming.source ? "Duplicate agent" : "New agent"}
          sub={
            naming.source
              ? "Copies the saved draft. Phone numbers and published versions stay with the original."
              : "Give it the job it does. Models, prompt, voice and tools come next."
          }
          width="max-w-[460px]"
          onClose={closeNaming}
          footer={
            <>
              <Button variant="secondary" onClick={closeNaming} disabled={submitting}>
                Cancel
              </Button>
              <Button onClick={create} disabled={!name.trim() || submitting}>
                {naming.source
                  ? submitting ? "Duplicating…" : "Duplicate agent"
                  : submitting ? "Creating…" : "Create agent"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <Field label="Name" htmlFor="new-agent-name">
              <Input
                id="new-agent-name"
                value={name}
                placeholder="Front desk receptionist"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void create();
                  }
                }}
              />
            </Field>
            {createErr && (
              <p className="mt-2.5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[13px] leading-5 text-danger">
                {createErr}
              </p>
            )}
          </div>
        </Modal>
      )}

      {deleteTarget && (
        <Modal
          title="Delete agent?"
          sub="Removes the draft and every version. Past calls keep their transcripts but lose their prompt, greeting and tools."
          width="max-w-[460px]"
          onClose={() => !deleting && setDeleteTarget(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDelete} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete agent"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="font-display text-[14px] font-semibold leading-5 tracking-tight text-ink">
                {deleteTarget.config.name || "Untitled agent"}
              </div>
              <div className="mt-1 text-[13px] leading-5 text-muted">
                {deleteTarget.published_version
                  ? `Live on v${deleteTarget.published_version}.`
                  : "Never published."}
              </div>
            </div>
            {deleteErr && (
              <div className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                <div className="first-letter:uppercase">{deleteErr.message}</div>
                {deleteErr.errors.length > 0 && (
                  <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-ink">
                    {deleteErr.errors.map((entry) => (
                      <li key={entry}>{entry}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </Modal>
      )}
    </AppShell>
  );
}
