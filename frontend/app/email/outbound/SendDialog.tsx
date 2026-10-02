"use client";

import { useEffect, useState } from "react";
import type { CreateEmailSendRequest, EmailBatchResponse, EmailSendResponse } from "@talqing/sdk";
import { Button, Input, Modal } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { TermsRow, limitToday, rampNeverRises } from "@/app/components/Pacing";
import { timeAgo } from "@/app/telephony/outbound-calling/shared";
import {
  SendTermsFields,
  startAtOf,
  termsBody,
  termsFrom,
  windowNeverOpens,
} from "./SendTerms";
import { isDraftingOn, isSendableRow, paceSentence, sendSpan } from "./shared";

/* The last thing between a person and a stranger's inbox.
 *
 * This dialog carries the safeguard that replaced the old fifty-row cap. That
 * cap was friction, not a guarantee — 5 000 rows is a hundred clicks of "select
 * all on page", which is not a hundred acts of reading. What actually protects a
 * sending domain is a RATE, so the two numbers this dialog puts in front of the
 * operator are the pace and the daily ceiling, and it says in plain words how
 * many days the send will take before they can press the button. */

export type SendSelection =
  /** Rows a person ticked in the table. */
  | { kind: "ids"; ids: string[] }
  /** "New send": the dialog works out what can go, and asks how much. */
  | { kind: "batch" };

type Scope = CreateEmailSendRequest["scope"];

export function SendDialog({
  batch,
  selection,
  prior,
  othersLive,
  onSent,
  onClose,
}: {
  batch: EmailBatchResponse;
  selection: SendSelection;
  /** The send this one replaces — the newest, if it was stopped. Its terms are
   *  what the operator last chose, and the batch's defaults may not be. */
  prior: EmailSendResponse | null;
  /** Whether another send of this batch still covers rows, which rules out a
   *  send of every row. */
  othersLive: boolean;
  onSent: (send: EmailSendResponse, rejected: number) => void;
  onClose: () => void;
}) {
  /* A send of every row only means something while drafting can still produce
     rows. Once it cannot, "every row" and "the drafts ready now" are the same
     rows, and asking would be a question with one answer. */
  const offerStanding = selection.kind === "batch" && isDraftingOn(batch.status);
  const stillDrafting = offerStanding ? batch.counts.pending + batch.counts.drafting : 0;
  const [scope, setScope] = useState<Scope>(
    offerStanding && batch.counts.draft === 0 && !othersLive ? "all" : "selected",
  );

  /* "The drafts ready now" is a list of ids, fetched here and sent as-is. It
     used to be resolved by the API on submit, so the dialog could say "Send
     312" and queue 340 that landed while the form was open. */
  const [readyIds, setReadyIds] = useState<string[] | null>(
    selection.kind === "ids" ? selection.ids : null,
  );

  // Prefilled from the send being replaced, not from the batch: cancel-and-
  // recreate used to take the batch's 15-minute pace for a send that had been
  // running at 30, and quietly double the rate.
  const [from, setFrom] = useState(prior?.from_email ?? batch.from_email);
  const [terms, setTerms] = useState(() => termsFrom(prior ?? batch, null));
  const [domains, setDomains] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!batch.integration_id) return;
    api
      .listEmailSenders(batch.integration_id)
      .then((res) => setDomains(res.domains))
      // Not fatal: the API re-checks the domain before creating anything, so the
      // worst case is a 400 in this dialog instead of a warning under the field.
      .catch(() => setDomains([]));
  }, [batch.integration_id]);

  useEffect(() => {
    if (selection.kind !== "batch") return;
    let current = true;
    (async () => {
      // Page after page, deduplicated: drafts landing underneath can shift the
      // offsets, and a row seen twice must not be sent twice.
      const ids = new Set<string>();
      for (let more = true, offset = 0; more; ) {
        const res = await api.listEmailBatchRecipients(batch.id, {
          status: "draft",
          limit: 200,
          offset,
        });
        for (const row of res.items) if (isSendableRow(row)) ids.add(row.id);
        more = res.has_more;
        offset += res.items.length;
      }
      if (current) setReadyIds([...ids]);
    })().catch((e: unknown) => current && setErr(apiErrorMessage(e, "Could not list the drafts.")));
    return () => {
      current = false;
    };
  }, [batch.id, selection.kind]);

  const ready = readyIds?.length ?? batch.counts.draft;
  // A standing send's size is a moving number: what is ready now plus what is
  // still being written, some of which will fail. Said as "up to".
  const count = scope === "all" ? ready + stillDrafting : ready;

  const domain = from.trim().split("@")[1]?.toLowerCase() ?? "";
  const unverified =
    domains !== null && domains.length > 0 && domain !== "" && !domains.includes(domain);
  const changedSender = from.trim() !== batch.from_email;
  const emptyWindow = windowNeverOpens(terms);

  // How much of today's ceiling is already gone. A send created at 16:00 on a
  // day that has already used 190 of 200 is not going to do what its author
  // thinks, and the arithmetic below is the only place that shows.
  // A send that keeps the batch's ramp keeps its progress, so today's limit is
  // read against where the batch's ramp has got to.
  const savedCap = { cap: batch.send_daily_cap, today: batch.send_daily_cap_today };
  const capToday = limitToday(terms.cap, savedCap);
  const capLeft = capToday === null ? null : Math.max(capToday - batch.sent_today, 0);
  const pacing = termsBody(terms);
  const span = sendSpan(count, pacing.send_gap_seconds, terms.cap, capToday, pacing.window);

  async function submit() {
    setBusy(true);
    setErr("");
    const body: CreateEmailSendRequest = {
      scope,
      ...(scope === "selected" ? { recipient_ids: readyIds ?? [] } : {}),
      ...(changedSender ? { from_email: from.trim() } : {}),
      // The rest of the replaced send's sender, which this dialog has no field
      // for and which would otherwise silently fall back to the batch's.
      ...(prior && prior.from_name !== batch.from_name ? { from_name: prior.from_name } : {}),
      ...(prior && prior.reply_to !== batch.reply_to ? { reply_to: prior.reply_to } : {}),
      start_at: startAtOf(terms),
      ...termsBody(terms),
    };
    try {
      const res = await api.createEmailSend(batch.id, body);
      onSent(res.send, res.rejected.length);
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const emails = (n: number) => `${n.toLocaleString()} ${n === 1 ? "email" : "emails"}`;
  const title =
    scope === "all"
      ? "Send every row as it is drafted?"
      : selection.kind === "ids"
        ? `Send ${emails(ready)}?`
        : `Send the ${ready.toLocaleString()} ${ready === 1 ? "draft" : "drafts"} ready now?`;

  return (
    <Modal
      title={title}
      sub={
        prior ? (
          <>
            Sender, hours, pace and daily cap are copied from the send{" "}
            {prior.status === "canceled" ? "you canceled" : "that stopped"}
            {prior.finished_at ? ` ${timeAgo(prior.finished_at)}` : ""}.
          </>
        ) : undefined
      }
      onClose={() => !busy && onClose()}
      width="max-w-[640px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Back
          </Button>
          <Button
            onClick={submit}
            disabled={
              busy ||
              unverified ||
              emptyWindow ||
              rampNeverRises(terms.cap) ||
              (scope === "selected" && (readyIds === null || readyIds.length === 0))
            }
          >
            {busy
              ? "Creating…"
              : terms.startMode === "at"
                ? "Schedule this send"
                : scope === "all"
                  ? "Start sending"
                  : `Send ${ready.toLocaleString()}`}
          </Button>
        </>
      }
    >
      <div className="grid gap-4 text-[14px] leading-6 text-ink-soft">
        {offerStanding && (
          <div role="radiogroup" aria-label="What this send covers" className="grid gap-2">
            <ScopeOption
              checked={scope === "selected"}
              onSelect={() => setScope("selected")}
              disabled={ready === 0}
              title={
                readyIds === null
                  ? "Only the drafts ready now"
                  : `Only the ${ready.toLocaleString()} ${ready === 1 ? "draft" : "drafts"} ready now`
              }
              body={
                ready === 0
                  ? "Nothing has finished drafting yet."
                  : "Exactly these rows. Anything drafted later waits for another send."
              }
            />
            <ScopeOption
              checked={scope === "all"}
              onSelect={() => setScope("all")}
              disabled={othersLive}
              title="Every row, as it finishes drafting"
              body={
                othersLive
                  ? "Not while another send of this batch is live — it would cover the same rows."
                  : `${ready.toLocaleString()} ready now, ${stillDrafting.toLocaleString()} still drafting. Keeps going until drafting is done.`
              }
            />
          </div>
        )}

        <p>
          <strong className="font-semibold text-ink">
            {scope === "all" ? "Up to " : ""}
            {count.toLocaleString()} real {count === 1 ? "email" : "emails"} to{" "}
            {count === 1 ? "a person" : "people"} who did not ask you just now.
          </strong>{" "}
          {scope === "all" ? (
            <>
              <span className="text-warn-ink">
                Each draft goes out as soon as it is written, without anyone reading it first.
              </span>{" "}
              You can pause or cancel the send at any point; nothing already sent can be
              recalled.
            </>
          ) : (
            "This cannot be undone, recalled or edited afterwards — though you can pause the send while it is going, and cancel it to stop the rest."
          )}
        </p>

        <SendTermsFields
          value={terms}
          onChange={setTerms}
          showStart
          savedCap={savedCap}
          leading={
            <TermsRow label="From">
              <Input
                value={from}
                onChange={(e) => setFrom(e.target.value)}
                className="w-full max-w-[320px] font-mono text-[13px]"
                aria-label="Address these go from"
              />
              <span className="basis-full text-[12.5px] leading-5 text-muted">
                {unverified ? (
                  <span className="text-danger">
                    {domain} is not verified on {batch.integration_name}. Verified:{" "}
                    {domains?.join(", ")}
                  </span>
                ) : changedSender ? (
                  <>
                    This send only — the batch keeps{" "}
                    <span className="font-mono">{batch.from_email}</span> as its default.
                  </>
                ) : (
                  <>
                    The batch&rsquo;s own address.
                    {domains?.length ? ` Verified: ${domains.join(", ")}` : ""}
                  </>
                )}
              </span>
            </TermsRow>
          }
        />

        {/* The whole point of the dialog: the consequence, in words, before the
            button. A 1 240-row send at 45 seconds is fifteen hours of sending —
            and nine working days once a daily cap and business hours are on. */}
        <div className="grid gap-1 rounded-xl border border-line-2 bg-white px-3.5 py-3 text-[13px] leading-5">
          <p className="text-muted">
            {paceSentence(terms.gap, 1)}{" "}
            {terms.startMode === "at" ? "Starts when you said, and takes " : "Takes "}
            <strong className="font-semibold text-ink">{span.span}</strong>
            {scope === "all" ? " once every row is drafted" : ""}
            {span.why && ` — ${span.why}`}.
          </p>
          {capLeft !== null && batch.sent_today > 0 && (
            <p className="text-muted">
              {batch.sent_today.toLocaleString()} already went out today, so{" "}
              {capLeft.toLocaleString()} of the ceiling is left before midnight.
            </p>
          )}
          {terms.cap === null && (
            <p className="text-warn-ink">
              No daily ceiling. A young sending domain that emits thousands in an afternoon is one
              nobody&rsquo;s inbox trusts afterwards.
            </p>
          )}
        </div>

        {emptyWindow && (
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

/** One of the two answers to "what does this send cover", as a pickable card.
 *  A native radio underneath, so arrows, Space and screen readers all work. */
function ScopeOption({
  checked,
  onSelect,
  disabled,
  title,
  body,
}: {
  checked: boolean;
  onSelect: () => void;
  disabled?: boolean;
  title: string;
  body: string;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-3 rounded-xl border px-3.5 py-3 transition-colors",
        checked ? "border-ink bg-white ring-1 ring-ink" : "border-line-2 bg-white hover:border-line-strong",
        disabled && "cursor-not-allowed opacity-55 hover:border-line-2",
      )}
    >
      <input
        type="radio"
        name="send-scope"
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
        className="mt-1 h-4 w-4 flex-none accent-ink"
      />
      <span className="grid gap-0.5">
        <span className="text-[14px] font-semibold leading-5 text-ink">{title}</span>
        <span className="text-[13px] leading-5 text-muted">{body}</span>
      </span>
    </label>
  );
}
