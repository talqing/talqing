"use client";

import { useState } from "react";
import type { EmailBatchResponse, EmailSendResponse } from "@talqing/sdk";
import { Badge, Button, Modal, Skeleton, useToast } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { hhmm, inZoneShort, timeAgo } from "@/app/telephony/outbound-calling/shared";
import {
  SEND_STATUS_LABEL,
  SEND_STATUS_VARIANT,
  SEND_WAIT_REASON,
  coversRows,
  isSendLive,
  paceSentence,
  pausedItself,
} from "./shared";
import { rampNeverRises } from "@/app/components/Pacing";
import { SendTermsFields, startAtOf, termsBody, termsFrom, windowNeverOpens } from "./SendTerms";

/* Every send this batch has made, and the controls for the live ones.
 *
 * This section is the point of the page. A send is a row now — it has a start
 * time, business hours, a pace, a ceiling and a lifecycle — and without
 * somewhere to see and steer it, all of that machinery is invisible and the
 * feature reads exactly like the fire-and-forget job it replaced. */

const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(140px,0.42fr)_minmax(150px,0.46fr)_minmax(190px,auto)]";

export function SendsSection({
  batch,
  sends,
  page,
  hasMore,
  blocked,
  onPage,
  onChanged,
  onSend,
}: {
  batch: EmailBatchResponse;
  /** Null while the first fetch is in flight. */
  sends: EmailSendResponse[] | null;
  page: number;
  hasMore: boolean;
  /** Why no new send can be created right now, or null when one can. */
  blocked: string | null;
  onPage: (page: number) => void;
  onChanged: () => void;
  onSend: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState<EmailSendResponse | null>(null);
  const [editing, setEditing] = useState<EmailSendResponse | null>(null);

  async function steer(run: () => Promise<unknown>, said: string) {
    setBusy(true);
    try {
      await run();
      toast({ msg: said, kind: "ok" });
      onChanged();
      setConfirmCancel(null);
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="grid min-w-0 gap-2.5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-display text-[15px] font-semibold leading-5 tracking-tight text-ink">
          Sends
        </h2>
        {!!sends?.length && (
          <span className="text-[12.5px] leading-5 text-muted">
            {sends.filter((s) => isSendLive(s.status)).length} live
          </span>
        )}
        {/* A disabled Button sets pointer-events:none, so its own title never
            fires — the wrapper is what hears the hover. */}
        <span className="ml-auto" title={blocked ?? undefined}>
          <Button size="sm" disabled={!!blocked} onClick={onSend}>
            New send
          </Button>
        </span>
      </div>

      {sends === null ? (
        <div className="grid gap-2 rounded-xl border border-line-2 bg-white p-4">
          <Skeleton className="h-4 w-[30%]" />
          <Skeleton className="h-3 w-[55%]" />
        </div>
      ) : sends.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line-2 bg-white px-4 py-6 text-center text-[13px] leading-5 text-muted">
          Nothing has been sent from this batch. Review the drafts below, pick the ones you want,
          and create a send — you can schedule it, pace it, and stop it at any point.
        </p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
          {/* Column headings, for the same reason the batch list has them: three
              rows of small grey text with no labels is a paragraph, not a table. */}
          <div
            className={cn(
              "hidden gap-x-4 border-b border-line bg-canvas px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
              ROW_GRID,
            )}
          >
            <span>Send</span>
            <span>Next</span>
            <span>Pace</span>
            <span className="sr-only">Actions</span>
          </div>
          {sends.map((send) => (
            <div
              key={send.id}
              className={cn(
                "grid items-center gap-x-4 gap-y-2 border-b border-line px-4 py-3 last:border-b-0",
                ROW_GRID,
              )}
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={SEND_STATUS_VARIANT[send.status]} dot={send.status === "sending"}>
                    {SEND_STATUS_LABEL[send.status]}
                  </Badge>
                  <span className="text-[13.5px] leading-5 text-ink">
                    <span className="font-semibold tabular-nums">
                      {send.counts.sent.toLocaleString()}
                    </span>
                    <span className="text-muted">
                      {/* A send of every row has no total while drafting runs,
                          so it says what it holds instead of a fraction. */}
                      {send.counts.total === null
                        ? " sent"
                        : ` of ${send.counts.total.toLocaleString()} sent`}
                    </span>
                  </span>
                  {send.counts.queued > 0 && (
                    <span className="text-[12px] leading-4 text-muted">
                      {send.counts.queued.toLocaleString()} ready
                    </span>
                  )}
                  {send.counts.drafting > 0 && (
                    <span className="text-[12px] leading-4 text-muted">
                      {send.counts.drafting.toLocaleString()} still drafting
                    </span>
                  )}
                  {send.counts.send_failed > 0 && (
                    <span className="text-[12px] leading-4 text-danger">
                      {send.counts.send_failed.toLocaleString()} failed
                    </span>
                  )}
                </div>
                <div
                  className="mt-0.5 truncate text-[12px] leading-4 text-faint"
                  title={send.failure_reason ?? undefined}
                >
                  {send.failure_reason ? (
                    /* A breaker pause keeps its rows covered and resumes from
                       where it stopped, so it is a warning and not a death —
                       and saying "Stopped" over a send that lost nothing sends
                       the operator looking for work to redo. */
                    pausedItself(send) ? (
                      <span className="text-warn">
                        Paused itself — {send.failure_reason}. Nothing was lost; Resume carries on.
                      </span>
                    ) : (
                      <span className="text-danger">Stopped — {send.failure_reason}</span>
                    )
                  ) : (
                    <>
                      <span className="font-mono">{send.from_email}</span>
                      {send.scope === "all" && " · every row, as it is drafted"}
                      {" · created "}
                      {timeAgo(send.created_at)}
                    </>
                  )}
                </div>
              </div>

              <div className="min-w-0 text-[12.5px] leading-4">
                {/* The one line that says why nothing is happening. The send
                    itself names the gate holding it — six of them — so this is
                    read rather than inferred. Waiting for drafting is the one
                    with no time to show. */}
                {send.next_send_reason === "drafting" ? (
                  <>
                    <div className="truncate text-ink-soft">Waiting for drafting</div>
                    <div className="truncate text-faint">sends each row once drafted</div>
                  </>
                ) : send.next_send_at ? (
                  <>
                    <div className="truncate text-ink-soft">
                      {inZoneShort(send.next_send_at, send.timezone)}
                    </div>
                    <div
                      className="truncate text-faint"
                      title={send.timezone.replace(/_/g, " ")}
                    >
                      {send.next_send_reason
                        ? SEND_WAIT_REASON[send.next_send_reason]
                        : send.timezone.replace(/_/g, " ")}
                    </div>
                  </>
                ) : send.finished_at ? (
                  <div className="truncate text-faint">Finished {timeAgo(send.finished_at)}</div>
                ) : (
                  <div className="truncate text-faint">
                    {send.status === "sending" ? "Sending now" : "Nothing scheduled"}
                  </div>
                )}
              </div>

              <div className="min-w-0 text-[12.5px] leading-4 text-muted">
                <div className="truncate">
                  {send.send_gap_seconds === 1
                    ? "One a second"
                    : send.send_gap_seconds < 60
                      ? `One every ${send.send_gap_seconds}s`
                      : `One every ${Math.round(send.send_gap_seconds / 60)} min`}
                </div>
                <div className="truncate text-faint">
                  {send.window
                    ? `${hhmm(send.window.start)}–${hhmm(send.window.end)}`
                    : "any hour"}
                  {send.send_daily_cap_today !== null &&
                    ` · ${send.send_daily_cap_today.toLocaleString()}/day`}
                  {send.send_daily_cap?.kind === "ramp" &&
                    ` → ${send.send_daily_cap.end.toLocaleString()}`}
                </div>
              </div>

              <div className="flex flex-wrap justify-end gap-1.5">
                {/* Editable while it still covers rows — paused included, which
                    is exactly when somebody wants to change the hours or the
                    pace before letting it carry on. */}
                {coversRows(send.status) && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => setEditing(send)}
                  >
                    Edit
                  </Button>
                )}
                {isSendLive(send.status) && (
                  <>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      onClick={() =>
                        steer(
                          () => api.pauseEmailSend(batch.id, send.id),
                          "Paused. It stops on the next email.",
                        )
                      }
                    >
                      Pause
                    </Button>
                  </>
                )}
                {send.status === "paused" && (
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() =>
                      steer(() => api.resumeEmailSend(batch.id, send.id), "Sending again.")
                    }
                  >
                    Resume
                  </Button>
                )}
                {(isSendLive(send.status) || send.status === "paused") && (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy}
                    onClick={() => setConfirmCancel(send)}
                  >
                    Cancel
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {(page > 0 || hasMore) && (
        <div className="flex items-center justify-end gap-2 text-[12.5px] leading-5 text-muted">
          <span>Page {page + 1}</span>
          <Button
            variant="secondary"
            size="sm"
            disabled={page === 0}
            onClick={() => onPage(page - 1)}
          >
            Newer
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!hasMore}
            onClick={() => onPage(page + 1)}
          >
            Older
          </Button>
        </div>
      )}

      {confirmCancel && (
        <Modal
          title="Cancel this send?"
          onClose={() => !busy && setConfirmCancel(null)}
          width="max-w-[520px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmCancel(null)} disabled={busy}>
                Keep sending
              </Button>
              {/* Not a danger button, and that is deliberate: cancelling gives
                  the drafts back. Painting it red would make the one recoverable
                  action on this page read as the destructive one. */}
              <Button
                disabled={busy}
                onClick={() =>
                  steer(
                    () => api.cancelEmailSend(batch.id, confirmCancel.id),
                    "Canceled. The unsent rows are drafts again.",
                  )
                }
              >
                Cancel this send
              </Button>
            </>
          }
        >
          <div className="grid gap-2.5 text-[14px] leading-6 text-ink-soft">
            <p>
              The{" "}
              <strong className="font-semibold text-ink">
                {confirmCancel.counts.queued.toLocaleString()}
              </strong>{" "}
              {confirmCancel.counts.queued === 1 ? "draft that has" : "drafts that have"} not gone
              yet stay drafts, exactly as they are, ready to go into another send.
              {confirmCancel.counts.drafting > 0 &&
                ` So do the ${confirmCancel.counts.drafting.toLocaleString()} rows still drafting, once they land.`}
            </p>
            <p>
              {confirmCancel.counts.sent > 0
                ? `The ${confirmCancel.counts.sent.toLocaleString()} already sent stay sent — nothing recalls an email.`
                : "Nothing has been sent by it yet."}{" "}
              A row that is mid-send right now finishes.
            </p>
            <p className="text-muted">
              This is also how to change what a send covers: cancel it and create another.
            </p>
          </div>
        </Modal>
      )}

      {editing && (
        <EditSend
          batch={batch}
          send={editing}
          onSaved={() => {
            setEditing(null);
            onChanged();
            toast({ msg: "Saved. The next pass uses it.", kind: "ok" });
          }}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}

/** Re-steer one send: its hours, its pace, its ceiling — and its start, while
 *  it has not begun.
 *
 *  Not its sender and not what it covers — a send that went out under two
 *  addresses would be two sends with one name, and changing what it covers is
 *  what cancel-and-create is for. */
function EditSend({
  batch,
  send,
  onSaved,
  onClose,
}: {
  batch: EmailBatchResponse;
  send: EmailSendResponse;
  onSaved: () => void;
  onClose: () => void;
}) {
  // The API moves `start_at` only while the send is `scheduled`; after that its
  // start is history, so the control is not offered.
  const startEditable = send.status === "scheduled";
  const [terms, setTerms] = useState(() => termsFrom(send, send.start_at));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const neverOpens = windowNeverOpens(terms);
  const noRamp = rampNeverRises(terms.cap);

  async function save() {
    setSaving(true);
    setErr("");
    try {
      await api.patchEmailSend(batch.id, send.id, {
        ...termsBody(terms),
        ...(startEditable ? { start_at: startAtOf(terms) } : {}),
      });
      onSaved();
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Re-steer this send"
      sub={
        send.status === "paused"
          ? "It stays paused. Resume it when you are ready and it carries on under these terms."
          : send.status === "scheduled"
            ? "It has not started yet. What it covers cannot change — cancel it and create another for that."
            : "It picks these up within a few minutes. What it covers cannot change — cancel it and create another for that."
      }
      onClose={() => !saving && onClose()}
      width="max-w-[640px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving || neverOpens || noRamp}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <SendTermsFields
          value={terms}
          onChange={setTerms}
          showStart={startEditable}
          savedCap={{ cap: send.send_daily_cap, today: send.send_daily_cap_today }}
        />
        <p className="text-[13px] leading-5 text-muted">
          {paceSentence(terms.gap, send.counts.queued || 1)}
        </p>
        {neverOpens && (
          <p className="rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
            Sending hours that start and end at the same time never open.
          </p>
        )}
        {err && (
          <p className="rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
            {err}
          </p>
        )}
      </div>
    </Modal>
  );
}
