"use client";

import { useEffect, useState } from "react";
import type { EmailBatchResponse, EmailRecipientResponse } from "@talqing/sdk";
import { Badge, Button, Input, Modal, Textarea } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { hhmm, timeAgo } from "@/app/telephony/outbound-calling/shared";
import { ROW_STATUS_LABEL, ROW_STATUS_VARIANT, isEditableRow, isSendableRow } from "./shared";

/* The email, as it will actually be sent — and the place to fix it and send it.
 *
 * A 300-word body in a table cell is not a review surface, so this renders the
 * same row the way its recipient will see it. Everything else here follows from
 * that: the moment a person can actually READ the email is the moment they can
 * judge it, so this is where the two verbs that judgement leads to belong —
 * change a word, and send this one.
 *
 * The table still edits cells in place, and that is the right tool for walking a
 * column fixing addresses. This is the other half: one row, all three mapped
 * fields at once, saved in a single request so the row's sendability is
 * re-decided once rather than three times.
 *
 * HTML goes into a sandboxed iframe rather than into this document. It is a
 * tenant's own content, and a task that emitted a `<script>` would otherwise run
 * it inside the dashboard — and `srcDoc` with no `allow-scripts` renders it
 * exactly as a mail client would anyway. */

export function RowPreview({
  batch,
  row,
  index,
  total,
  onMove,
  onSave,
  onSent,
  onClose,
}: {
  batch: EmailBatchResponse;
  row: EmailRecipientResponse;
  /** Position in the page currently on screen, 0-based. */
  index: number;
  total: number;
  onMove: (delta: number) => void;
  /** Writes the overrides and replaces this row in the page's state. Throws. */
  onSave: (row: EmailRecipientResponse, overrides: Record<string, string>) => Promise<void>;
  onSent: (address: string) => void;
  onClose: () => void;
}) {
  const map = batch.field_map;
  const value = (column: string) => {
    const raw = row.columns[column];
    return raw == null ? "" : String(raw);
  };
  const to = value(map.to);
  const subject = value(map.subject);
  const body = value(map.body);
  const sender = batch.from_name ? `${batch.from_name} <${batch.from_email}>` : batch.from_email;

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ to, subject, body });
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // Previous/Next puts a different email in the same dialog. Nothing typed
  // about the last one may survive that, and neither may a half-pressed send.
  useEffect(() => {
    setEditing(false);
    setConfirming(false);
    setErr("");
  }, [row.id]);

  // Not bound while editing, and never when the keystroke is going into a
  // field: `j` is "next row" on a reading surface and the letter j in a subject
  // line everywhere else.
  useEffect(() => {
    if (editing) return;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      if (e.key === "ArrowDown" || e.key === "j") onMove(1);
      if (e.key === "ArrowUp" || e.key === "k") onMove(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onMove, editing]);

  const editable = isEditableRow(row.status);
  const sendable = isSendableRow(row);
  const dirty = draft.to !== to || draft.subject !== subject || draft.body !== body;
  const capSpent =
    batch.send_daily_cap_today !== null && batch.sent_today >= batch.send_daily_cap_today;

  async function save() {
    setBusy(true);
    setErr("");
    try {
      // Only what actually changed. An override is not just a value, it is a
      // claim that a PERSON chose it: it is marked in the table, it shadows the
      // model's own value and it survives a redraft. Writing all three every
      // time would quietly pin a subject nobody touched against the next
      // redraft. Keyed by COLUMN, so a field map pointing two of the three at
      // one column writes it once rather than racing itself.
      const changed: Record<string, string> = {};
      if (draft.to !== to) changed[map.to] = draft.to;
      if (draft.subject !== subject) changed[map.subject] = draft.subject;
      if (draft.body !== body) changed[map.body] = draft.body;
      await onSave(row, changed);
      setEditing(false);
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    setBusy(true);
    setErr("");
    try {
      await api.createEmailSend(batch.id, {
        scope: "selected",
        recipient_ids: [row.id],
        // A person pressing send on one email they have just read in full IS
        // the attendance the business-hours window stands in for, so it does
        // not apply here and neither does a start time. The DAILY CAP is
        // deliberately left to inherit: it is about how much this domain emits
        // today, and sending one at a time is not an exemption from that.
        start_at: null,
        window: null,
      });
      setConfirming(false);
      onSent(to);
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Row ${row.row_number}`}
      sub={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Badge variant={ROW_STATUS_VARIANT[row.status]}>{ROW_STATUS_LABEL[row.status]}</Badge>
          {row.task_version != null && (
            <span className="text-[12.5px] text-muted">
              drafted by v{row.task_version}
              {batch.published_task_version != null &&
                row.task_version < batch.published_task_version &&
                ` · v${batch.published_task_version} is published now`}
            </span>
          )}
          {row.sent_at && (
            <span className="text-[12.5px] text-muted">
              went out {timeAgo(row.sent_at)}
              {row.sent_from && ` from ${row.sent_from}`}
            </span>
          )}
        </span>
      }
      onClose={() => !busy && onClose()}
      width="max-w-[760px]"
      footer={
        editing ? (
          <>
            <span className="mr-auto text-[12.5px] text-muted">
              Saved as an edit beside what the task wrote — a redraft will not discard it.
            </span>
            <Button variant="secondary" disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button disabled={busy || !dirty} onClick={save}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </>
        ) : confirming && sendable ? (
          <>
            <Button variant="secondary" disabled={busy} onClick={() => setConfirming(false)}>
              Back
            </Button>
            <Button disabled={busy} onClick={send}>
              {busy ? "Sending…" : `Send to ${to}`}
            </Button>
          </>
        ) : (
          <>
            <span className="mr-auto text-[12.5px] tabular-nums text-muted">
              {index + 1} of {total} on this page
            </span>
            <Button variant="secondary" disabled={index === 0} onClick={() => onMove(-1)}>
              Previous
            </Button>
            <Button variant="secondary" disabled={index >= total - 1} onClick={() => onMove(1)}>
              Next
            </Button>
            {editable && (
              <Button
                variant="secondary"
                onClick={() => {
                  setDraft({ to, subject, body });
                  setEditing(true);
                }}
              >
                Edit
              </Button>
            )}
            {sendable && <Button onClick={() => setConfirming(true)}>Send this one</Button>}
          </>
        )
      }
    >
      <div className="grid gap-4">
        {row.not_ready_reason && !editing && (
          <p className="rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-2 text-[13px] leading-5 text-warn-ink">
            This row cannot be sent: {row.not_ready_reason}
            {editable && " — Edit fills it in."}
          </p>
        )}
        {/* `last_error` on an unsendable draft is the same sentence the line
            above just said, because that is what wrote it. Only a DIFFERENT
            error is news — a provider's refusal, a send that failed. */}
        {row.last_error && row.last_error !== row.not_ready_reason && !editing && (
          <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[13px] leading-5 text-danger">
            {row.last_error}
          </p>
        )}

        {/* Only what the operator cannot already see. The email is on the screen
            and the address is written on the button, so saying it mails a real
            person is telling somebody reading an email that it is an email.
            What is NOT on screen is the timing: that this one leaves outside the
            batch's hours, and that a spent ceiling holds it until tomorrow. */}
        {confirming && sendable && (batch.window || capSpent) && (
          <div className="grid gap-1.5 rounded-xl border border-line-2 bg-white px-3.5 py-3 text-[13px] leading-5 text-muted">
            {batch.window && (
              <p>
                Goes straight away, outside the batch&rsquo;s sending hours of{" "}
                {hhmm(batch.window.start)}–{hhmm(batch.window.end)}.
              </p>
            )}
            {capSpent && (
              <p className="text-warn-ink">
                Today&rsquo;s ceiling of {batch.send_daily_cap_today?.toLocaleString()} is already spent,
                so this one waits until after midnight {batch.timezone.replace(/_/g, " ")}.
              </p>
            )}
          </div>
        )}

        {err && (
          <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[13px] leading-5 text-danger">
            {err}
          </p>
        )}

        {/* The same box in both modes, so pressing Edit changes what you can do
            to the header rather than where the header is. */}
        <dl className="grid gap-2 rounded-xl border border-line-2 bg-canvas px-3.5 py-3 text-[13px] leading-5">
          <Line label="From" value={sender} mono />
          {editing ? (
            <EditLine label="To" column={map.to}>
              <Input
                value={draft.to}
                onChange={(e) => setDraft({ ...draft, to: e.target.value })}
                disabled={busy}
                spellCheck={false}
                placeholder="someone@example.com"
                className="min-h-9 py-1.5 font-mono text-[12.5px]"
                aria-label="Address this goes to"
              />
            </EditLine>
          ) : (
            <Line label="To" value={to || "— nothing resolved"} mono />
          )}
          {batch.reply_to && <Line label="Reply-to" value={batch.reply_to} mono />}
          {editing ? (
            <EditLine label="Subject" column={map.subject}>
              <Input
                value={draft.subject}
                onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                disabled={busy}
                className="min-h-9 py-1.5 text-[13px]"
                aria-label="Subject line"
              />
            </EditLine>
          ) : (
            <Line label="Subject" value={subject || "— nothing resolved"} />
          )}
        </dl>

        {editing ? (
          <Textarea
            value={draft.body}
            onChange={(e) => setDraft({ ...draft, body: e.target.value })}
            disabled={busy}
            className="h-[420px] text-[13.5px] leading-6"
            aria-label={batch.body_format === "html" ? "Message body, as HTML" : "Message body"}
            placeholder={
              batch.body_format === "html" ? "The HTML this email is made of" : "What it says"
            }
          />
        ) : batch.body_format === "html" ? (
          <iframe
            title={`Row ${row.row_number} body`}
            sandbox=""
            srcDoc={body}
            className="h-[420px] w-full rounded-xl border border-line-2 bg-white"
          />
        ) : (
          <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-xl border border-line-2 bg-white px-4 py-3.5 font-sans text-[13.5px] leading-6 text-ink">
            {body || "— nothing resolved"}
          </pre>
        )}
      </div>
    </Modal>
  );
}

function Line({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 gap-3">
      <dt className="w-[72px] flex-none text-muted">{label}</dt>
      <dd className={mono ? "min-w-0 break-all font-mono text-[12.5px] text-ink" : "min-w-0 text-ink"}>
        {value}
      </dd>
    </div>
  );
}

/** The same row, holding a field. The column's name goes under it: an operator
 *  typing an address here is editing a named cell the table also shows, and the
 *  two surfaces should not look like two different places to put it. */
function EditLine({
  label,
  column,
  children,
}: {
  label: string;
  column: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 gap-3">
      <dt className="w-[72px] flex-none pt-2 text-muted">{label}</dt>
      <dd className="min-w-0 flex-1">
        {children}
        <span className="mt-1 block font-mono text-[11.5px] text-faint">{column}</span>
      </dd>
    </div>
  );
}
