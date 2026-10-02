"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type {
  EmailBatchResponse,
  EmailBatchStatus,
  IntegrationResponse,
  TaskResponse,
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
import { useActiveRegion } from "@/lib/regions";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { inZoneShort, timeAgo } from "@/app/telephony/outbound-calling/shared";
import { BatchProgress } from "./BatchProgress";
import { CreateBatch } from "./CreateBatch";
import { emailAccounts } from "./PolicyFields";
import {
  BATCH_STATUS_LABEL,
  BATCH_STATUS_VARIANT,
  DRAFT_WAIT_REASON,
  isSteerable,
  pausedItself,
  reachable,
} from "./shared";

const PAGE_SIZE = 50;
const SEARCHABLE_FROM = 6;

const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(150px,0.38fr)_minmax(112px,0.24fr)_36px] lg:grid-cols-[minmax(0,1fr)_minmax(170px,0.34fr)_minmax(150px,0.26fr)_minmax(112px,0.22fr)_36px]";

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
  email: (
    <svg className="h-[18px] w-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="5.5" width="18" height="13" rx="2.5" />
      <path d="m3.8 7 7.1 5.3a2 2 0 0 0 2.2 0L20.2 7" />
    </svg>
  ),
};

type Filter = "all" | "live" | "review" | "done";

/** Live means drafting is still moving. `drafted` is deliberately its own
 *  filter: it is not "finished", it is "waiting for you". */
const isLive = (s: EmailBatchStatus) =>
  s === "drafting" || s === "scheduled" || s === "paused";

export default function EmailOutreachPage() {
  const region = useActiveRegion();
  const router = useRouter();
  const toast = useToast();
  const [creating, setCreating] = useState(false);

  const [batches, setBatches] = useState<EmailBatchResponse[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [accounts, setAccounts] = useState<IntegrationResponse[]>([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [err, setErr] = useState("");

  const load = useCallback((quiet = false) => {
    if (!quiet) setBatches(null);
    api
      .listEmailBatches({ limit: PAGE_SIZE })
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

  useEffect(() => {
    Promise.all([api.listTasks(), api.listIntegrations()])
      .then(([taskPage, integrationPage]) => {
        setTasks(taskPage.items);
        setAccounts(emailAccounts(integrationPage.items));
      })
      .catch(() => {});
  }, []);

  // Poll only while something is genuinely moving. A drafted batch has nothing
  // scheduled anywhere — polling it would be asking a question with a fixed
  // answer.
  const anyMoving = (batches ?? []).some(
    (b) => isLive(b.status) || b.counts.queued > 0 || b.counts.sending > 0,
  );
  useEffect(() => {
    if (!anyMoving || creating) return;
    const timer = window.setInterval(() => load(true), 5000);
    return () => window.clearInterval(timer);
  }, [anyMoving, creating, load]);

  function replace(batch: EmailBatchResponse) {
    setBatches((current) => (current ?? []).map((b) => (b.id === batch.id ? batch : b)));
  }

  async function steer(id: string, run: () => Promise<EmailBatchResponse>, said: string) {
    try {
      replace(await run());
      toast({ msg: said, kind: "ok" });
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    }
  }

  async function loadMore() {
    try {
      const page = await api.listEmailBatches({ limit: PAGE_SIZE, offset: (batches ?? []).length });
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
  const blocked = !accounts.length
    ? {
        why: "Connect a Resend account under Integrations before you can send a batch.",
        body:
          "A batch sends on your own Resend account. Connect one under Integrations first — and read their terms while you are there: they prohibit cold outreach, purchased lists and scraped contacts.",
        href: "/integrations",
        cta: "Connect Resend",
      }
    : !tasks.length
      ? {
          why: "Build an agent task under Tasks before you can send a batch.",
          body:
            "Every row is written by an agent task — the same prompt, model and tools an agent has, producing a typed result instead of a conversation. Build one under Tasks first.",
          href: "/tasks",
          cta: "Create a task",
        }
      : null;

  const all = batches ?? [];
  const liveCount = all.filter((b) => isLive(b.status)).length;
  const reviewCount = all.filter((b) => b.status === "drafted" && b.counts.draft > 0).length;
  const needle = query.trim().toLowerCase();
  const visible = all.filter((b) => {
    if (filter === "live" && !isLive(b.status)) return false;
    if (filter === "review" && !(b.status === "drafted" && b.counts.draft > 0)) return false;
    if (filter === "done" && (isLive(b.status) || (b.status === "drafted" && b.counts.draft > 0)))
      return false;
    if (!needle) return true;
    return (
      b.name.toLowerCase().includes(needle) ||
      (b.task_name ?? "").toLowerCase().includes(needle) ||
      b.from_email.toLowerCase().includes(needle)
    );
  });

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Email"
          sub="Run an agent task over a list, read every draft it writes, and send the ones you pick — on a schedule and a pace you set."
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
            icon={Icon.email}
            title={`No email batches in ${region.name}`}
            body={
              blocked?.body ??
              "Upload a list, pick the task that writes each email, and read what it produced before anything goes out."
            }
            /* Telling someone to go build a task and then handing them nothing
               to click is the one place a CTA is most needed, not least. */
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
                    aria-label="Search batches by name, task or sender"
                    className="pl-9"
                  />
                </div>
                <Segment
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "All", count: all.length },
                    { value: "live", label: "Drafting", count: liveCount },
                    { value: "review", label: "To review", count: reviewCount },
                    {
                      value: "done",
                      label: "Done",
                      count: all.length - liveCount - reviewCount,
                    },
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
                    <span>Drafted · sent</span>
                    <span className="hidden lg:block">Schedule</span>
                    <span>Status</span>
                    <span className="sr-only">Actions</span>
                  </div>
                  {visible.map((batch) => (
                    <BatchRow
                      key={batch.id}
                      batch={batch}
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
          tasks={tasks}
          accounts={accounts}
          onCancel={() => setCreating(false)}
          onCreated={(batch) => {
            setCreating(false);
            // Straight into the batch that was just made. There is nothing to
            // do on this page for a list that has drafted nothing yet.
            router.push(`/email/outbound/detail?id=${batch.id}`);
          }}
        />
      )}
    </AppShell>
  );
}

function BatchRow({
  batch,
  onCopyId,
  onSteer,
}: {
  batch: EmailBatchResponse;
  onCopyId: () => void;
  onSteer: (id: string, run: () => Promise<EmailBatchResponse>, said: string) => void;
}) {
  const schedule = batch.next_draft_at
    ? {
        line: `Waits until ${inZoneShort(batch.next_draft_at, batch.timezone)}`,
        under: batch.next_draft_reason
          ? DRAFT_WAIT_REASON[batch.next_draft_reason]
          : batch.timezone.replace(/_/g, " "),
      }
    : batch.drafted_at
      ? { line: `Drafted ${timeAgo(batch.drafted_at)}`, under: `${batch.draft_concurrency} at a time` }
      : batch.started_at
        ? {
            line: `Started ${timeAgo(batch.started_at)}`,
            under: `${batch.draft_concurrency} at a time`,
          }
        : { line: "Not started", under: `${batch.draft_concurrency} at a time` };

  return (
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2.5 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      {/* A real link, so the row opens in a new tab, can be copied, and shows
          its destination in the status bar. The batch is a page now. */}
      <Link
        href={`/email/outbound/detail?id=${batch.id}`}
        aria-label={`Open ${batch.name}`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />

      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          {Icon.email}
        </span>
        <div className="min-w-0">
          <div className="truncate font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
            {batch.name}
          </div>
          {pausedItself(batch) ? (
            /* A breaker pause. Resumable, and nothing was lost — the badge on
               its own reads exactly like a pause somebody chose. */
            <div className="mt-0.5 truncate text-[12px] leading-4 text-warn-ink">
              Paused itself — {batch.failure_reason}
            </div>
          ) : batch.status === "failed" && batch.failure_reason ? (
            <div className="mt-0.5 truncate text-[12px] leading-4 text-danger">
              Stopped — {batch.failure_reason}
            </div>
          ) : (
            <div className="mt-0.5 flex min-w-0 items-center gap-2 overflow-hidden text-[12px] leading-4 text-muted">
              <span className="truncate">{batch.task_name ?? "Task deleted"}</span>
              <span aria-hidden className="text-line-strong">·</span>
              <span className="truncate font-mono text-[11.5px]">{batch.from_email}</span>
            </div>
          )}
        </div>
      </div>

      <div className="hidden sm:block">
        <BatchProgress batch={batch} compact />
        <div className="mt-1.5 text-[11.5px] leading-4 text-faint tabular-nums">
          {/* Two numbers, never one. A batch whose 4 000 drafts are all
              unsent has done a lot and sent nothing, and that is exactly what
              somebody scanning this page needs to see. */}
          {batch.counts.draft.toLocaleString()} to review ·{" "}
          {batch.counts.sent.toLocaleString()} sent of{" "}
          {reachable(batch).sending.toLocaleString()}
        </div>
      </div>

      <div className="hidden lg:block">
        <div className="truncate text-[13px] leading-5 text-ink-soft">{schedule.line}</div>
        <div className="truncate text-[11.5px] leading-4 text-faint">{schedule.under}</div>
      </div>

      <div className="flex flex-col items-start gap-1.5">
        <Badge variant={BATCH_STATUS_VARIANT[batch.status]} dot={batch.status === "drafting"}>
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
            { label: "Copy batch ID", onSelect: onCopyId },
            ...(isSteerable(batch.status)
              ? batch.status === "paused"
                ? [
                    {
                      label: "Resume",
                      onSelect: () =>
                        onSteer(batch.id, () => api.resumeEmailBatch(batch.id), "Drafting again."),
                    },
                  ]
                : [
                    {
                      label: "Pause",
                      onSelect: () =>
                        onSteer(
                          batch.id,
                          () => api.pauseEmailBatch(batch.id),
                          "Paused. Rows already drafting will finish.",
                        ),
                    },
                  ]
              : []),
            // No send action here at all, and no cancel: both belong on the
            // batch's own page, where the drafts and the consequences are on
            // screen. Sending is never one click from a list.
          ]}
        />
      </div>
    </div>
  );
}

