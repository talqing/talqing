"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import type {
  AgentResponse,
  IntegrationResponse,
  WhatsAppBatchResponse,
  WhatsAppRecipientResponse,
} from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { AddRecipientsModal } from "@/app/components/AddRecipientsModal";
import { DeleteBatchModal } from "@/app/components/DeleteBatchModal";
import {
  Badge,
  Button,
  btn,
  Container,
  Field,
  FigureTabs,
  Input,
  Menu,
  Modal,
  Pager,
  SHEET,
  Skeleton,
  Tooltip,
  useToast,
} from "@/app/components/ui";
import { api, talqing } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { inZoneShort, timeAgo } from "@/app/telephony/outbound-calling/shared";
import { EMPTY_LIST, type ParsedList, toCsv } from "@/app/email/outbound/csv";
import { RecipientEditor } from "@/app/email/outbound/RecipientEditor";
import { limitToday, rampNeverRises, usedToday } from "@/app/components/Pacing";
import { sendSpan } from "@/app/email/outbound/shared";
import {
  SendTermsFields,
  type SendTermsDraft,
  startAtOf,
  termsBody,
  termsFrom,
  windowNeverOpens,
} from "@/app/email/outbound/SendTerms";
import { CreateBatch, type InitialBatch } from "../CreateBatch";
import { listCopy } from "../listCopy";
import { Progress } from "../Progress";
import { ResendRow, type ResendDraft, resendBody, resendFrom } from "../ResendRow";
import { TemplatePreview } from "../TemplatePreview";
import {
  BATCH_STATUS_LABEL,
  BATCH_STATUS_VARIANT,
  ROW_STATUS_LABEL,
  ROW_STATUS_VARIANT,
  TAB_FILTER,
  type Tab,
  WAIT_REASON,
  formatPhone,
  pausedItself,
  plainError,
  resendsAt,
  rowCells,
  sentCount,
  tabCount,
  whatsAppSenders,
} from "../shared";

const PAGE_SIZE = 50;
const TABS: { value: Tab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "ready", label: "Ready" },
  { value: "skipped", label: "Skipped" },
  { value: "sent", label: "Sent" },
  { value: "delivered", label: "Delivered" },
  { value: "read", label: "Read" },
  { value: "replied", label: "Replied" },
  { value: "failed", label: "Failed" },
];

export default function WhatsAppBatchPage() {
  return (
    <Suspense>
      <BatchDetail />
    </Suspense>
  );
}

function BatchDetail() {
  const id = useSearchParams().get("id") ?? "";
  const router = useRouter();
  const toast = useToast();
  const [batch, setBatch] = useState<WhatsAppBatchResponse | null>(null);
  const [rows, setRows] = useState<WhatsAppRecipientResponse[] | null>(null);
  const [tab, setTab] = useState<Tab>("all");
  const [page, setPage] = useState(0);
  const [loadingRows, setLoadingRows] = useState(false);
  // The newest request for rows; an older one that answers late is dropped.
  const rowsRequest = useRef(0);
  const table = useRef<HTMLElement>(null);
  const [err, setErr] = useState("");
  const [sending, setSending] = useState(false);
  const [editingPace, setEditingPace] = useState(false);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<WhatsAppRecipientResponse | null>(null);
  const [previewing, setPreviewing] = useState<WhatsAppRecipientResponse | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [resending, setResending] = useState<{
    senders: IntegrationResponse[];
    agents: AgentResponse[];
    initial: InitialBatch;
  } | null>(null);

  const loadBatch = useCallback(() => {
    talqing.whatsapp.batches
      .get({ batch_id: id })
      .then(setBatch)
      .catch((e) => setErr(apiErrorMessage(e, "Could not load this batch.")));
  }, [id]);

  const loadRows = useCallback(() => {
    const request = ++rowsRequest.current;
    setLoadingRows(true);
    talqing.whatsapp.batches.recipients
      .list({ batch_id: id, status: TAB_FILTER[tab], limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      .then((res) => {
        if (request === rowsRequest.current) setRows(res.items);
      })
      .catch((e) => setErr(apiErrorMessage(e, "Could not load the rows.")))
      .finally(() => {
        if (request === rowsRequest.current) setLoadingRows(false);
      });
  }, [id, tab, page]);

  useEffect(() => {
    if (id) loadBatch();
  }, [id, loadBatch]);
  useEffect(() => {
    if (id) loadRows();
  }, [id, loadRows]);

  // A row skipped or restored can empty the last page of the tab it left.
  const rowTotal = batch ? tabCount(batch, tab) : 0;
  const lastPage = Math.max(Math.ceil(rowTotal / PAGE_SIZE) - 1, 0);
  useEffect(() => {
    if (page > lastPage) setPage(lastPage);
  }, [page, lastPage]);

  function refresh() {
    loadBatch();
    loadRows();
  }

  function showTab(next: Tab) {
    setTab(next);
    setPage(0);
    setRows(null);
  }

  async function steer(run: () => Promise<WhatsAppBatchResponse>, said: string) {
    try {
      setBatch(await run());
      toast({ msg: said, kind: "ok" });
    } catch (e) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    }
  }

  async function act(verb: "skip" | "restore", row: WhatsAppRecipientResponse) {
    try {
      const body = { batch_id: id, selectRecipientsRequest: { recipient_ids: [row.id] } };
      const result =
        verb === "skip"
          ? await talqing.whatsapp.batches.recipients.skip(body)
          : await talqing.whatsapp.batches.recipients.restore(body);
      if (result.rejected.length) toast({ msg: result.rejected[0].reason, kind: "err" });
      refresh();
    } catch (e) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    }
  }

  async function exportCsv() {
    if (!batch) return;
    try {
      const all: WhatsAppRecipientResponse[] = [];
      for (let offset = 0; ; offset += 200) {
        const page = await talqing.whatsapp.batches.recipients.list({ batch_id: id, limit: 200, offset });
        all.push(...page.items);
        if (!page.has_more) break;
      }
      const csv = toCsv([
        [...batch.input_columns, "status", "error", "sent_at", "delivered_at", "read_at", "replied_at"],
        ...all.map((r) => [
          ...batch.input_columns.map((c) => r.overrides[c] ?? r.input[c] ?? ""),
          ROW_STATUS_LABEL[r.status],
          plainError(r) ?? "",
          r.sent_at ?? "",
          r.delivered_at ?? "",
          r.read_at ?? "",
          r.replied_at ?? "",
        ]),
      ]);
      // The BOM is what makes Excel read non-Latin names as UTF-8.
      const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `${batch.name}.csv`;
      link.click();
      // Not in the same tick: that cancels the download in some browsers.
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      toast({ msg: apiErrorMessage(e, "Could not export the rows."), kind: "err" });
    }
  }

  /* A batch sends each row once. The rows WhatsApp refused go again as a new
     batch: same sender, template, mapping and pace, with those rows as its list. */
  async function newBatchFromFailed() {
    if (!batch) return;
    try {
      const failed: WhatsAppRecipientResponse[] = [];
      for (let offset = 0; ; offset += 200) {
        const page = await talqing.whatsapp.batches.recipients.list({
          batch_id: id,
          status: TAB_FILTER.failed,
          limit: 200,
          offset,
        });
        failed.push(...page.items);
        if (!page.has_more) break;
      }
      const [integrations, agents] = await Promise.all([api.listIntegrations(), api.listAllAgents()]);
      setResending({
        senders: whatsAppSenders(integrations.items),
        agents,
        initial: {
          senderId: batch.integration_id ?? "",
          templateId: batch.template.id,
          list: {
            columns: batch.input_columns,
            rows: failed.map((row) => {
              const cells = rowCells(row);
              return Object.fromEntries(batch.input_columns.map((c) => [c, cells[c] ?? ""]));
            }),
            badColumns: [],
            error: "",
          },
          toColumn: batch.to_column,
          variableMap: batch.variable_map,
          terms: termsFrom(batch, null),
          resend: resendFrom(batch),
        },
      });
    } catch (e) {
      toast({ msg: apiErrorMessage(e, "Could not load the failed rows."), kind: "err" });
    }
  }

  if (!id) return null;
  const sent = batch ? sentCount(batch) : 0;
  const left =
    batch &&
    sendSpan(
      batch.counts.ready,
      batch.send_gap_seconds,
      batch.send_daily_cap,
      batch.send_daily_cap_today,
      batch.window,
    );
  // The columns the message uses come first, then the rest of the list.
  const mapped = batch ? [...new Set(Object.values(batch.variable_map))].filter((c) => c !== batch.to_column) : [];
  const rest = batch ? batch.input_columns.filter((c) => c !== batch.to_column && !mapped.includes(c)) : [];
  const open = batch && ["draft", "scheduled", "sending", "paused"].includes(batch.status);
  // A completed batch is idle, not over: its pace, its rows and new rows are all still in play.
  const steerable = open || batch?.status === "completed";

  return (
    <AppShell>
      <Container>
        <div className="pt-6">
          <Link href="/whatsapp/outbound" className="text-[13px] font-medium text-muted hover:text-ink">
            ← WhatsApp
          </Link>
        </div>
        {err && !batch ? (
          <div className="mt-6 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        ) : !batch ? (
          <div className="mt-4">
            <Skeleton className="h-8 w-64" />
            <Skeleton className="mt-3 h-4 w-96" />
            <Skeleton className="mt-8 h-24 w-full rounded-xl" />
          </div>
        ) : (
          <>
            <header className="mb-5 mt-2 flex flex-col gap-4 border-b border-line pb-5 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2.5">
                  <BatchName batch={batch} onChange={setBatch} />
                  <Badge variant={BATCH_STATUS_VARIANT[batch.status]} dot={batch.status === "sending"}>
                    {BATCH_STATUS_LABEL[batch.status]}
                  </Badge>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-5 text-muted">
                  <span className="font-mono text-[12.5px] text-ink-soft">{batch.template.name}</span>
                  <span aria-hidden className="text-line-strong">·</span>
                  <span>
                    from {batch.sender_e164 ? formatPhone(batch.sender_e164) : "a disconnected sender"}
                  </span>
                  <span aria-hidden className="text-line-strong">·</span>
                  {batch.agent_name ? (
                    <span>
                      replies go to <span className="text-ink-soft">{batch.agent_name}</span>
                    </span>
                  ) : (
                    <span className="text-warn">no agent answers replies</span>
                  )}
                </div>
              </div>
              <div className="flex flex-none items-center gap-2">
                {steerable && (
                  <Button variant="secondary" onClick={() => setAdding(true)}>
                    Add recipients
                  </Button>
                )}
                {batch.status === "draft" && (
                  <Button onClick={() => setSending(true)} disabled={!batch.counts.ready}>
                    Start sending
                  </Button>
                )}
                {(batch.status === "scheduled" || batch.status === "sending" || batch.status === "completed") && (
                  <Button
                    variant="secondary"
                    onClick={() =>
                      steer(
                        () => talqing.whatsapp.batches.pause({ batch_id: id }),
                        batch.status === "completed"
                          ? "Paused. Rows you add wait until you resume."
                          : "Paused before the next message.",
                      )
                    }
                  >
                    Pause
                  </Button>
                )}
                {batch.status === "paused" && (
                  <Button
                    onClick={() => steer(() => talqing.whatsapp.batches.resume({ batch_id: id }), "Sending again.")}
                  >
                    Resume
                  </Button>
                )}
                <Button variant="ghost" onClick={refresh}>
                  Refresh
                </Button>
                <Menu
                  label="More actions"
                  items={[
                    { label: "Export CSV", onSelect: exportCsv },
                    ...(batch.counts.failed + batch.counts.undelivered > 0
                      ? [{ label: "New batch from failed rows", onSelect: newBatchFromFailed }]
                      : []),
                    // A draft has nothing to stop, so it is deleted rather than canceled.
                    ...(open && batch.status !== "draft"
                      ? [{ label: "Cancel batch", onSelect: () => setConfirmCancel(true), danger: true }]
                      : [{ label: "Delete batch", onSelect: () => setConfirmDelete(true), danger: true }]),
                  ]}
                />
              </div>
            </header>

            {(pausedItself(batch) || batch.status === "failed") && batch.failure_reason && (
              <div
                className={cn(
                  "mb-5 rounded-lg border px-3.5 py-2.5 text-[13.5px]",
                  batch.status === "failed"
                    ? "border-danger/25 bg-danger/[0.05] text-danger"
                    : "border-warn/25 bg-warn/[0.06] text-warn",
                )}
              >
                {batch.status === "failed" ? "Stopped: " : "Paused itself: "}
                {batch.failure_reason}
              </div>
            )}

            <FigureTabs
              className="mb-5"
              value={tab}
              onChange={showTab}
              options={TABS.map((t) => ({
                ...t,
                count: tabCount(batch, t.value),
                detail: tabDetail(batch, t.value),
              }))}
            >
              <div className="flex items-center gap-4">
                <Progress batch={batch} bare className="flex-1" />
                <span className="flex-none text-[12px] leading-4 text-muted tabular-nums">
                  {sent.toLocaleString()} of {(batch.counts.total - batch.counts.skipped).toLocaleString()} sent
                </span>
              </div>
            </FigureTabs>

            <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_392px]">
              <aside className="grid min-w-0 gap-5 scroll-thin lg:sticky lg:top-4 lg:order-2 lg:max-h-[calc(100vh-2rem)] lg:overflow-y-auto">
                <div className="rounded-xl border border-line-2 bg-white p-4">
                  <div className="mb-3 flex min-h-8 items-center justify-between gap-2">
                    <span className="text-[13px] font-semibold text-ink">Sending</span>
                    {steerable && (
                      <Button variant="secondary" size="sm" onClick={() => setEditingPace(true)}>
                        Edit
                      </Button>
                    )}
                  </div>
                  <dl className="grid grid-cols-[76px_minmax(0,1fr)] gap-y-2 text-[13px] leading-5">
                    <dt className="text-muted">Pace</dt>
                    <dd className="text-ink-soft">{paceLabel(batch)}</dd>
                    <dt className="text-muted">Hours</dt>
                    <dd className="text-ink-soft">
                      {batch.window
                        ? `${batch.window.start.slice(0, 5)}–${batch.window.end.slice(0, 5)}${windowDaysLabel(batch.window.days)}`
                        : "Any time"}
                      <span className="block text-[12px] text-faint">{batch.timezone.replace(/_/g, " ")}</span>
                    </dd>
                    <dt className="text-muted">Held back</dt>
                    <dd className="text-ink-soft">
                      {batch.delivery_attempts > 1
                        ? `Sent again up to ${batch.delivery_attempts - 1}×, ${batch.delivery_retry_after_hours} h apart`
                        : "Not sent again"}
                    </dd>
                    <dt className="text-muted">Next</dt>
                    <dd className="text-ink-soft">
                      {batch.next_send_at ? (
                        <>
                          {inZoneShort(batch.next_send_at, batch.timezone)}
                          {batch.next_send_reason && (
                            <span className="block text-[12px] text-faint">{WAIT_REASON[batch.next_send_reason]}</span>
                          )}
                        </>
                      ) : batch.status === "sending" ? (
                        "Sending now"
                      ) : batch.finished_at ? (
                        `Finished ${timeAgo(batch.finished_at)}`
                      ) : batch.status === "draft" ? (
                        "When you start sending"
                      ) : (
                        "—"
                      )}
                    </dd>
                    {open && left && batch.counts.ready > 0 && (
                      <>
                        <dt className="text-muted">Takes</dt>
                        <dd className="text-ink-soft">
                          {left.span[0].toUpperCase() + left.span.slice(1)}
                          {left.why && <span className="block text-[12px] text-faint">{left.why}</span>}
                        </dd>
                      </>
                    )}
                  </dl>
                </div>
                <div className="rounded-xl border border-line-2 bg-white p-4">
                  <div className="mb-3 flex items-center justify-between text-[13px]">
                    <span className="font-semibold text-ink">Template</span>
                    <span className="text-faint">
                      {batch.template.category.toLowerCase()} · {batch.template.language}
                    </span>
                  </div>
                  <TemplatePreview template={batch.template} />
                  {batch.template.variables.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-muted">
                      {batch.template.variables.map((v) => (
                        <span key={v}>
                          <span className="font-mono text-ink-soft">{`{{${v}}}`}</span> ← {batch.variable_map[v]}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </aside>

              <div className="min-w-0">

                {/* Sideways scroll, like the email table: the list's own columns
                    come after the ones every batch has. */}
                <section
                  ref={table}
                  className={cn(
                    SHEET.wrap,
                    "scroll-mt-4 transition-opacity",
                    loadingRows && rows && "opacity-60",
                  )}
                >
                  {rows === null ? (
                    [0, 1, 2].map((i) => (
                      <div key={i} className="border-b border-line px-4 py-4 last:border-b-0">
                        <Skeleton className="h-4 w-2/3" />
                      </div>
                    ))
                  ) : rows.length === 0 ? (
                    <div className="px-4 py-10 text-center text-[13px] text-muted">No rows here.</div>
                  ) : (
                    <table className={SHEET.table}>
                      <thead className={SHEET.thead}>
                        <tr className={SHEET.headRow}>
                          <th className={SHEET.thNumber}>#</th>
                          <th className={SHEET.thPinned}>
                            <span className="flex items-center gap-1.5">
                              To <span className={SHEET.thName}>{batch.to_column}</span>
                            </span>
                          </th>
                          {mapped.map((column) => (
                            <th key={column} className={SHEET.thPinned}>
                              <span className="flex items-center gap-1.5">
                                {batch.template.variables
                                  .filter((v) => batch.variable_map[v] === column)
                                  .map((v) => `{{${v}}}`)
                                  .join(" ")}
                                <span className={SHEET.thName}>{column}</span>
                              </span>
                            </th>
                          ))}
                          <th className={cn(SHEET.th, "w-[110px]")}>Status</th>
                          <th className="w-[84px]" />
                          <th className={cn(SHEET.th, "w-[84px]")}>Updated</th>
                          <th className="w-9" />
                          {rest.map((column) => (
                            <th key={column} className={SHEET.thData}>
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row) => (
                          <RecipientRow
                            key={row.id}
                            row={row}
                            batch={batch}
                            phone={row.to_e164 ? formatPhone(row.to_e164) : rowCells(row)[batch.to_column] || "—"}
                            mapped={mapped}
                            rest={rest}
                            editable={Boolean(steerable) && (row.status === "ready" || row.status === "skipped")}
                            onPreview={() => setPreviewing(row)}
                            onEdit={() => setEditing(row)}
                            onSkip={() => act("skip", row)}
                            onRestore={() => act("restore", row)}
                          />
                        ))}
                      </tbody>
                    </table>
                  )}
                </section>
                <Pager
                  className="mt-3"
                  page={page}
                  pageSize={PAGE_SIZE}
                  total={rowTotal}
                  onPage={(next) => {
                    setPage(next);
                    table.current?.scrollIntoView({ block: "start" });
                  }}
                />
              </div>
            </div>
          </>
        )}
      </Container>

      {batch && sending && (
        <SendDialog
          batch={batch}
          onClose={() => setSending(false)}
          onSent={(next) => {
            setSending(false);
            setBatch(next);
            toast({ msg: next.start_at ? "Scheduled." : "Sending.", kind: "ok" });
          }}
        />
      )}
      {batch && adding && (
        <AddRows batch={batch} onAdded={refresh} onClose={() => setAdding(false)} />
      )}
      {batch && editingPace && (
        <EditPacing
          batch={batch}
          onClose={() => setEditingPace(false)}
          onSaved={(next) => {
            setEditingPace(false);
            setBatch(next);
            toast({ msg: "Sending settings saved.", kind: "ok" });
          }}
        />
      )}
      {batch && editing && (
        <EditRow
          batch={batch}
          row={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
      {batch && previewing && (
        <PreviewRow batch={batch} row={previewing} onClose={() => setPreviewing(null)} />
      )}
      {resending && (
        <CreateBatch
          {...resending}
          onCancel={() => setResending(null)}
          onCreated={(created) => {
            setResending(null);
            showTab("all");
            router.push(`/whatsapp/outbound/detail?id=${created.id}`);
          }}
        />
      )}
      {batch && confirmDelete && (
        <DeleteBatchModal
          run={() => talqing.whatsapp.batches.delete({ batch_id: id })}
          onExport={exportCsv}
          onDeleted={() => router.push("/whatsapp/outbound")}
          onClose={() => setConfirmDelete(false)}
        >
          <p>
            The batch and its {batch.counts.total.toLocaleString()}{" "}
            {batch.counts.total === 1 ? "row" : "rows"} are deleted for good, with who was messaged
            and every delivery and reply count.
          </p>
          <p>The conversations it started stay.</p>
        </DeleteBatchModal>
      )}
      {batch && confirmCancel && (
        <Modal
          title="Cancel this batch?"
          sub="Nothing more is sent. Messages already sent stay sent, and replies are still answered."
          onClose={() => setConfirmCancel(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmCancel(false)}>
                Keep it
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirmCancel(false);
                  void steer(() => talqing.whatsapp.batches.cancel({ batch_id: id }), "Canceled.");
                }}
              >
                Cancel batch
              </Button>
            </>
          }
        />
      )}
    </AppShell>
  );
}

/* The batch's name, edited in place: the pencil, or the name itself, swaps the
   heading for an input. Enter or leaving the field saves; Escape does not. */
function BatchName({
  batch,
  onChange,
}: {
  batch: WhatsAppBatchResponse;
  onChange: (batch: WhatsAppBatchResponse) => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<string | null>(null);
  const cancelled = useRef(false);

  async function save() {
    const name = (draft ?? "").trim();
    setDraft(null);
    if (cancelled.current || !name || name === batch.name) return;
    onChange({ ...batch, name });
    try {
      onChange(
        await talqing.whatsapp.batches.update({ batch_id: batch.id, patchWhatsAppBatchRequest: { name } }),
      );
    } catch (e) {
      onChange(batch);
      toast({ msg: apiErrorMessage(e, "Could not rename the batch."), kind: "err" });
    }
  }

  function start() {
    cancelled.current = false;
    setDraft(batch.name);
  }

  if (draft !== null)
    return (
      <input
        autoFocus
        value={draft}
        maxLength={200}
        size={Math.max(draft.length, 12)}
        aria-label="Batch name"
        onChange={(e) => setDraft(e.target.value)}
        onFocus={(e) => e.target.select()}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === "Escape") cancelled.current = true;
          if (e.key === "Enter" || e.key === "Escape") e.currentTarget.blur();
        }}
        className="-mx-2 min-w-0 max-w-full rounded-lg border border-line-2 bg-white px-2 font-display text-[24px] font-semibold leading-8 tracking-tight text-ink outline-none focus:border-ink focus:ring-2 focus:ring-ink/10"
      />
    );
  return (
    <>
      <h1
        onClick={start}
        className="cursor-text truncate font-display text-[24px] font-semibold leading-8 tracking-tight text-ink"
      >
        {batch.name}
      </h1>
      <button
        type="button"
        onClick={start}
        aria-label="Rename batch"
        className="-ml-1 grid h-7 w-7 flex-none place-items-center rounded-lg text-faint transition-colors hover:bg-subtle hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
      >
        <svg className="h-[15px] w-[15px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M4 20h4L19 9l-4-4L4 16Z" />
          <path d="m13.5 6.5 4 4" />
        </svg>
      </button>
    </>
  );
}

/* One line per row. Every row carries the same template, so the message is a
   preview away rather than in every row. */
function RecipientRow({
  row,
  batch,
  phone,
  mapped,
  rest,
  editable,
  onPreview,
  onEdit,
  onSkip,
  onRestore,
}: {
  row: WhatsAppRecipientResponse;
  batch: WhatsAppBatchResponse;
  /** The number it sends to, or the cell as typed while that is not a number. */
  phone: string;
  mapped: string[];
  rest: string[];
  editable: boolean;
  onPreview: () => void;
  onEdit: () => void;
  onSkip: () => void;
  onRestore: () => void;
}) {
  const router = useRouter();
  const cells = rowCells(row);
  const error = plainError(row);
  const nextAttempt = resendsAt(row, batch);
  const conversation = row.conversation_id;
  const items = [
    ...(editable ? [{ label: "Edit cells", onSelect: onEdit }] : []),
    ...(editable && row.status === "ready" ? [{ label: "Skip", onSelect: onSkip }] : []),
    // A row its own cells skipped comes back by fixing the cell, not by restoring it.
    ...(editable && row.status === "skipped" && row.skip_reason === "operator"
      ? [{ label: "Restore", onSelect: onRestore }]
      : []),
    ...(conversation
      ? [{ label: "Open conversation", onSelect: () => router.push(`/conversations?id=${conversation}`) }]
      : []),
  ];
  return (
    <tr className={SHEET.row}>
      <td className={SHEET.tdNumber}>{row.row_number}</td>
      <td className={cn(SHEET.tdPinned, "font-mono text-[12.5px]", !row.to_e164 && "font-normal text-faint")}>
        {phone}
      </td>
      {mapped.map((column) => (
        <td key={column} className={SHEET.tdPinned} title={cells[column]}>
          {cells[column] || <span className="text-placeholder">—</span>}
        </td>
      ))}
      <td className={SHEET.td}>
        <div className="flex items-center gap-2 whitespace-nowrap">
          {error || nextAttempt ? (
            <Tooltip
              label={
                <>
                  {error}
                  {nextAttempt && (
                    <span className="block">
                      Next attempt {inZoneShort(nextAttempt, batch.timezone)} ({row.delivery_attempts + 1} of{" "}
                      {batch.delivery_attempts})
                    </span>
                  )}
                </>
              }
            >
              <Badge variant={ROW_STATUS_VARIANT[row.status]}>{ROW_STATUS_LABEL[row.status]}</Badge>
            </Tooltip>
          ) : (
            <Badge variant={ROW_STATUS_VARIANT[row.status]}>{ROW_STATUS_LABEL[row.status]}</Badge>
          )}
          {row.replied_at && <Badge variant="live">Replied</Badge>}
        </div>
      </td>
      <td className={SHEET.td}>
        <button type="button" onClick={onPreview} className={btn("ghost", "sm")}>
          Preview
        </button>
      </td>
      <td className={cn(SHEET.td, "whitespace-nowrap text-[12px] tabular-nums text-muted")}>
        {timeAgo(row.replied_at ?? row.read_at ?? row.delivered_at ?? row.sent_at)}
      </td>
      <td className="pr-2">
        {items.length > 0 && <Menu label={`Actions for row ${row.row_number}`} items={items} />}
      </td>
      {rest.map((column) => (
        <td key={column} className={SHEET.tdData} title={cells[column]}>
          {cells[column] || <span className="text-placeholder">—</span>}
        </td>
      ))}
    </tr>
  );
}

/* The message as this person gets it: the template with the row's values in
   place, then how far it got and when. */
function PreviewRow({
  batch,
  row,
  onClose,
}: {
  batch: WhatsAppBatchResponse;
  row: WhatsAppRecipientResponse;
  onClose: () => void;
}) {
  const cells = rowCells(row);
  const error = plainError(row);
  const nextAttempt = resendsAt(row, batch);
  const events = [
    { label: "Sent", at: row.sent_at },
    { label: "Delivered", at: row.delivered_at },
    { label: "Read", at: row.read_at },
    { label: "Replied", at: row.replied_at },
  ].filter((e) => e.at);
  return (
    <Modal
      title={`Row ${row.row_number}`}
      sub={row.to_e164 ? `To ${formatPhone(row.to_e164)}` : undefined}
      onClose={onClose}
      width="max-w-[480px]"
      footer={
        <>
          {row.conversation_id && (
            <Link href={`/conversations?id=${row.conversation_id}`} className={btn("secondary")}>
              Open conversation
            </Link>
          )}
          <Button onClick={onClose} data-dialog-autofocus>
            Done
          </Button>
        </>
      }
    >
      <div className="grid gap-4 pb-2">
        <div className="grid gap-1.5 text-[13px] leading-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={ROW_STATUS_VARIANT[row.status]}>{ROW_STATUS_LABEL[row.status]}</Badge>
            {row.replied_at && <Badge variant="live">Replied</Badge>}
            {error && (
              <span className={row.status === "skipped" || nextAttempt ? "text-warn" : "text-danger"}>{error}</span>
            )}
          </div>
          {nextAttempt && (
            <p className="text-[12.5px] text-muted">
              Next attempt <span className="text-ink-soft">{inZoneShort(nextAttempt, batch.timezone)}</span> (
              {row.delivery_attempts + 1} of {batch.delivery_attempts})
            </p>
          )}
          {events.length > 0 && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-muted">
              {events.map((e) => (
                <span key={e.label}>
                  {e.label} <span className="text-ink-soft">{inZoneShort(e.at, batch.timezone)}</span>
                </span>
              ))}
            </div>
          )}
        </div>
        <TemplatePreview
          template={batch.template}
          values={Object.fromEntries(Object.entries(batch.variable_map).map(([v, c]) => [v, cells[c] ?? ""]))}
        />
      </div>
    </Modal>
  );
}

function EditRow({
  batch,
  row,
  onClose,
  onSaved,
}: {
  batch: WhatsAppBatchResponse;
  row: WhatsAppRecipientResponse;
  onClose: () => void;
  onSaved: () => void;
}) {
  const mapped = new Set(Object.values(batch.variable_map));
  // The phone first, then what the message uses, then the agent's extra context.
  const rank = (c: string) => (c === batch.to_column ? 0 : mapped.has(c) ? 1 : 2);
  const columns = [...batch.input_columns].sort((a, b) => rank(a) - rank(b));
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(columns.map((c) => [c, row.overrides[c] ?? row.input[c] ?? ""])),
  );
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string[]>([]);
  const preview = Object.fromEntries(
    Object.entries(batch.variable_map).map(([v, c]) => [v, values[c] ?? ""]),
  );

  async function save() {
    setSaving(true);
    setErr([]);
    try {
      const overrides = Object.fromEntries(
        columns
          .filter((c) => values[c] !== (row.overrides[c] ?? row.input[c] ?? ""))
          .map((c) => [c, values[c] === (row.input[c] ?? "") ? "" : values[c]]),
      );
      await talqing.whatsapp.batches.recipients.update({
        batch_id: batch.id,
        recipient_id: row.id,
        patchWhatsAppRecipientRequest: { overrides },
      });
      onSaved();
    } catch (e) {
      const list = apiErrorList(e);
      setErr(list.length ? list : [apiErrorMessage(e)]);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={`Row ${row.row_number}`}
      onClose={() => !saving && onClose()}
      width="max-w-[640px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving}>
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-4 pb-2">
        {err.length > 0 && (
          <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] text-danger">
            {err.join(" ")}
          </div>
        )}
        <TemplatePreview
          template={batch.template}
          values={preview}
          bodyClassName="max-h-[200px] overflow-y-auto scroll-thin"
        />
        <div className="grid gap-3 sm:grid-cols-2">
          {columns.map((c) => (
            <Field
              key={c}
              label={c === batch.to_column ? `${c} · phone number` : mapped.has(c) ? `${c} · in the message` : c}
            >
              <Input value={values[c] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [c]: e.target.value }))} />
            </Field>
          ))}
        </div>
      </div>
    </Modal>
  );
}

/** Weekdays the window is open on, when it is not every day. */
function windowDaysLabel(days: number[] | undefined): string {
  if (!days || days.length === 7) return "";
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  return days.join() === "1,2,3,4,5" ? ", Mon–Fri" : `, ${days.map((d) => names[d - 1]).join(" ")}`;
}

/** The line under a figure: today's sends against the cap, or a share of the
 *  rows that left. Nothing before any has. */
function tabDetail(batch: WhatsAppBatchResponse, tab: Tab): string | null {
  const sent = sentCount(batch);
  if (tab === "all" || tab === "ready" || tab === "skipped" || !sent) return null;
  if (tab === "sent")
    return usedToday(batch.sent_today, batch.send_daily_cap_today, batch.send_daily_cap);
  return `${Math.round((tabCount(batch, tab) / sent) * 100)}% of sent`;
}

/** The pace a batch sends at, in one line. */
function paceLabel(batch: WhatsAppBatchResponse): string {
  const gap = batch.send_gap_seconds;
  const cap = batch.send_daily_cap;
  const limit =
    cap === null
      ? ", no daily cap"
      : cap.kind === "fixed"
        ? `, at most ${cap.limit.toLocaleString()} a day`
        : `, ramping from ${cap.start.toLocaleString()} to ${cap.end.toLocaleString()} a day`;
  return `One every ${gap % 60 ? `${gap}s` : `${gap / 60} min`}${limit}`;
}

/* Starting: when, and at what pace, prefilled from the batch. The dialog says
   how long the send takes at that pace and cap before the button is pressed. */
function SendDialog({
  batch,
  onClose,
  onSent,
}: {
  batch: WhatsAppBatchResponse;
  onClose: () => void;
  onSent: (batch: WhatsAppBatchResponse) => void;
}) {
  const [terms, setTerms] = useState<SendTermsDraft>(() => termsFrom(batch, null));
  const [resend, setResend] = useState<ResendDraft>(() => resendFrom(batch));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const emptyWindow = windowNeverOpens(terms);
  const body = termsBody(terms);
  const savedCap = { cap: batch.send_daily_cap, today: batch.send_daily_cap_today };
  const span = sendSpan(
    batch.counts.ready,
    body.send_gap_seconds,
    terms.cap,
    limitToday(terms.cap, savedCap),
    body.window,
  );

  async function send() {
    setSaving(true);
    setErr("");
    try {
      onSent(
        await talqing.whatsapp.batches.send({
          batch_id: batch.id,
          sendWhatsAppBatchRequest: { ...body, ...resendBody(resend), start_at: startAtOf(terms) },
        }),
      );
    } catch (e) {
      setErr(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={`Start sending to ${batch.counts.ready.toLocaleString()} ${batch.counts.ready === 1 ? "person" : "people"}`}
      sub="Messages can't be recalled once sent."
      onClose={() => !saving && onClose()}
      width="max-w-[640px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={send} disabled={saving || emptyWindow || rampNeverRises(terms.cap)}>
            {terms.startMode === "at" ? "Schedule" : "Start sending"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 pb-2">
        {err && (
          <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] text-danger">
            {err}
          </div>
        )}
        <SendTermsFields
          value={terms}
          onChange={setTerms}
          showStart
          noun="message"
          savedCap={savedCap}
          trailing={<ResendRow value={resend} onChange={setResend} />}
        />
        <div className="grid gap-1 rounded-xl border border-line-2 bg-white px-3.5 py-3 text-[13px] leading-5 text-muted">
          <p>
            {terms.startMode === "at" ? "Starts when you said, and takes " : "Takes "}
            <strong className="font-semibold text-ink">{span.span}</strong>
            {span.why && ` — ${span.why}`}.
          </p>
          {terms.cap === null && (
            <p className="text-warn">
              No daily cap. WhatsApp refuses every message past your tier&rsquo;s daily limit.
            </p>
          )}
          <p>A new WhatsApp business can message 250 people a day; keep the daily cap at or under your tier.</p>
        </div>
        {emptyWindow && (
          <p className="rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
            Sending hours that start and end at the same time never open.
          </p>
        )}
      </div>
    </Modal>
  );
}

/* The pace, hours and cap, and the start while it has not come yet. Pacing
   reaches a batch already sending on its next message. */
/** Append rows to the batch. On a live or completed batch they are messaged without another press. */
function AddRows({
  batch,
  onAdded,
  onClose,
}: {
  batch: WhatsAppBatchResponse;
  onAdded: () => void;
  onClose: () => void;
}) {
  const [list, setList] = useState<ParsedList>(EMPTY_LIST);
  const note =
    batch.status === "draft"
      ? { text: "Added for review. Nothing is sent until you press Send." }
      : batch.status === "paused"
        ? { text: "Sent once you resume." }
        : { text: "Sent at this batch's pace and hours.", tone: "warn" as const };
  return (
    <AddRecipientsModal
      batchName={batch.name}
      count={list.rows.length}
      blocked={list.badColumns.length > 0}
      note={note}
      fixHint="They're in the batch as skipped. Fix the cell in the table and they're sent."
      submit={() =>
        api.addWhatsAppBatchRecipients(batch.id, {
          recipients: list.rows.map((input) => ({ input })),
        })
      }
      onAdded={onAdded}
      onClose={onClose}
    >
      <RecipientEditor
        list={list}
        onChange={setList}
        copy={listCopy(batch.template, { columns: batch.input_columns, toColumn: batch.to_column })}
      />
    </AddRecipientsModal>
  );
}

function EditPacing({
  batch,
  onClose,
  onSaved,
}: {
  batch: WhatsAppBatchResponse;
  onClose: () => void;
  onSaved: (batch: WhatsAppBatchResponse) => void;
}) {
  const scheduled = batch.status === "scheduled";
  const [terms, setTerms] = useState<SendTermsDraft>(() =>
    termsFrom(batch, scheduled ? batch.start_at : null),
  );
  const [resend, setResend] = useState<ResendDraft>(() => resendFrom(batch));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    setSaving(true);
    setErr("");
    try {
      onSaved(
        await talqing.whatsapp.batches.update({
          batch_id: batch.id,
          patchWhatsAppBatchRequest: {
            ...termsBody(terms),
            ...resendBody(resend),
            ...(scheduled ? { start_at: startAtOf(terms) } : {}),
          },
        }),
      );
    } catch (e) {
      setErr(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Sending settings"
      sub={
        batch.status === "sending"
          ? "Changes apply from the next message."
          : "How fast this batch goes out, and when."
      }
      onClose={() => !saving && onClose()}
      width="max-w-[640px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            onClick={save}
            disabled={saving || windowNeverOpens(terms) || rampNeverRises(terms.cap)}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 pb-2">
        {err && (
          <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] text-danger">
            {err}
          </div>
        )}
        <SendTermsFields
          value={terms}
          onChange={setTerms}
          showStart={scheduled}
          noun="message"
          savedCap={{ cap: batch.send_daily_cap, today: batch.send_daily_cap_today }}
          trailing={<ResendRow value={resend} onChange={setResend} />}
        />
        <p className="text-[12.5px] leading-5 text-muted">
          A new WhatsApp business can message 250 people a day; keep the daily cap at or under your tier.
        </p>
      </div>
    </Modal>
  );
}
