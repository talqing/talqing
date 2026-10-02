"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  AgentResponse,
  CallBatchRecipientResponse,
  CallBatchRecipientStatus,
  CallBatchResponse,
  PhoneNumberResponse,
} from "@talqing/sdk";
import {
  Badge,
  Button,
  ListSkeleton,
  Modal,
  Segment,
  useToast,
} from "@/app/components/ui";
import { usePublishedVars } from "@/app/components/SessionVars";
import { AddRecipientsModal } from "@/app/components/AddRecipientsModal";
import { usedToday } from "@/app/components/Pacing";
import { DeleteBatchModal } from "@/app/components/DeleteBatchModal";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import {
  PolicyFields,
  type PolicyDraft,
  draftFromBatch,
  policyProblems,
  toPatchRequest,
} from "./PolicyFields";
import { BatchProgress } from "./BatchProgress";
import { RecipientEditor } from "./RecipientEditor";
import { type RecipientRow, applyServerErrors } from "./recipients";
import {
  BATCH_STATUS_LABEL,
  BATCH_STATUS_VARIANT,
  RECIPIENT_STATUS_LABEL,
  RECIPIENT_STATUS_VARIANT,
  hhmm,
  asSentence,
  inZone,
  isCancelable,
  isSteerable,
  zoneAbbreviation,
} from "./shared";

const PAGE_SIZE = 50;

const TABS: { value: CallBatchRecipientStatus | "all"; label: string }[] = [
  { value: "all", label: "Everyone" },
  { value: "pending", label: "Not called" },
  { value: "dialing", label: "Calling" },
  { value: "completed", label: "Reached" },
  { value: "failed", label: "Failed" },
  { value: "canceled", label: "Canceled" },
];

/**
 * One batch, over the list, driven by `?id=` so it survives a refresh and can be
 * shared. The recipients table is paginated server-side: ten thousand rows is a
 * normal batch, not an edge case.
 */
export function BatchDetail({
  batch,
  agents,
  numbers,
  onChanged,
  onDeleted,
  onClose,
}: {
  batch: CallBatchResponse;
  agents: AgentResponse[];
  numbers: PhoneNumberResponse[];
  onChanged: (batch: CallBatchResponse) => void;
  onDeleted: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [tab, setTab] = useState<CallBatchRecipientStatus | "all">("all");
  const [recipients, setRecipients] = useState<CallBatchRecipientResponse[]>([]);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => setPage(0), [tab]);

  const load = useCallback(() => {
    setLoading(true);
    api
      .listCallBatchRecipients(batch.id, {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        status: tab === "all" ? undefined : tab,
      })
      .then((res) => {
        setRecipients(res.items);
        setHasMore(res.has_more);
        setErr("");
      })
      .catch((e) => setErr(apiErrorMessage(e, "Could not load this batch's recipients.")))
      .finally(() => setLoading(false));
    // `batch.status` is a real dependency, not defensive: a recipient's status is
    // computed on read from the pair (batch status, row status), so cancelling a
    // batch changes every untouched row's answer without touching a single row.
    // Without this the counts would say "canceled 4" beside four rows still
    // reading "not called", which is the same fact disagreeing with itself.
  }, [batch.id, batch.status, page, tab]);

  useEffect(() => load(), [load]);

  // A running batch keeps moving. Refresh both halves while it does, and stop
  // the moment it does not — a completed batch is a document, not a dashboard.
  useEffect(() => {
    if (batch.status !== "running" && batch.status !== "scheduled") return;
    const timer = window.setInterval(() => {
      api.getCallBatch(batch.id).then(onChanged).catch(() => {});
      load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [batch.id, batch.status, onChanged, load]);

  async function steer(
    action: "pause" | "resume" | "cancel",
    run: () => Promise<CallBatchResponse>,
  ) {
    setBusy(true);
    try {
      onChanged(await run());
      toast({
        msg:
          action === "pause"
            ? batch.status === "completed"
              ? "Paused. Anyone you add waits until you resume."
              : "Paused. Calls already in progress will finish."
            : action === "resume"
              ? "Running again."
              : "Canceled. Calls already ringing will finish.",
        kind: "ok",
      });
      setConfirmCancel(false);
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  }

  const zone = useMemo(() => zoneAbbreviation(batch.start_at, batch.timezone), [batch]);

  return (
    <Modal
      title={batch.name}
      sub={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={batch.agent_plan && !batch.agent_id ? "italic" : undefined}>
            {batch.agent_name ?? "Agent deleted"}
          </span>
          {/* Every call this campaign places runs the same plan, validated once
              when it was created — so "which script did this campaign use" is
              answerable from here rather than from the API. An inline entry
              agent has no row to link to, hence the italic above. */}
          {batch.agent_plan && (
            <Badge
              variant="info"
              title={
                (batch.agent_plan?.members?.length ?? 1) > 1
                  ? `${(batch.agent_plan?.members ?? []).map((m) => m.name).join(" → ")}. The first answers.`
                  : "This campaign runs a modified copy of the agent, pinned when it was created — republishing the agent does not change it."
              }
            >
              {(batch.agent_plan?.members?.length ?? 1) > 1
                ? `Team · ${batch.agent_plan?.members?.length}`
                : batch.agent_id
                  ? "Overridden"
                  : "Inline"}
            </Badge>
          )}
          <span className="text-line-strong">·</span>
          <span className="font-mono text-[12.5px]">{batch.from_e164 ?? "Number deleted"}</span>
          <span className="text-line-strong">·</span>
          <span>{batch.timezone.replace(/_/g, " ")}</span>
        </span>
      }
      onClose={onClose}
      width="max-w-[900px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
          {!isCancelable(batch.status) && (
            <Button variant="danger" className="mr-auto" onClick={() => setConfirmDelete(true)}>
              Delete batch
            </Button>
          )}
          <Link href={`/calls?batch_id=${batch.id}`}>
            <Button variant="secondary">See the calls</Button>
          </Link>
          {isSteerable(batch.status) && (
            <>
              <Button variant="secondary" onClick={() => setEditing(true)}>
                Edit
              </Button>
              {batch.status === "paused" ? (
                <Button disabled={busy} onClick={() => steer("resume", () => api.resumeCallBatch(batch.id))}>
                  Resume
                </Button>
              ) : (
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() => steer("pause", () => api.pauseCallBatch(batch.id))}
                >
                  Pause
                </Button>
              )}
              {isCancelable(batch.status) && (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmCancel(true)}>
                  Cancel batch
                </Button>
              )}
            </>
          )}
        </>
      }
    >
      <div className="grid gap-5">
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant={BATCH_STATUS_VARIANT[batch.status]} dot>
            {BATCH_STATUS_LABEL[batch.status]}
          </Badge>
          {batch.next_dial_at && (
            <span className="text-[13px] leading-5 text-muted">
              Waiting until {inZone(batch.next_dial_at, batch.timezone)} {zone}
              {batch.next_dial_reason === "daily_cap" && " — daily limit reached"}
            </span>
          )}
          {batch.dial_daily_cap_today !== null && (
            <span className="text-[13px] leading-5 tabular-nums text-muted">
              {usedToday(batch.dialed_today, batch.dial_daily_cap_today, batch.dial_daily_cap)}
            </span>
          )}
          {batch.calling_window && (
            <span className="text-[13px] leading-5 text-muted">
              Calls {hhmm(batch.calling_window.start)}–{hhmm(batch.calling_window.end)} {zone}
            </span>
          )}
          <span className="ml-auto text-[13px] leading-5 text-muted">
            {batch.max_concurrency} at a time
            {batch.dial_gap_seconds > 0 &&
              ` · ${
                batch.dial_gap_seconds % 60
                  ? `${batch.dial_gap_seconds}s`
                  : `${batch.dial_gap_seconds / 60} min`
              } apart`}
            {batch.max_attempts > 1 && ` · up to ${batch.max_attempts} attempts each`}
          </span>
        </div>

        {batch.status === "failed" && batch.failure_reason && (
          <div className="rounded-xl border border-danger/25 bg-danger/[0.05] px-4 py-3 text-[13px] leading-5 text-danger">
            <strong className="font-semibold">This batch stopped itself.</strong>{" "}
            {asSentence(batch.failure_reason)} Ten calls in a row failed to get off the ground,
            so it stopped rather than working through the rest of the list.
          </div>
        )}

        {/* A running batch that has dialled nothing this pass says why here.
            Being out of credits is the case this exists for: the batch is not
            failed and nothing is wrong with it, so the status badge alone would
            show a campaign that has quietly stopped placing calls with no
            explanation anywhere on the page. The reason clears itself on the
            next successful dial. */}
        {batch.status !== "failed" && batch.failure_reason && (
          <div className="rounded-xl border border-warn/25 bg-warn/[0.06] px-4 py-3 text-[13px] leading-5 text-warn">
            <strong className="font-semibold">Paused on its own.</strong>{" "}
            {asSentence(batch.failure_reason)} It picks up again by itself once that is sorted —
            there is no button to press.
          </div>
        )}

        <BatchProgress batch={batch} />

        <div>
          <div className="mb-2.5 flex flex-wrap items-center gap-2">
            <Segment value={tab} onChange={setTab} options={TABS} />
            {isSteerable(batch.status) && (
              <Button variant="secondary" size="sm" className="ml-auto" onClick={() => setAdding(true)}>
                <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
                  <path d="M12 5v14M5 12h14" />
                </svg>
                Add recipients
              </Button>
            )}
          </div>
          {err && (
            <p className="mb-2.5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[13px] leading-5 text-danger">
              {err}
            </p>
          )}
          {loading && recipients.length === 0 ? (
            <ListSkeleton rows={5} />
          ) : recipients.length === 0 ? (
            <p className="rounded-xl border border-line-2 bg-white px-4 py-6 text-center text-[13px] text-muted">
              Nobody in this batch is {RECIPIENT_STATUS_LABEL[tab as CallBatchRecipientStatus]?.toLowerCase() ?? "here"} right now.
            </p>
          ) : (
            <RecipientTable rows={recipients} />
          )}
          {(page > 0 || hasMore) && (
            <div className="mt-2.5 flex items-center justify-end gap-1.5 text-[13px] text-muted">
              <Button
                variant="secondary"
                size="sm"
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
              >
                Previous
              </Button>
              <span className="px-1 tabular-nums">{page + 1}</span>
              <Button
                variant="secondary"
                size="sm"
                disabled={!hasMore}
                onClick={() => setPage(page + 1)}
              >
                Next
              </Button>
            </div>
          )}
        </div>
      </div>

      {editing && (
        <EditPolicy
          batch={batch}
          agents={agents}
          numbers={numbers}
          onSaved={(next) => {
            onChanged(next);
            setEditing(false);
            toast({ msg: "Saved. The next call uses it.", kind: "ok" });
          }}
          onClose={() => setEditing(false)}
        />
      )}

      {adding && (
        <AddPeople
          batch={batch}
          onAdded={() => {
            api.getCallBatch(batch.id).then(onChanged).catch(() => {});
            load();
          }}
          onClose={() => setAdding(false)}
        />
      )}

      {confirmDelete && (
        <DeleteBatchModal
          run={() => api.deleteCallBatch(batch.id)}
          onDeleted={onDeleted}
          onClose={() => setConfirmDelete(false)}
        >
          <p>
            The batch and its list of {batch.counts.total.toLocaleString()}{" "}
            {batch.counts.total === 1 ? "person" : "people"} are deleted for good.
          </p>
          <p>The calls it made stay under Calls, with their recordings and costs.</p>
        </DeleteBatchModal>
      )}

      {confirmCancel && (
        <Modal
          title="Cancel this batch?"
          onClose={() => !busy && setConfirmCancel(false)}
          width="max-w-[480px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmCancel(false)} disabled={busy}>
                Keep it running
              </Button>
              <Button
                variant="danger"
                disabled={busy}
                onClick={() => steer("cancel", () => api.cancelCallBatch(batch.id))}
              >
                Cancel the batch
              </Button>
            </>
          }
        >
          <div className="grid gap-2.5 text-[14px] leading-6 text-ink-soft">
            <p>
              The {batch.counts.pending.toLocaleString()} people not yet called will not be
              called. There is no undo.
            </p>
            {batch.counts.dialing > 0 && (
              <p>
                {batch.counts.dialing} {batch.counts.dialing === 1 ? "call is" : "calls are"} on
                the line right now. {batch.counts.dialing === 1 ? "It" : "They"} will finish
                normally rather than being cut off mid-sentence.
              </p>
            )}
          </div>
        </Modal>
      )}
    </Modal>
  );
}

function RecipientTable({ rows }: { rows: CallBatchRecipientResponse[] }) {
  const keys = useMemo(() => {
    const out: string[] = [];
    for (const row of rows) {
      for (const key of Object.keys(row.userdata)) if (!out.includes(key)) out.push(key);
    }
    return out.slice(0, 4);
  }, [rows]);

  return (
    <div className="overflow-x-auto rounded-xl border border-line-2 bg-white">
      <table className="w-full min-w-[620px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line text-left text-[12px] font-medium text-muted">
            <th className="w-12 px-3 py-2 font-medium">#</th>
            <th className="min-w-[150px] px-3 py-2 font-medium">Number</th>
            {keys.map((key) => (
              <th key={key} className="min-w-[110px] px-3 py-2 font-mono font-medium text-ink-soft">
                {key}
              </th>
            ))}
            <th className="w-[110px] px-3 py-2 font-medium">Status</th>
            <th className="min-w-[170px] px-3 py-2 font-medium">Outcome</th>
            <th className="w-[70px] px-3 py-2 font-medium" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="border-b border-line last:border-0">
              <td className="px-3 py-2 font-mono text-[12px] text-faint tabular-nums">
                {row.row_number}
              </td>
              <td className="px-3 py-2 font-mono text-[12.5px] text-ink">{row.to}</td>
              {keys.map((key) => (
                <td
                  key={key}
                  className="max-w-[180px] truncate px-3 py-2 text-ink-soft"
                  title={row.userdata[key] ?? ""}
                >
                  {row.userdata[key] ?? <span className="text-placeholder">—</span>}
                </td>
              ))}
              <td className="px-3 py-2">
                <Badge variant={RECIPIENT_STATUS_VARIANT[row.status]}>
                  {RECIPIENT_STATUS_LABEL[row.status]}
                </Badge>
              </td>
              <td className="px-3 py-2 text-ink-soft">
                <span
                  className={cn(row.status === "failed" && "text-danger")}
                  title={row.last_close_reason ?? ""}
                >
                  {row.last_close_reason_label ?? <span className="text-placeholder">—</span>}
                </span>
                {row.attempts > 1 && (
                  <span className="ml-1.5 text-[12px] text-faint">· {row.attempts} tries</span>
                )}
              </td>
              <td className="px-3 py-2 text-right">
                {row.session_id ? (
                  <Link
                    href={`/calls?id=${row.session_id}`}
                    className="text-[12.5px] font-medium text-ink underline-offset-2 hover:underline"
                  >
                    Call →
                  </Link>
                ) : (
                  <span className="text-[12.5px] text-placeholder">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Append people to the batch: the same list editor as create. */
function AddPeople({
  batch,
  onAdded,
  onClose,
}: {
  batch: CallBatchResponse;
  onAdded: () => void;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<RecipientRow[]>([]);
  return (
    <AddRecipientsModal
      batchName={batch.name}
      count={rows.length}
      blocked={rows.some((r) => r.problems.length > 0)}
      note={{
        text:
          batch.status === "paused"
            ? "Called once you resume."
            : "Called at this batch's pace and hours.",
      }}
      submit={() =>
        api.addCallBatchRecipients(batch.id, {
          recipients: rows.map((r) => ({ to: r.to, userdata: r.userdata })),
        })
      }
      onRejected={(errors) => {
        const placed = applyServerErrors(rows, errors);
        setRows(placed.rows);
        return placed.unattached;
      }}
      onAdded={onAdded}
      onClose={onClose}
    >
      <RecipientEditor rows={rows} onChange={setRows} />
    </AddRecipientsModal>
  );
}

/** The same policy form as create, minus the list — because it is the same policy. */
function EditPolicy({
  batch,
  agents,
  numbers,
  onSaved,
  onClose,
}: {
  batch: CallBatchResponse;
  agents: AgentResponse[];
  numbers: PhoneNumberResponse[];
  onSaved: (batch: CallBatchResponse) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<PolicyDraft>(() => draftFromBatch(batch));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const startEditable = batch.status === "scheduled";
  // The variables the agent currently picked declares. The bag itself cannot be
  // edited here — `PATCH` has no `vars` — so this is what turns "you have
  // re-pointed this campaign at an agent it can no longer run" into a sentence
  // before the click rather than a 400 after it.
  const { declared, error: varsError } = usePublishedVars(draft.agentId || null, agents);
  const problems = varsError ? [varsError] : policyProblems(draft, declared);

  async function save() {
    setSaving(true);
    setErr("");
    try {
      onSaved(await api.patchCallBatch(batch.id, toPatchRequest(draft, startEditable)));
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Edit this batch"
      sub="Everything here is policy: the next call picks it up, and nobody already called is called again."
      onClose={() => !saving && onClose()}
      width="max-w-[680px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving || problems.length > 0}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <PolicyFields
        draft={draft}
        onChange={setDraft}
        agents={agents}
        numbers={numbers}
        declared={declared}
        startEditable={startEditable}
        varsEditable={false}
        savedCap={{ cap: batch.dial_daily_cap, today: batch.dial_daily_cap_today }}
      />
      {(err || problems.length > 0) && (
        <p className="mt-4 rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
          {err || problems[0]}
        </p>
      )}
    </Modal>
  );
}
