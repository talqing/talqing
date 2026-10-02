"use client";

import { useMemo, useState } from "react";
import type {
  AgentResponse,
  CallBatchResponse,
  PhoneNumberResponse,
} from "@talqing/sdk";
import { Button, Modal, UnsavedChangesModal } from "@/app/components/ui";
import { usePublishedVars } from "@/app/components/SessionVars";
import { api } from "@/lib/api";
import { apiErrorMessage, apiErrorList } from "@/lib/apiError";
import {
  PolicyFields,
  type PolicyDraft,
  emptyDraft,
  policyProblems,
  toCreateRequest,
} from "./PolicyFields";
import { RecipientEditor } from "./RecipientEditor";
import { applyServerErrors, type RecipientRow } from "./recipients";
import { zoneAbbreviation } from "./shared";

/**
 * Creating a batch, over the list rather than instead of it.
 *
 * A dialog and not a page: nothing here needs a URL of its own, and coming back
 * to the batches you already have should not be a navigation.
 */
export function CreateBatch({
  agents,
  numbers,
  onCreated,
  onCancel,
}: {
  agents: AgentResponse[];
  numbers: PhoneNumberResponse[];
  onCreated: (batch: CallBatchResponse) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<PolicyDraft>(emptyDraft);
  const [rows, setRows] = useState<RecipientRow[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  // The PUBLISHED version's variables, which is what every call this batch
  // places will run — `agents[].config` is the draft.
  const { declared, error: varsError } = usePublishedVars(draft.agentId || null, agents);

  const problems = useMemo(() => {
    // First: until this is read there is no telling what the campaign owes.
    const list = varsError ? [varsError] : policyProblems(draft, declared);
    if (!rows.length) list.push("Add at least one person to call.");
    else if (rows.some((r) => r.problems.length)) list.push("Fix the rows flagged above.");
    return list;
  }, [draft, rows, declared, varsError]);

  const agent = agents.find((a) => a.id === draft.agentId);
  const number = numbers.find((n) => n.id === draft.fromPhoneNumberId);
  // A click on the backdrop is one pixel away from a click inside it, and this
  // form can hold ten thousand rows somebody spent an afternoon on.
  const started = rows.length > 0 || !!draft.name.trim() || !!draft.agentId || !!draft.fromPhoneNumberId;

  async function submit() {
    setSaving(true);
    setErr("");
    try {
      const batch = await api.createCallBatch(
        toCreateRequest(
          draft,
          rows.map((r) => ({ to: r.to, userdata: r.userdata })),
          declared,
        ),
      );
      onCreated(batch);
    } catch (error: unknown) {
      // The API answers a bad list with one 400 naming every bad row. Put each
      // of those back on the row it names — an operator with 10 000 rows on
      // screen should not have to search for row 4 812 by hand.
      const reported = apiErrorList(error);
      const { rows: flagged, unattached } = applyServerErrors(rows, reported);
      setRows(flagged);
      // Say nothing down here once every problem is showing on its own row: the
      // envelope's "invalid call batch" adds no information and reads as a second,
      // vaguer failure beside the specific ones.
      const attached = reported.length - unattached.length;
      setErr(unattached.length ? unattached.join(" ") : attached ? "" : apiErrorMessage(error));
      setConfirming(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="New batch"
      sub="Pick who to call and what the agent should know about each of them, then say when it may dial."
      onClose={() => (started ? setDiscarding(true) : onCancel())}
      width="max-w-[920px]"
    >
      <section>
        <Step
          n={1}
          title="Who to call"
          sub="Type them in, upload a file, or both — nothing is dialled until you say so."
        />
        <RecipientEditor rows={rows} onChange={setRows} />
      </section>

      <section className="mt-7 border-t border-line pt-6">
        <Step n={2} title="How it runs" sub="Which agent dials, from which number, and when." />
        <PolicyFields
          draft={draft}
          onChange={setDraft}
          agents={agents}
          numbers={numbers}
          declared={declared}
        />
      </section>

      {err && (
        <p className="mt-4 rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
          {err}
        </p>
      )}

      {/* The action bar rides along instead of sitting at the end of a form this
          tall — the answer to "can I start yet" should never be a scroll away. */}
      <div className="sticky bottom-0 -mx-6 mt-6 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line bg-surface px-6 py-3.5">
        <p className="min-w-0 flex-1 text-[13px] leading-5 text-muted">
          {problems.length ? (
            problems[0]
          ) : (
            <>
              <span className="font-semibold text-ink tabular-nums">
                {rows.length.toLocaleString()} {rows.length === 1 ? "call" : "calls"}
              </span>{" "}
              from <span className="font-mono text-[12.5px]">{number?.e164}</span> by{" "}
              {agent?.config?.name}
              {draft.startMode === "now"
                ? ", starting right away."
                : `, starting ${new Date(`${draft.startDate}T${draft.startTime}`).toLocaleString(undefined, {
                    day: "numeric",
                    month: "short",
                    hour: "2-digit",
                    minute: "2-digit",
                  })} ${zoneAbbreviation(null, draft.timezone)}.`}
            </>
          )}
        </p>
        <Button variant="secondary" onClick={() => (started ? setDiscarding(true) : onCancel())}>
          Cancel
        </Button>
        <Button onClick={() => setConfirming(true)} disabled={problems.length > 0}>
          Review and start
        </Button>
      </div>

      {discarding && (
        <UnsavedChangesModal
          sub="Nobody has been called, and this list and its settings are not saved anywhere."
          onKeepEditing={() => setDiscarding(false)}
          onDiscard={onCancel}
        />
      )}

      {confirming && (
        <Modal
          title="Start calling?"
          onClose={() => !saving && setConfirming(false)}
          width="max-w-[520px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirming(false)} disabled={saving}>
                Back
              </Button>
              <Button onClick={submit} disabled={saving}>
                {saving ? "Starting…" : "Start the batch"}
              </Button>
            </>
          }
        >
          <div className="grid gap-3 text-[14px] leading-6 text-ink-soft">
            <p>
              <strong className="font-semibold text-ink">
                {rows.length.toLocaleString()} real phone {rows.length === 1 ? "call" : "calls"}
              </strong>{" "}
              from <span className="font-mono text-[13px]">{number?.e164}</span>, made by{" "}
              <strong className="font-semibold text-ink">
                {agent?.config?.name ?? "your agent"}
              </strong>
              . Every one of them is billed.
            </p>
            <p>
              {draft.startMode === "now"
                ? "Dialling starts within the minute."
                : `Dialling starts at ${draft.startTime} on ${draft.startDate}, ${draft.timezone.replace(/_/g, " ")} time.`}
              {draft.windowOn &&
                ` It only calls between ${draft.windowStart} and ${draft.windowEnd}, and waits outside those hours.`}
            </p>
            <p>
              Up to {draft.maxConcurrency} {draft.maxConcurrency === 1 ? "call" : "calls"} at a
              time. You can pause or cancel it at any point.
            </p>
          </div>
        </Modal>
      )}
    </Modal>
  );
}

function Step({ n, title, sub }: { n: number; title: string; sub: string }) {
  return (
    <div className="mb-3.5 flex items-start gap-3">
      <span
        aria-hidden
        className="grid h-6 w-6 flex-none place-items-center rounded-full border border-line-2 bg-white font-mono text-[11.5px] font-medium text-ink-soft"
      >
        {n}
      </span>
      <div className="min-w-0">
        <h2 className="text-[14.5px] font-semibold leading-6 text-ink">{title}</h2>
        <p className="text-[13px] leading-5 text-muted">{sub}</p>
      </div>
    </div>
  );
}
