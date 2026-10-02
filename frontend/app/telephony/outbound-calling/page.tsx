"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import type {
  AgentResponse,
  CallBatchResponse,
  CallBatchStatus,
  PhoneNumberResponse,
} from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import {
  Badge,
  btn,
  Button,
  Container,
  EmptyState,
  Input,
  Menu,
  PageHead,
  Segment,
  Skeleton,
  Tooltip,
  useToast,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { outboundNumbers } from "./PolicyFields";
import { CreateBatch } from "./CreateBatch";
import { BatchDetail } from "./BatchDetail";
import { BatchProgress } from "./BatchProgress";
import {
  BATCH_STATUS_LABEL,
  BATCH_STATUS_VARIANT,
  inZoneShort,
  isCancelable,
  isSteerable,
  timeAgo,
} from "./shared";

const PAGE_SIZE = 50;
/* Search appears once a list is long enough to need it — the same threshold the
   agents page uses, for the same reason: a search box over four rows is furniture. */
const SEARCHABLE_FROM = 6;

const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(120px,0.3fr)_minmax(112px,0.24fr)_36px] lg:grid-cols-[minmax(0,1fr)_minmax(130px,0.26fr)_minmax(150px,0.28fr)_minmax(112px,0.22fr)_36px]";

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
  outbound: (
    <svg className="h-[18px] w-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M4 8.5C4 7 5 6 6.5 6H8l1.5 4L8 11.5a11 11 0 0 0 6.5 6.5L16 16.5 20 18v1.5c0 1.5-1 2.5-2.5 2.5A13.5 13.5 0 0 1 4 8.5Z" />
      <path d="M15 8.5h6m0 0-2.5-2.5M21 8.5 18.5 11" />
    </svg>
  ),
};

type Filter = "all" | "live" | "done";

/** Live means "the dispatcher still has business with it", which is what an
 *  operator scanning this page is looking for. A paused batch is live: somebody
 *  has to decide about it. */
const isLive = (s: CallBatchStatus) => s === "running" || s === "scheduled" || s === "paused";

function OutboundCallingInner() {
  const region = useActiveRegion();
  // Query params, not path segments: the dashboard ships as a static export and
  // cannot prerender an unbounded set of batch ids.
  const params = useSearchParams();
  const toast = useToast();
  // The create dialog gets no query param of its own: `?id=` names a batch that
  // exists and is worth linking to, whereas restoring "the new-batch form was
  // open" restores an empty form — the same thing the button one click away
  // gives you, and not something anyone means to send to someone else.
  const [creating, setCreating] = useState(false);
  const [selectedId, setSelectedId] = useState(params.get("id") ?? "");

  const [batches, setBatches] = useState<CallBatchResponse[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumberResponse[]>([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [err, setErr] = useState("");

  const load = useCallback((quiet = false) => {
    if (!quiet) setBatches(null);
    api
      .listCallBatches({ limit: PAGE_SIZE })
      .then((page) => {
        setBatches(page.items);
        setHasMore(page.has_more);
        setErr("");
      })
      .catch((e) => {
        setBatches([]);
        setErr(apiErrorMessage(e, "Could not load your batches."));
      });
  }, []);

  useEffect(() => load(), [load]);

  // Everything a batch can be pointed at. Loaded once for the page so the create
  // panel and the edit dialog agree on the choices without fetching twice.
  useEffect(() => {
    Promise.all([
      api.listAllAgents(),
      api.listPhoneNumbers({ limit: 200 }),
      api.listTelephonyAccounts({ limit: 200 }),
    ])
      .then(([allAgents, numberPage, accountPage]) => {
        setAgents(
          (allAgents as AgentResponse[]).filter(
            (a) => a.published_version != null && a.config?.channel === "voice",
          ),
        );
        setNumbers(outboundNumbers(numberPage.items, accountPage.items));
      })
      .catch(() => {});
  }, []);

  // A batch that is dialling changes under the reader. Poll only while one is,
  // and stop as soon as none is — a page of finished batches is a record.
  const anyRunning = (batches ?? []).some((b) => b.status === "running" || b.status === "scheduled");
  useEffect(() => {
    if (!anyRunning || creating) return;
    const timer = window.setInterval(() => load(true), 5000);
    return () => window.clearInterval(timer);
  }, [anyRunning, creating, load]);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (selectedId) url.searchParams.set("id", selectedId);
    else url.searchParams.delete("id");
    window.history.replaceState(null, "", url);
  }, [selectedId]);

  const selected = useMemo(
    () => (batches ?? []).find((b) => b.id === selectedId) ?? null,
    [batches, selectedId],
  );

  // A deep link may name a batch outside the first page. Fetch it by id rather
  // than pretending the list is complete — the same reason /calls does.
  useEffect(() => {
    if (!selectedId || selected || batches === null) return;
    let stale = false;
    api
      .getCallBatch(selectedId)
      .then((batch) => {
        if (!stale) setBatches((current) => [batch, ...(current ?? []).filter((b) => b.id !== batch.id)]);
      })
      .catch(() => {
        if (!stale) setSelectedId("");
      });
    return () => {
      stale = true;
    };
  }, [selectedId, selected, batches]);

  function replace(batch: CallBatchResponse) {
    setBatches((current) => (current ?? []).map((b) => (b.id === batch.id ? batch : b)));
  }

  async function steer(id: string, run: () => Promise<CallBatchResponse>, said: string) {
    try {
      replace(await run());
      toast({ msg: said, kind: "ok" });
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    }
  }

  async function loadMore() {
    try {
      const page = await api.listCallBatches({ limit: PAGE_SIZE, offset: (batches ?? []).length });
      setBatches((current) => [...(current ?? []), ...page.items]);
      setHasMore(page.has_more);
    } catch (e: unknown) {
      setErr(apiErrorMessage(e, "Could not load more batches."));
    }
  }

  /* A batch needs two things this workspace may not have yet. Both the greyed
     "New batch" button and the empty state read from here, so the page can
     never disable an action without saying which one is missing and where to
     go and get it — the empty state only renders on a workspace with no
     batches at all, and the button outlives it. */
  const blocked = !numbers.length
    ? {
        why: "Set up a number that can dial out, under Phone Numbers, before you can run a batch.",
        body:
          "A batch dials from one of your own numbers, and none of them can call out yet. Set one up under Phone Numbers first.",
        href: "/telephony/phone-numbers",
        cta: "Set up a number",
      }
    : !agents.length
      ? {
          why: "Publish a voice agent before you can run a batch.",
          body:
            "A batch runs whichever version of an agent is published when it dials, so publish a voice agent first.",
          href: "/agents",
          cta: "Publish an agent",
        }
      : null;

  const all = batches ?? [];
  const liveCount = all.filter((b) => isLive(b.status)).length;
  const needle = query.trim().toLowerCase();
  const visible = all.filter((b) => {
    if (filter === "live" && !isLive(b.status)) return false;
    if (filter === "done" && isLive(b.status)) return false;
    if (!needle) return true;
    return (
      b.name.toLowerCase().includes(needle) ||
      (b.agent_name ?? "").toLowerCase().includes(needle) ||
      (b.from_e164 ?? "").includes(needle)
    );
  });

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Outbound Calling"
          sub="Give an agent a list of people and a time, and it works through them — with every call, transcript and cost kept."
          actions={
            /* A disabled Button sets pointer-events:none, so its own title
               attribute never fires — the wrapper is what hears the hover. */
            blocked ? (
              <Tooltip label={blocked.why} className="cursor-not-allowed">
                <Button disabled>
                  {Icon.plus}
                  New batch
                </Button>
              </Tooltip>
            ) : (
              <Button onClick={() => setCreating(true)}>
                {Icon.plus}
                New batch
              </Button>
            )
          }
        />

        {err && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {batches === null ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            {[0, 1, 2, 3].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <Skeleton className="h-9 w-9 rounded-[10px]" />
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[24%]" />
                  <Skeleton className="mt-2 h-3 w-[36%]" />
                </div>
                <Skeleton className="h-1.5 w-32 rounded-full" />
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : all.length === 0 ? (
          <EmptyState
            icon={Icon.outbound}
            title={`No call batches in ${region.name}`}
            body={
              blocked?.body ??
              "Upload a list of people, pick an agent and a number, and say when it may start calling."
            }
            /* Telling someone to go set a number up and then handing them
               nothing to click is the one place a CTA is most needed, not
               least. */
            cta={
              blocked ? (
                <Link href={blocked.href} className={btn("primary", "sm")}>
                  {blocked.cta}
                </Link>
              ) : (
                <Button onClick={() => setCreating(true)}>
                  {Icon.plus}
                  Create your first batch
                </Button>
              )
            }
          />
        ) : (
          <>
            {all.length >= SEARCHABLE_FROM && (
              <div className="mb-3 flex flex-wrap items-center gap-2.5">
                <div className="relative min-w-[180px] flex-1 sm:max-w-[280px]">
                  <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint">
                    {Icon.search}
                  </span>
                  <Input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search batches"
                    aria-label="Search batches by name, agent or number"
                    className="pl-9"
                  />
                </div>
                <Segment
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "All", count: all.length },
                    { value: "live", label: "Live", count: liveCount },
                    { value: "done", label: "Finished", count: all.length - liveCount },
                  ]}
                />
              </div>
            )}

            <section className="min-w-0 overflow-hidden rounded-xl border border-line-2 bg-white">
              {visible.length === 0 ? (
                <div className="px-4 py-12 text-center">
                  <p className="text-[14px] font-medium leading-5 text-ink">No batches match that</p>
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
                    <span>Batch</span>
                    <span>Progress</span>
                    <span className="hidden lg:block">Schedule</span>
                    <span>Status</span>
                    <span className="sr-only">Actions</span>
                  </div>
                  {visible.map((batch) => (
                    <BatchRow
                      key={batch.id}
                      batch={batch}
                      onOpen={() => setSelectedId(batch.id)}
                      onCopyId={() => {
                        void navigator.clipboard.writeText(batch.id);
                        toast({ msg: "Batch ID copied", kind: "ok" });
                      }}
                      onSteer={steer}
                    />
                  ))}
                </>
              )}
            </section>

            {hasMore && (
              <Button variant="secondary" onClick={loadMore} className="mt-3">
                Load more
              </Button>
            )}
          </>
        )}
      </Container>

      {creating && (
        <CreateBatch
          agents={agents}
          numbers={numbers}
          onCancel={() => setCreating(false)}
          onCreated={(batch) => {
            setBatches((current) => [batch, ...(current ?? [])]);
            setCreating(false);
            setSelectedId(batch.id);
          }}
        />
      )}

      {selected && (
        <BatchDetail
          batch={selected}
          agents={agents}
          numbers={numbers}
          onChanged={replace}
          onDeleted={() => {
            setBatches((current) => (current ?? []).filter((b) => b.id !== selected.id));
            setSelectedId("");
          }}
          onClose={() => setSelectedId("")}
        />
      )}
    </AppShell>
  );
}

function BatchRow({
  batch,
  onOpen,
  onCopyId,
  onSteer,
}: {
  batch: CallBatchResponse;
  onOpen: () => void;
  onCopyId: () => void;
  onSteer: (id: string, run: () => Promise<CallBatchResponse>, said: string) => void;
}) {
  const called = batch.counts.completed + batch.counts.failed;
  /* A time still ahead of the batch is shown on the batch's clock and says which
     one; a time behind it is shown as "4m ago", where a zone would be noise. */
  const waitingFor = batch.status === "scheduled" ? batch.start_at : batch.next_dial_at;
  const schedule = waitingFor
    ? {
        line: `Waits until ${inZoneShort(waitingFor, batch.timezone)}`,
        under:
          batch.next_dial_reason === "daily_cap"
            ? "daily limit reached"
            : batch.timezone.replace(/_/g, " "),
      }
    : batch.ended_at
      ? { line: `Ended ${timeAgo(batch.ended_at)}`, under: `${batch.max_concurrency} at a time` }
      : batch.started_at
        ? {
            line: `Started ${timeAgo(batch.started_at)}`,
            under: `${batch.max_concurrency} at a time`,
          }
        : { line: "Not started", under: `${batch.max_concurrency} at a time` };

  return (
    /* A div with a stretched link rather than a link wrapping everything, so the
       overflow menu inside it is not a button inside an anchor. */
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2.5 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      <button
        type="button"
        onClick={onOpen}
        aria-label={`Open ${batch.name}`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />

      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          {Icon.outbound}
        </span>
        <div className="min-w-0">
          <div className="truncate font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
            {batch.name}
          </div>
          {/* When a batch has stopped itself, why is the most important thing the
              row can say — so it takes the meta line rather than sharing it. */}
          {batch.status === "failed" && batch.failure_reason ? (
            <div className="mt-0.5 truncate text-[12px] leading-4 text-danger">
              Stopped after ten failures in a row — {batch.failure_reason.toLowerCase()}
            </div>
          ) : (
            <div className="mt-0.5 flex min-w-0 items-center gap-2 overflow-hidden text-[12px] leading-4 text-muted">
              <span className="truncate">{batch.agent_name ?? "Agent deleted"}</span>
              <span aria-hidden className="text-line-strong">·</span>
              <span className="flex-none font-mono text-[11.5px]">
                {batch.from_e164 ?? "number deleted"}
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="hidden sm:block">
        <BatchProgress batch={batch} compact />
        <div className="mt-1.5 text-[11.5px] leading-4 text-faint tabular-nums">
          {called.toLocaleString()} of {batch.counts.total.toLocaleString()} called
        </div>
      </div>

      <div className="hidden lg:block">
        <div className="truncate text-[13px] leading-5 text-ink-soft">{schedule.line}</div>
        <div className="truncate text-[11.5px] leading-4 text-faint">{schedule.under}</div>
      </div>

      <div className="flex flex-col items-start gap-1.5">
        <Badge variant={BATCH_STATUS_VARIANT[batch.status]} dot={batch.status === "running"}>
          {BATCH_STATUS_LABEL[batch.status]}
        </Badge>
        <span className="text-[11.5px] leading-4 text-faint">
          Created {timeAgo(batch.created_at)}
        </span>
      </div>

      <div className="relative z-10 flex justify-end">
        <Menu
          label={`More actions for ${batch.name}`}
          items={[
            { label: "See the calls", onSelect: () => window.location.assign(`/calls?batch_id=${batch.id}`) },
            { label: "Copy batch ID", onSelect: onCopyId },
            ...(isSteerable(batch.status)
              ? batch.status === "paused"
                ? [
                    {
                      label: "Resume",
                      onSelect: () =>
                        onSteer(batch.id, () => api.resumeCallBatch(batch.id), "Running again."),
                    },
                  ]
                : [
                    {
                      label: "Pause",
                      onSelect: () =>
                        onSteer(
                          batch.id,
                          () => api.pauseCallBatch(batch.id),
                          batch.status === "completed"
                            ? "Paused. Anyone you add waits until you resume."
                            : "Paused. Calls already in progress will finish.",
                        ),
                    },
                  ]
              : []),
            // Cancel confirms in the batch's own dialog, which is where the
            // consequences are spelled out. A one-click cancel from a row menu
            // would be the most expensive misclick on the platform.
            ...(isCancelable(batch.status)
              ? [{ label: "Cancel batch…", onSelect: onOpen, danger: true }]
              : []),
          ]}
        />
      </div>
    </div>
  );
}

export default function OutboundCallingPage() {
  return (
    <Suspense fallback={null}>
      <OutboundCallingInner />
    </Suspense>
  );
}
