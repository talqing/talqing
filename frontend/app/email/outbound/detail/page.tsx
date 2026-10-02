"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import type {
  EmailBatchResponse,
  EmailRecipientResponse,
  EmailRecipientStatus,
  EmailSendResponse,
  IntegrationResponse,
  TaskResponse,
} from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { AddRecipientsModal } from "@/app/components/AddRecipientsModal";
import { DeleteBatchModal } from "@/app/components/DeleteBatchModal";
import {
  Badge,
  Button,
  Container,
  DescriptionItem,
  Input,
  ListSkeleton,
  Menu,
  Modal,
  Panel,
  Pager,
  FigureTabs,
  Skeleton,
  useToast,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { usedToday } from "@/app/components/Pacing";
import { hhmm, inZone, timeAgo, zoneAbbreviation } from "@/app/telephony/outbound-calling/shared";
import { BatchProgress } from "../BatchProgress";
import { LIST_COPY } from "../CreateBatch";
import { EMPTY_LIST, type ParsedList, batchCsv, type ExportRow } from "../csv";
import { RecipientEditor } from "../RecipientEditor";
import { EditPolicy } from "../EditPolicy";
import { RedraftDialog, type RedraftKind } from "../RedraftDialog";
import { ReviewTable } from "../ReviewTable";
import { RowPreview } from "../RowPreview";
import { SendDialog, type SendSelection } from "../SendDialog";
import { SendsSection } from "../SendsSection";
import { TraceModal } from "../TraceModal";
import { emailAccounts } from "../PolicyFields";
import {
  BATCH_STATUS_LABEL,
  BATCH_STATUS_VARIANT,
  DRAFT_WAIT_REASON,
  coversRows,
  isDraftingOn,
  isSendableRow,
  isSteerable,
  paceSentence,
  pausedItself,
  rowError,
  unwrittenColumns,
} from "../shared";

/* One batch, as a page.
 *
 * It was a modal, and a modal was the wrong container the moment sending became
 * a thing with a lifecycle: the review table wants a sticky header and room to
 * search, and the sends list has nowhere to live inside a dialog. Reached as
 * `/email/outbound/detail?id=…` because the static export forbids dynamic route
 * segments — `next.config.js` explains why, and agents, tasks and tools all
 * already use this shape.
 *
 * The order down the page is the order the work happens in: what this batch is,
 * how far it has got, what has been sent, and then the drafts themselves. */

const PAGE_SIZE = 50;
// Sends are their own page, because they are their own list with their own
// length: a batch reviewed one row at a time has one send per email.
const SENDS_PAGE_SIZE = 20;

const TABS: { value: EmailRecipientStatus | "all"; label: string }[] = [
  { value: "all", label: "Everyone" },
  { value: "draft", label: "Drafts" },
  { value: "pending", label: "Not drafted" },
  { value: "draft_failed", label: "Draft failed" },
  { value: "skipped", label: "Skipped" },
  { value: "queued", label: "Queued" },
  { value: "sent", label: "Sent" },
  { value: "send_failed", label: "Send failed" },
];

function BatchDetailInner() {
  const params = useSearchParams();
  const batchId = params.get("id") ?? "";
  const router = useRouter();
  const toast = useToast();

  const [batch, setBatch] = useState<EmailBatchResponse | null>(null);
  const [sends, setSends] = useState<EmailSendResponse[] | null>(null);
  /* "Send this one" in the row preview creates a send per email, so an operator
     who reviews one at a time passes fifty within an hour — and the oldest, live
     ones used to drop off the only place they can be paused or cancelled. */
  const [sendPage, setSendPage] = useState(0);
  const [moreSends, setMoreSends] = useState(false);
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [accounts, setAccounts] = useState<IntegrationResponse[]>([]);

  const [tab, setTab] = useState<EmailRecipientStatus | "all">("all");
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<EmailRecipientResponse[]>([]);
  const [page, setPage] = useState(0);
  const [loadingRows, setLoadingRows] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [sending, setSending] = useState<SendSelection | null>(null);
  const [redrafting, setRedrafting] = useState<RedraftKind | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [tracing, setTracing] = useState<EmailRecipientResponse | null>(null);
  const [previewing, setPreviewing] = useState<string | null>(null);
  // Rows fetched so far while an export runs; null when none is running.
  const [exported, setExported] = useState<number | null>(null);

  useEffect(() => {
    setPage(0);
    setSelected(new Set());
  }, [tab]);

  const loadBatch = useCallback(() => {
    if (!batchId) return;
    api
      .getEmailBatch(batchId)
      .then(setBatch)
      .catch((e) => setErr(apiErrorMessage(e, "Could not load this batch.")));
    api
      .listEmailSends(batchId, { limit: SENDS_PAGE_SIZE, offset: sendPage * SENDS_PAGE_SIZE })
      .then((p) => {
        setSends(p.items);
        setMoreSends(p.has_more);
      })
      .catch(() => setSends([]));
  }, [batchId, sendPage]);

  const loadRows = useCallback(() => {
    if (!batchId) return;
    setLoadingRows(true);
    api
      .listEmailBatchRecipients(batchId, {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        status: tab === "all" ? undefined : tab,
      })
      .then((res) => setRows(res.items))
      .catch((e) => setErr(apiErrorMessage(e, "Could not load this batch's rows.")))
      .finally(() => setLoadingRows(false));
  }, [batchId, page, tab]);

  // Rows leave a tab as they are drafted and sent, which can empty its last page.
  const rowTotal = batch ? (tab === "all" ? batch.counts.total : batch.counts[tab]) : 0;
  const lastPage = Math.max(Math.ceil(rowTotal / PAGE_SIZE) - 1, 0);
  useEffect(() => {
    if (page > lastPage) setPage(lastPage);
  }, [page, lastPage]);

  useEffect(() => loadBatch(), [loadBatch]);
  useEffect(() => loadRows(), [loadRows]);

  useEffect(() => {
    Promise.all([api.listTasks(), api.listIntegrations()])
      .then(([taskPage, integrationPage]) => {
        setTasks(taskPage.items);
        setAccounts(emailAccounts(integrationPage.items));
      })
      .catch(() => {});
  }, []);

  // A search over the merged column space, on the page in front of you. Not a
  // server query: the row's columns are a per-batch bag with no index, and a
  // "search" that silently only matched three of them would be worse than none.
  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () =>
      needle
        ? rows.filter((r) =>
            Object.values(r.columns).some(
              (v) => v != null && String(v).toLowerCase().includes(needle),
            ),
          )
        : rows,
    [rows, needle],
  );

  const previewIndex = visible.findIndex((r) => r.id === previewing);
  const zone = useMemo(
    () => (batch ? zoneAbbreviation(batch.start_at, batch.timezone) : ""),
    [batch],
  );

  /* How many rows the button would actually rewrite — not how many an older
     version wrote, which stays true for ever once anything is sent and left the
     banner up permanently offering to redraft nothing. */
  const staleRows = batch?.stale_redraftable ?? 0;

  /* A canceled batch can never draft again, and redrafting one used to DESTROY
     its drafts: the rows reset, the batch stayed canceled, and nothing could
     bring them back. The API refuses it now; the page must not offer it. */
  const canRedraft = !!batch && batch.status !== "canceled";

  async function steer(kind: "pause" | "resume" | "cancel", run: () => Promise<EmailBatchResponse>) {
    setBusy(true);
    try {
      setBatch(await run());
      toast({
        msg:
          kind === "pause"
            ? batch?.status === "drafted"
              ? "Paused. Rows you add wait until you resume drafting."
              : "Paused. Rows already drafting will finish."
            : kind === "resume"
              ? "Drafting again."
              : "Stopped drafting. The drafts already made are still yours to send.",
        kind: "ok",
      });
      setConfirmCancel(false);
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  }

  async function act(verb: "skip" | "restore", run: () => Promise<{ affected: number }>) {
    setBusy(true);
    try {
      const res = await run();
      toast({
        msg: `${res.affected.toLocaleString()} ${res.affected === 1 ? "row" : "rows"} ${
          verb === "skip" ? "skipped" : "restored"
        }.`,
        kind: "ok",
      });
      setSelected(new Set());
      loadBatch();
      loadRows();
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  }

  /** Every row of the batch as one CSV download, whichever tab is showing.
   *
   *  Page after page rather than all at once: 10,000 rows is 50 requests and a
   *  few seconds, not worth 50 in flight together. No `status` filter, which is
   *  what keeps the offsets stable while the batch drafts or sends underneath —
   *  the list is in row order and a batch never gains or loses a row. A page
   *  that fails downloads nothing, never a file that is quietly short. */
  async function exportCsv(target: EmailBatchResponse) {
    setExported(0);
    try {
      const kept: ExportRow[] = [];
      let more = true;
      while (more) {
        const res = await api.listEmailBatchRecipients(target.id, {
          limit: 200,
          offset: kept.length,
        });
        for (const row of res.items) {
          kept.push({ columns: row.columns, status: row.status, error: rowError(row) });
        }
        more = res.has_more;
        setExported(kept.length);
      }
      const url = URL.createObjectURL(
        new Blob(["\uFEFF", batchCsv(target, kept)], { type: "text/csv;charset=utf-8" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `${target.name}-export.csv`;
      link.click();
      // Not revoked in this tick, which can cancel the download in some browsers.
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e, "Could not export this batch."), kind: "err" });
    } finally {
      setExported(null);
    }
  }

  /** Write one row's overrides and put the fresh row back on the page.
   *
   *  Throws, deliberately: the table swallows the failure into a toast because
   *  the edit was a blur nobody is waiting on, and the preview shows it beside
   *  the Save button somebody is. */
  async function saveCells(row: EmailRecipientResponse, overrides: Record<string, string>) {
    const next = await api.patchEmailBatchRecipient(batchId, row.id, { overrides });
    setRows((current) => current.map((r) => (r.id === next.id ? next : r)));
  }

  async function editCell(row: EmailRecipientResponse, column: string, value: string) {
    try {
      await saveCells(row, { [column]: value });
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
      loadRows();
    }
  }

  if (!batchId) {
    return (
      <AppShell>
        <Container className="min-h-screen">
          <p className="mt-16 text-center text-[14px] text-muted">
            No batch named. <Link href="/email/outbound" className="underline">Back to Email</Link>.
          </p>
        </Container>
      </AppShell>
    );
  }

  if (!batch) {
    return (
      <AppShell>
        <Container className="min-h-screen">
          <div className="grid gap-4 pt-8">
            <Skeleton className="h-7 w-[28%]" />
            <Skeleton className="h-3 w-[46%]" />
            <ListSkeleton rows={6} />
          </div>
          {err && (
            <p className="mt-4 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
              {err}
            </p>
          )}
        </Container>
      </AppShell>
    );
  }

  /* The live sends on the page in front of you. The API is the authority on
     all of this and refuses with a sentence; these only keep the page from
     offering what it would refuse. */
  const liveSends = (sends ?? []).filter((s) => coversRows(s.status));
  const standingLive = liveSends.some((s) => s.scope === "all");
  const draftingOn = isDraftingOn(batch.status);
  const sendBlocked = standingLive
    ? "A send of every row is live, and it already covers every draft."
    : batch.counts.draft > 0 || (draftingOn && liveSends.length === 0)
      ? null
      : draftingOn
        ? "Nothing has finished drafting, and a send of every row cannot start beside another send."
        : "Nothing is waiting to be sent.";
  // The send a new one replaces: the newest, if somebody stopped it. Its pace,
  // hours and cap are what the operator last chose — the batch's may not be.
  const newest = sendPage === 0 ? sends?.[0] : undefined;
  const prior = newest && (newest.status === "canceled" || newest.status === "failed") ? newest : null;

  const identityEditable =
    liveSends.length === 0 &&
    batch.counts.sending + batch.counts.sent + batch.counts.send_failed === 0;
  const sendableOnPage = visible.filter(isSendableRow);
  const unsent = batch.counts.draft + batch.counts.queued + batch.counts.draft_failed;

  return (
    <AppShell>
      <div className="min-h-screen bg-surface text-ink">
        <Container className="min-h-screen">
          <div className="sticky top-0 z-30 -mx-6 border-b border-line bg-surface/90 px-6 backdrop-blur-md">
            <div className="flex min-h-[56px] flex-wrap items-center gap-x-3 gap-y-2 py-2.5">
              <Link
                href="/email/outbound"
                className="-ml-1.5 flex h-8 flex-none items-center gap-1 rounded-lg px-1.5 text-[13px] font-medium text-muted transition-colors hover:bg-hover hover:text-ink"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M14 6l-6 6 6 6" />
                </svg>
                Email
              </Link>
              <span aria-hidden className="h-4 w-px flex-none bg-line-2" />
              <h1 className="min-w-0 truncate font-display text-[19px] font-semibold leading-7 tracking-[-0.015em] text-ink">
                {batch.name}
              </h1>
              <Badge variant={BATCH_STATUS_VARIANT[batch.status]} dot={batch.status === "drafting"}>
                {BATCH_STATUS_LABEL[batch.status]}
              </Badge>
              <div className="ml-auto flex flex-none items-center gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={exported !== null}
                  onClick={() => exportCsv(batch)}
                  className="tabular-nums"
                >
                  {exported === null
                    ? "Export CSV"
                    : `Exporting… ${Math.floor((exported / batch.counts.total) * 100)}%`}
                </Button>
                {/* A canceled or failed batch drafts nothing new, and neither does
                    one whose task is gone. */}
                {batch.status !== "canceled" && batch.status !== "failed" && batch.task_id && (
                  <Button variant="secondary" size="sm" onClick={() => setAdding(true)}>
                    Add recipients
                  </Button>
                )}
                <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                {/* `drafted` pauses too: rows added later then wait for a resume. */}
                {(isSteerable(batch.status) || batch.status === "drafted") &&
                  (batch.status === "paused" ? (
                    <Button
                      size="sm"
                      disabled={busy}
                      onClick={() => steer("resume", () => api.resumeEmailBatch(batch.id))}
                    >
                      Resume drafting
                    </Button>
                  ) : (
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      onClick={() => steer("pause", () => api.pauseEmailBatch(batch.id))}
                    >
                      Pause drafting
                    </Button>
                  ))}
                <Menu
                  label={`More actions for ${batch.name}`}
                  items={[
                    {
                      label: "Copy batch ID",
                      onSelect: () => {
                        void navigator.clipboard.writeText(batch.id);
                        toast({ msg: "Batch ID copied", kind: "ok" });
                      },
                    },
                    ...(isSteerable(batch.status)
                      ? [
                          {
                            label: "Stop drafting…",
                            onSelect: () => setConfirmCancel(true),
                            danger: true,
                          },
                        ]
                      : [
                          {
                            label: "Delete batch…",
                            onSelect: () => setConfirmDelete(true),
                            danger: true,
                            // A send still holding rows has to end first.
                            disabled: liveSends.length > 0,
                          },
                        ]),
                  ]}
                />
              </div>
            </div>
          </div>

          <div className="grid min-w-0 gap-6 pt-5">
            <Overview batch={batch} zone={zone} live={liveSends[0]} />

            {batch.status === "failed" && batch.failure_reason && (
              <div className="rounded-xl border border-danger/25 bg-danger/[0.05] px-4 py-3 text-[13px] leading-5 text-danger">
                <strong className="font-semibold">Drafting stopped itself.</strong>{" "}
                {batch.failure_reason} Any send already going out is unaffected.
              </div>
            )}

            {/* The circuit breaker, which pauses rather than stops. It is the
                same badge a person's pause produces, so without this the two
                are indistinguishable — and they want very different things from
                whoever is reading the page. Nothing was lost, and it says so. */}
            {pausedItself(batch) && (
              <div className="rounded-xl border border-warn/30 bg-warn/[0.05] px-4 py-3 text-[13px] leading-5 text-warn-ink">
                <strong className="font-semibold">Drafting paused itself.</strong>{" "}
                {batch.failure_reason} Nothing was lost — every row is where it was, and
                Resume drafting carries on from there.
              </div>
            )}

            <SendsSection
              batch={batch}
              sends={sends}
              page={sendPage}
              hasMore={moreSends}
              blocked={sendBlocked}
              onPage={setSendPage}
              onChanged={() => {
                loadBatch();
                loadRows();
              }}
              onSend={() => setSending({ kind: "batch" })}
            />

            {/* The counts are the filter: each figure shows its rows below. */}
            <FigureTabs
              value={tab}
              onChange={setTab}
              options={TABS.map((t) => ({
                ...t,
                count: t.value === "all" ? batch.counts.total : batch.counts[t.value],
                detail: tabDetail(batch, t.value),
              }))}
            >
              <BatchProgress batch={batch} />
            </FigureTabs>

            <section className="-mt-3 grid min-w-0 gap-3">
              {/* Two rows, not one. Heading and the things you DO on the first;
                  the filter you are looking through on the second. Seven
                  controls on one line made every one of them look optional.

                  Sticky under the page header, because this is the toolbar you
                  reach for with fifty rows on screen — and because the table's
                  own heading cannot be sticky without either hiding a row or
                  nesting a scrollbar (see ReviewTable). */}
              <div className="sticky top-[52px] z-20 -mx-6 flex flex-wrap items-center gap-2 border-b border-line bg-surface/95 px-6 py-2.5 backdrop-blur-md">
                <h2 className="font-display text-[15px] font-semibold leading-5 tracking-tight text-ink">
                  {TABS.find((t) => t.value === tab)?.label}
                </h2>
                <span className="text-[12.5px] leading-5 text-muted tabular-nums">
                  {rowTotal.toLocaleString()} {rowTotal === 1 ? "row" : "rows"}
                </span>
                {tab !== "all" && (
                  <button
                    type="button"
                    onClick={() => setTab("all")}
                    className="text-[12.5px] font-medium text-ink underline decoration-line-strong underline-offset-2 hover:decoration-ink"
                  >
                    Show everyone
                  </button>
                )}
                <div className="ml-auto flex flex-wrap items-center gap-1.5">
                  <Input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search this page"
                    aria-label="Search the rows on this page"
                    className="w-[190px]"
                  />
                  {/* The one bulk action worth a button of its own: it is safe,
                      it is contextual, and it disappears when there is nothing
                      to fix. Redrafting EVERYTHING sits in the menu below,
                      deliberately far from it — the two are one word apart in
                      the API and five thousand paid task runs apart in effect. */}
                  {canRedraft && batch.counts.draft_failed > 0 && (
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      onClick={() => setRedrafting("failed")}
                    >
                      Redraft {batch.counts.draft_failed.toLocaleString()} failed
                    </Button>
                  )}
                  <Menu
                    label="Bulk actions on these rows"
                    items={[
                      // The API's "every row skip can act on": drafts, queued
                      // drafts a send has not taken yet, and failed drafts.
                      ...(unsent
                        ? [
                            {
                              label: `Skip all ${unsent.toLocaleString()} not yet sent`,
                              onSelect: () =>
                                act("skip", () =>
                                  api.skipEmailBatchRecipients(batch.id, {
                                    selection: "all_eligible",
                                  }),
                                ),
                            },
                          ]
                        : []),
                      ...(batch.skips.operator
                        ? [
                            {
                              label: `Restore ${batch.skips.operator.toLocaleString()} you skipped`,
                              onSelect: () =>
                                act("restore", () =>
                                  api.restoreEmailBatchRecipients(batch.id, {
                                    selection: "all_eligible",
                                  }),
                                ),
                            },
                          ]
                        : []),
                      ...(canRedraft
                        ? [
                            {
                              label: "Redraft everything…",
                              onSelect: () => setRedrafting("all"),
                              danger: true,
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              </div>

              <NotSendable batch={batch} onShow={setTab} />

              {canRedraft && staleRows > 0 && batch.published_task_version != null && (
                <div className="flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-warn-ink">
                  <span>
                    <StaleSentence batch={batch} />
                  </span>
                  <Button
                    variant="secondary"
                    size="sm"
                    className="ml-auto"
                    onClick={() => setRedrafting("stale_version")}
                  >
                    Redraft those {staleRows.toLocaleString()}
                  </Button>
                </div>
              )}

              {err && (
                <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[13px] leading-5 text-danger">
                  {err}
                </p>
              )}

              {loadingRows && rows.length === 0 ? (
                <ListSkeleton rows={6} />
              ) : visible.length === 0 ? (
                <p className="rounded-xl border border-line-2 bg-white px-4 py-8 text-center text-[13px] text-muted">
                  {needle ? "Nothing on this page matches that." : "No rows here right now."}
                </p>
              ) : (
                <ReviewTable
                  batch={batch}
                  rows={visible}
                  selected={selected}
                  busy={busy}
                  onToggle={(id) =>
                    setSelected((current) => {
                      const next = new Set(current);
                      if (next.has(id)) next.delete(id);
                      else next.add(id);
                      return next;
                    })
                  }
                  onToggleAll={() =>
                    setSelected((current) =>
                      sendableOnPage.every((r) => current.has(r.id))
                        ? new Set()
                        : new Set(sendableOnPage.map((r) => r.id)),
                    )
                  }
                  onEdit={editCell}
                  onTrace={setTracing}
                  onPreview={(row) => setPreviewing(row.id)}
                />
              )}

              {needle && (
                <p className="text-[13px] text-muted">
                  {visible.length.toLocaleString()} of {rows.length.toLocaleString()} on this page match
                </p>
              )}
              <Pager page={page} pageSize={PAGE_SIZE} total={rowTotal} onPage={setPage} />
            </section>
          </div>
        </Container>

        {/* The send bar only appears once something is ticked. A permanent bar
            saying "0 selected" on a page whose job is reading is noise. */}
        {selected.size > 0 && (
          <div className="sticky bottom-0 z-20 border-t border-line bg-surface/95 px-6 py-3 backdrop-blur-md">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <p className="min-w-0 flex-1 text-[13px] leading-5">
                <span className="font-semibold tabular-nums text-ink">{selected.size}</span>{" "}
                {selected.size === 1 ? "email" : "emails"} from{" "}
                <span className="font-mono text-[12.5px] text-ink">{batch.from_email}</span>.
              </p>
              <Button variant="secondary" size="sm" onClick={() => setSelected(new Set())}>
                Clear
              </Button>
              <Button
                onClick={() => setSending({ kind: "ids", ids: [...selected] })}
                disabled={busy}
              >
                Send {selected.size.toLocaleString()} selected
              </Button>
            </div>
          </div>
        )}
      </div>

      {tracing?.task_run_id && batch.task_id && (
        <TraceModal
          taskId={batch.task_id}
          runId={tracing.task_run_id}
          rowNumber={tracing.row_number}
          onClose={() => setTracing(null)}
        />
      )}

      {previewIndex >= 0 && (
        <RowPreview
          batch={batch}
          row={visible[previewIndex]}
          index={previewIndex}
          total={visible.length}
          onMove={(delta) => {
            const next = visible[previewIndex + delta];
            if (next) setPreviewing(next.id);
          }}
          onSave={async (row, overrides) => {
            await saveCells(row, overrides);
            // An edit here can move the row between `draft` and `draft_failed`,
            // which is the number in the toolbar and on the send button.
            loadBatch();
          }}
          onSent={(address) => {
            toast({ msg: `Queued to ${address}.`, kind: "ok" });
            loadBatch();
            loadRows();
          }}
          onClose={() => setPreviewing(null)}
        />
      )}

      {sending && (
        <SendDialog
          batch={batch}
          selection={sending}
          prior={prior}
          othersLive={liveSends.length > 0}
          onClose={() => setSending(null)}
          onSent={(send, rejected) => {
            setSending(null);
            setSelected(new Set());
            const queued = send.counts.total;
            toast({
              msg:
                queued === null
                  ? "Created. Every row goes out as it finishes drafting."
                  : rejected
                    ? `${queued.toLocaleString()} queued, ${rejected.toLocaleString()} refused — see the rows.`
                    : `${queued.toLocaleString()} queued.`,
              kind: rejected ? "err" : "ok",
            });
            loadBatch();
            loadRows();
          }}
        />
      )}

      {redrafting && (
        <RedraftDialog
          batch={batch}
          kind={redrafting}
          staleRows={staleRows}
          onClose={() => setRedrafting(null)}
          onDone={(affected) => {
            setRedrafting(null);
            toast({
              // A paused batch drafts nothing until it is resumed, and the
              // breaker now pauses batches by itself, so this is no longer an
              // edge case worth glossing over: the rows genuinely sit there.
              msg:
                batch.status === "paused"
                  ? `${affected.toLocaleString()} ${affected === 1 ? "row" : "rows"} reset — they draft when you resume drafting.`
                  : `${affected.toLocaleString()} ${affected === 1 ? "row" : "rows"} queued to draft again.`,
              kind: "ok",
            });
            loadBatch();
            loadRows();
          }}
        />
      )}

      {editing && (
        <EditPolicy
          batch={batch}
          tasks={tasks}
          accounts={accounts}
          identityEditable={identityEditable}
          onSaved={(next) => {
            setBatch(next);
            setEditing(false);
            toast({ msg: "Saved. The next pass uses it.", kind: "ok" });
          }}
          onClose={() => setEditing(false)}
        />
      )}

      {adding && (
        <AddRows
          batch={batch}
          onAdded={() => {
            loadBatch();
            loadRows();
          }}
          onClose={() => setAdding(false)}
        />
      )}

      {confirmDelete && (
        <DeleteBatchModal
          run={() => api.deleteEmailBatch(batch.id)}
          onExport={() => exportCsv(batch)}
          onDeleted={() => router.push("/email/outbound")}
          onClose={() => setConfirmDelete(false)}
        >
          <p>
            The batch, its {batch.counts.total.toLocaleString()}{" "}
            {batch.counts.total === 1 ? "row" : "rows"}, their drafts and its sends are deleted for
            good. That includes the record of what was sent to whom.
          </p>
        </DeleteBatchModal>
      )}

      {confirmCancel && (
        <Modal
          title="Stop drafting this batch?"
          onClose={() => !busy && setConfirmCancel(false)}
          width="max-w-[520px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmCancel(false)} disabled={busy}>
                Keep drafting
              </Button>
              <Button
                variant="danger"
                disabled={busy}
                onClick={() => steer("cancel", () => api.cancelEmailBatch(batch.id))}
              >
                Stop drafting
              </Button>
            </>
          }
        >
          <div className="grid gap-2.5 text-[14px] leading-6 text-ink-soft">
            <p>
              The {batch.counts.pending.toLocaleString()} rows not yet drafted never will be.
            </p>
            <p>
              The {(batch.counts.draft + batch.counts.queued).toLocaleString()} drafts already
              written stay here and can still be sent, and any send already scheduled or going out
              is untouched. This stops the drafting and nothing else.
            </p>
          </div>
        </Modal>
      )}
    </AppShell>
  );
}

/** What this batch is, as labelled facts rather than a sentence.
 *
 *  This was one wrapping line of eight dot-separated fragments — every fact the
 *  same size, the same grey, and unlabelled, so "Asia/Kolkata" and
 *  "Resend (smoke test)" read as the same kind of thing. Six labelled cells in a
 *  grid is the same information and none of the work. */
/** Append rows to the batch, saying first whether a send will mail them unreviewed. */
function AddRows({
  batch,
  onAdded,
  onClose,
}: {
  batch: EmailBatchResponse;
  onAdded: () => void;
  onClose: () => void;
}) {
  const [list, setList] = useState<ParsedList>(EMPTY_LIST);
  /* Whether a send of every row will take the new drafts: one that is live, or
     the batch's most recent send if it finished on its own — the API reopens
     that one. Read here rather than from the sends section, which may be showing
     a later page. The response's `standing_send_id` is what actually happened. */
  const [standing, setStanding] = useState<boolean | null>(null);
  useEffect(() => {
    api
      .listEmailSends(batch.id, { limit: 1 })
      .then((page) => {
        const latest = page.items[0];
        setStanding(
          !!latest && latest.scope === "all" && (coversRows(latest.status) || latest.status === "sent"),
        );
      })
      .catch(() => setStanding(false));
  }, [batch.id]);

  const sendLine = "Then sent by your send of every row — nobody reviews them first.";
  const text =
    batch.status === "paused"
      ? `Drafted once you resume drafting.${standing ? ` ${sendLine}` : ""}`
      : standing
        ? "Drafted, then sent by your send of every row — nobody reviews them first."
        : "Drafted for review. Nothing is sent until you send them.";

  return (
    <AddRecipientsModal
      batchName={batch.name}
      count={list.rows.length}
      blocked={list.badColumns.length > 0 || standing === null}
      note={{ text, tone: standing ? "warn" : undefined }}
      fixHint="They're in the batch as skipped. Fill in the cell in the table and they're drafted."
      submit={() =>
        api.addEmailBatchRecipients(batch.id, {
          recipients: list.rows.map((input) => ({ input })),
        })
      }
      onAdded={onAdded}
      onClose={onClose}
    >
      <RecipientEditor
        list={list}
        onChange={setList}
        copy={{
          ...LIST_COPY,
          starterColumns: batch.input_columns,
          sample: { csv: `${batch.input_columns.join(",")}\n`, fileName: `${batch.name}-rows.csv` },
        }}
      />
    </AddRecipientsModal>
  );
}

function Overview({
  batch,
  zone,
  live,
}: {
  batch: EmailBatchResponse;
  zone: string;
  /** The newest send still covering rows, if any. */
  live: EmailSendResponse | undefined;
}) {
  const days = batch.window ? weekdayRange(batch.window.days ?? []) : null;
  return (
    <Panel className="grid gap-3">
      {/* One line of facts, each as wide as what it says: equal columns cut
          the task's name short to leave room beside a two-word value. */}
      <dl className="flex flex-wrap gap-x-10 gap-y-3.5">
        <DescriptionItem term="Writes each email">
          {batch.task_name ?? "Task deleted"}
          {batch.published_task_version != null && (
            <span className="ml-1.5 text-muted">v{batch.published_task_version}</span>
          )}
        </DescriptionItem>
        <DescriptionItem term="Sends from" mono copyValue={batch.from_email}>
          {batch.from_email}
        </DescriptionItem>
        <DescriptionItem term="Through">
          {batch.integration_name ?? "Account disconnected"}
        </DescriptionItem>
        <DescriptionItem term="Sending hours">
          {batch.window
            ? `${days} ${hhmm(batch.window.start)}–${hhmm(batch.window.end)} ${zone}`
            : `Any hour · ${batch.timezone.replace(/_/g, " ")}`}
        </DescriptionItem>
        {/* The batch's pace is the DEFAULT for the next send. A live send
            carries its own, shown on its row below, and the two disagreeing
            used to read as one fact. */}
        <DescriptionItem term={live && live.send_gap_seconds !== batch.send_gap_seconds ? "Pace for the next send" : "Pace"}>
          {paceSentence(batch.send_gap_seconds, 1).replace(/\.$/, "")}
        </DescriptionItem>
        <DescriptionItem term="Drafting">
          {batch.provider_cost != null && (
            <span className="font-mono text-[12.5px] tabular-nums">${batch.provider_cost.toFixed(4)}</span>
          )}
          {batch.drafted_at && (
            <span className="text-muted">
              {batch.provider_cost != null && " · "}done {timeAgo(batch.drafted_at)}
            </span>
          )}
          {batch.provider_cost == null && !batch.drafted_at && <span className="text-muted">Not finished</span>}
        </DescriptionItem>
      </dl>
      {batch.next_draft_at && (
        <p className="border-t border-line pt-3 text-[12.5px] leading-4 text-ink-soft">
          Drafting waits until {inZone(batch.next_draft_at, batch.timezone)} {zone}
          {batch.next_draft_reason === "retry" && ` — ${DRAFT_WAIT_REASON.retry}`}
        </p>
      )}
    </Panel>
  );
}

/** The line under a figure, where there is something the count alone leaves out. */
function tabDetail(batch: EmailBatchResponse, tab: EmailRecipientStatus | "all"): string | null {
  const { counts, skips } = batch;
  if (tab === "draft" && counts.draft) return "ready to send";
  if (tab === "pending" && counts.drafting) return `${counts.drafting.toLocaleString()} drafting now`;
  if (tab === "skipped" && skips.unfillable) return `${skips.unfillable.toLocaleString()} can never be sent`;
  if (tab === "queued" && counts.queued) return "in a send";
  if (tab === "sent")
    return batch.send_daily_cap_today === null
      ? `${batch.sent_today.toLocaleString()} today, no daily cap`
      : usedToday(batch.sent_today, batch.send_daily_cap_today, batch.send_daily_cap);
  return null;
}

/** Every bucket of the rows an older version wrote, named.
 *
 *  "1,499 rows were written by v5" beside a button offering to redraft 705 is
 *  two numbers that do not add up; this says where the other 794 are. */
function StaleSentence({ batch }: { batch: EmailBatchResponse }) {
  const published = batch.published_task_version ?? 0;
  const older = batch.drafted_versions.filter((v) => v.version < published);
  const total = older.reduce((n, v) => n + v.count, 0);
  const rest = total - batch.stale_redraftable - batch.stale_sent;
  const parts = [
    `${batch.stale_redraftable.toLocaleString()} can be redrafted`,
    ...(batch.stale_sent ? [`${batch.stale_sent.toLocaleString()} have already been sent`] : []),
    ...(rest > 0 ? [`${rest.toLocaleString()} cannot be`] : []),
  ];
  return (
    <>
      <strong className="font-semibold">
        {total.toLocaleString()} {total === 1 ? "row was" : "rows were"} written by{" "}
        {older.length === 1 ? `v${older[0].version}` : "older versions"}
      </strong>
      {older.length > 1 &&
        ` (${older.map((v) => `${v.count.toLocaleString()} on v${v.version}`).join(", ")})`}{" "}
      of {batch.task_name ?? "this task"}. v{published} is published now.{" "}
      {parts.length > 1
        ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}.`
        : `${parts[0]}.`}
    </>
  );
}

/** Why rows in this batch will not be sent, by reason — before anyone presses a
 *  button that costs money.
 *
 *  Two different remedies hide behind "not sendable". A row whose address column
 *  the task never writes is fixed by typing a cell in, and a redraft would buy the
 *  same empty result; a draft that came back without a subject might well come
 *  back with one. Named here, the operator can tell them apart. */
function NotSendable({
  batch,
  onShow,
}: {
  batch: EmailBatchResponse;
  onShow: (tab: EmailRecipientStatus) => void;
}) {
  const unfillable = batch.skips.unfillable;
  const failed = batch.counts.draft_failed;
  if (!unfillable && !failed) return null;
  const columns = unwrittenColumns(batch);
  const shownFailures = batch.draft_failures.reduce((n, f) => n + f.count, 0);
  return (
    <div className="grid gap-2 rounded-xl border border-line-2 bg-canvas px-3.5 py-2.5 text-[13px] leading-5 text-ink-soft">
      {unfillable > 0 && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="min-w-0 flex-1">
            <strong className="font-semibold text-ink">
              {unfillable.toLocaleString()} {unfillable === 1 ? "row" : "rows"} can never be sent.
            </strong>{" "}
            {columns.length === 1 ? (
              <>
                <code className="font-mono text-[12.5px] text-ink">{columns[0]}</code> is empty on{" "}
                {unfillable === 1 ? "it" : "them"}, and {batch.task_name ?? "the task"} does not
                write it
              </>
            ) : (
              <>A column {batch.task_name ?? "the task"} does not write is empty on {unfillable === 1 ? "it" : "them"}</>
            )}{" "}
            — so {unfillable === 1 ? "it is" : "they are"} skipped, not drafted. Fill the cell in to
            bring a row back.
          </span>
          <button
            type="button"
            onClick={() => onShow("skipped")}
            className="text-[12.5px] font-medium text-ink underline decoration-line-strong underline-offset-2 hover:decoration-ink"
          >
            Show them
          </button>
        </div>
      )}
      {failed > 0 && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="min-w-0 flex-1">
            <strong className="font-semibold text-ink">
              {failed.toLocaleString()} {failed === 1 ? "draft" : "drafts"} failed:
            </strong>{" "}
            {batch.draft_failures.length === 1 && batch.draft_failures[0].count === failed
              ? batch.draft_failures[0].reason
              : batch.draft_failures
                  .map((f) => `${f.reason} — ${f.count.toLocaleString()}`)
                  .join(" · ")}
            {failed > shownFailures &&
              ` · ${(failed - shownFailures).toLocaleString()} for other reasons`}
          </span>
          <button
            type="button"
            onClick={() => onShow("draft_failed")}
            className="text-[12.5px] font-medium text-ink underline decoration-line-strong underline-offset-2 hover:decoration-ink"
          >
            Show them
          </button>
        </div>
      )}
    </div>
  );
}

/** "Mon–Fri", "Mon, Wed, Thu", "Every day" — the compact form of a day set. */
function weekdayRange(days: number[]): string {
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const sorted = [...days].sort((a, b) => a - b);
  if (sorted.length === 7 || sorted.length === 0) return "Every day";
  const contiguous = sorted.every((d, i) => i === 0 || d === sorted[i - 1] + 1);
  if (contiguous && sorted.length > 2) {
    return `${names[sorted[0] - 1]}–${names[sorted[sorted.length - 1] - 1]}`;
  }
  return sorted.map((d) => names[d - 1]).join(", ");
}

export default function EmailBatchDetailPage() {
  return (
    <Suspense fallback={null}>
      <BatchDetailInner />
    </Suspense>
  );
}
