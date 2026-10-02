"use client";
import { useState } from "react";
import type { AnalysisResponse, CallOutcome } from "@talqing/sdk";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { Badge, Button, Modal, Panel, Tooltip } from "../components/ui";

export const OUTCOME_VARIANT: Record<CallOutcome, "live" | "danger" | "default"> = {
  success: "live",
  failure: "danger",
  unknown: "default",
};

export const OUTCOME_LABEL: Record<CallOutcome, string> = {
  success: "Success",
  failure: "Failure",
  unknown: "Unclear",
};

export function OutcomeBadge({ outcome }: { outcome: CallOutcome }) {
  return (
    <Badge variant={OUTCOME_VARIANT[outcome]} dot>
      {OUTCOME_LABEL[outcome]}
    </Badge>
  );
}

/* Why a call has no analysis. An empty panel that does not explain itself reads
   as a broken feature, and two of these three are configuration the reader can
   act on — so each says which.

   The two re-run cases point at the ↻ control in this panel's corner. They used
   to point at a control "in the calls list" that has never existed anywhere in
   the app.

   `inProgress` is what keeps the first two honest. The session row claims
   `analysis_status = 'pending'` the moment the call starts (see
   `workers/session/sessions.py::create_session`), so `pending` means two
   different things depending on whether the call is over — "it runs when the
   call ends" or "nobody ever ran it". Before that claim existed, a live call
   sat at the default `none` and this panel told its owner analysis was
   switched off while it was, in fact, switched on. */
function whyNothing(
  analysis: AnalysisResponse,
  inProgress: boolean,
  contentDeletedAt: string | null,
  noun: "call" | "chat",
): string | null {
  if (contentDeletedAt) {
    // The analysis ran and its output was erased with the rest of the call's
    // content. Without this the panel renders its own header over nothing,
    // which reads as "the analysis found nothing to say".
    return `Deleted on ${new Date(contentDeletedAt).toLocaleDateString()} under your organization's data retention policy.`;
  }
  if (analysis.status === "completed") return null;
  if (analysis.status === "none") {
    return "Analysis is off for this agent. Turn it on under Analysis in the agent editor.";
  }
  if (analysis.status === "pending") {
    return inProgress
      ? `Analysis runs once this ${noun} ends.`
      : `Analysis has not finished. If this ${noun} ended a while ago, the worker did not get to it — run it now with the ↻ button above.`;
  }
  if (analysis.status === "failed") {
    return `Analysis did not complete. The ${noun} itself was unaffected; run it again with the ↻ button above.`;
  }
  switch (analysis.skip_reason) {
    case "too_short":
      return "Skipped: nobody said enough for there to be anything to analyse. Nothing was charged.";
    case "call_failed":
      return `Skipped: the ${noun} failed before a conversation happened. Nothing was charged.`;
    case "produces_nothing":
      return "Skipped: analysis is on, but no summary, success definition or fields are set.";
    default:
      return `Skipped for this ${noun}.`;
  }
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const ReloadIcon = (
  <svg
    width="15"
    height="15"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </svg>
);

/**
 * Re-run this call's analysis, or try the agent's current draft on it.
 *
 * Both endpoints have existed since the analysis engine shipped and neither was
 * reachable from anywhere in the app. They belong on this panel because this is
 * where somebody stands when they realise their success definition is wrong.
 *
 * One corner icon rather than two labelled buttons: they are a rare, deliberate
 * action, and a pair of them across the top of the panel drew the eye away from
 * the summary — which is what the panel is FOR. The dialog is where the choice
 * and the cost get explained, so the trigger does not have to.
 */
function AnalysisControls({
  sessionId,
  agentId,
  noun,
  onRerun,
  onPreview,
}: {
  sessionId: string;
  agentId: string | null;
  noun: "call" | "chat";
  onRerun: () => void;
  onPreview: (analysis: AnalysisResponse, model: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<"rerun" | "preview" | null>(null);
  const [error, setError] = useState("");

  async function rerun() {
    setBusy("rerun");
    setError("");
    try {
      if (noun === "chat") await api.rerunChatAnalysis(sessionId);
      else await api.backfillCallAnalysis({ call_ids: [sessionId] });
      setOpen(false);
      onRerun();
    } catch (e) {
      setError(apiErrorMessage(e, "Could not re-run the analysis."));
    } finally {
      setBusy(null);
    }
  }

  async function preview() {
    if (!agentId) return;
    setBusy("preview");
    setError("");
    try {
      // The draft as it stands in the agent editor right now — which is the
      // whole question being asked ("would my new definition have caught this?").
      const agent = await api.getAgent(agentId);
      const draft = agent.config.analysis;
      if (!draft) {
        setError("This agent's draft has no analysis definition to try.");
        return;
      }
      const body = { analysis: draft, agent_id: agentId };
      const result =
        noun === "chat"
          ? await api.previewChatAnalysis(sessionId, body)
          : await api.previewCallAnalysis(sessionId, body);
      setOpen(false);
      onPreview(result.analysis, result.model ?? null);
    } catch (e) {
      setError(apiErrorMessage(e, "Could not preview the analysis."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <Tooltip label="Run the analysis again">
        <button
          type="button"
          aria-label="Run the analysis again"
          onClick={() => {
            setError("");
            setOpen(true);
          }}
          className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-line-2 bg-white text-muted transition-colors hover:border-line-strong hover:text-ink"
        >
          {ReloadIcon}
        </button>
      </Tooltip>

      {open && (
        <Modal
          title="Re-run analysis"
          sub={`Reads the finished transcript again and replaces this ${noun}'s summary, outcome and extracted fields.`}
          width="max-w-[560px]"
          onClose={() => !busy && setOpen(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setOpen(false)} disabled={busy !== null}>
                Cancel
              </Button>
              <Button variant="primary" onClick={() => void rerun()} disabled={busy !== null}>
                {busy === "rerun" ? "Re-running…" : "Re-run"}
              </Button>
            </>
          }
        >
          <div className="grid gap-3 pb-4">
            <p className="text-[13px] leading-6 text-muted">
              This uses the agent&rsquo;s <strong className="font-medium text-ink">saved</strong>{" "}
              analysis definition. It is a real model call, so the workspace is charged for it and
              the {noun} is re-priced.
            </p>
            {/* The other half of the reason somebody opened this dialog: they
                have been editing the definition and want to know whether the new
                one would have judged this call differently — before saving it. */}
            {agentId && (
              <div className="grid gap-2 rounded-lg border border-line bg-subtle px-3 py-2.5">
                <p className="text-[12.5px] leading-5 text-muted">
                  Still tuning the definition? Try the agent&rsquo;s current draft against this {noun}
                  instead. It costs the same one model call and writes nothing.
                </p>
                <Button
                  variant="secondary"
                  size="sm"
                  className="justify-self-start"
                  onClick={() => void preview()}
                  disabled={busy !== null}
                >
                  {busy === "preview" ? "Trying…" : "Try current draft"}
                </Button>
              </div>
            )}
            {error && <p className="text-[12.5px] leading-5 text-danger">{error}</p>}
          </div>
        </Modal>
      )}
    </>
  );
}

export function CallAnalysisPanel({
  analysis,
  sessionId,
  agentId,
  inProgress,
  contentDeletedAt,
  onRerun,
  noun = "call",
}: {
  analysis: AnalysisResponse;
  sessionId: string;
  agentId: string | null;
  /* A chat is analysed through its own routes, and is not called a call. */
  noun?: "call" | "chat";
  /* A running call has no finished transcript to analyse, which changes both
     what `pending` means and whether re-running is a coherent request. */
  inProgress: boolean;
  /* A purged call has no transcript to analyse either, and never will. */
  contentDeletedAt: string | null;
  onRerun: () => void;
}) {
  /* A preview is deliberately transient — nothing was written, so it must not
     survive a reload or be mistaken for the call's stored verdict. */
  const [preview, setPreview] = useState<{ analysis: AnalysisResponse; model: string | null } | null>(
    null,
  );
  const shown = preview?.analysis ?? analysis;
  const reason = whyNothing(shown, inProgress, contentDeletedAt, noun);
  const fields = Object.entries(shown.fields ?? {});

  return (
    <Panel className="grid gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="font-display text-[15px] font-semibold tracking-tight text-ink">
          {noun === "call" ? "Call analysis" : "Chat analysis"}
        </div>
        {shown.outcome && <OutcomeBadge outcome={shown.outcome} />}
        {/* Nothing to re-run against yet: both endpoints read the stored
            transcript, so on a live call they would judge half a conversation,
            overwrite the summary with it, and re-price a call that is still
            running up a bill. */}
        {!inProgress && !contentDeletedAt && (
          <div className="ml-auto">
            <AnalysisControls
              sessionId={sessionId}
              agentId={agentId}
              noun={noun}
              onRerun={() => {
                setPreview(null);
                onRerun();
              }}
              onPreview={(result, model) => setPreview({ analysis: result, model })}
            />
          </div>
        )}
      </div>

      {preview && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-info/25 bg-info/[0.05] px-3 py-2 text-[12.5px] leading-5 text-info">
          <span>
            Showing a preview from the agent&rsquo;s current draft
            {preview.model ? ` (${preview.model})` : ""}. Nothing was saved.
          </span>
          <button
            type="button"
            onClick={() => setPreview(null)}
            className="font-medium underline underline-offset-2"
          >
            Show the stored analysis
          </button>
        </div>
      )}

      {reason ? (
        <p className="text-[13px] leading-5 text-muted">{reason}</p>
      ) : (
        <>
          {shown.summary && (
            <p className="text-[13.5px] leading-6 text-ink-soft">{shown.summary}</p>
          )}

          {shown.outcome_rationale && (
            <p className="rounded-lg border border-line bg-subtle px-3 py-2 text-[12.5px] leading-5 text-muted">
              <strong className="font-medium text-ink">Why {OUTCOME_LABEL[shown.outcome ?? "unknown"].toLowerCase()}:</strong>{" "}
              {shown.outcome_rationale}
            </p>
          )}

          {fields.length > 0 && (
            <div className="grid gap-1.5 border-t border-line pt-3">
              <div className="text-[12px] font-medium uppercase tracking-wide text-faint">
                Extracted
              </div>
              <dl className="grid gap-x-4 gap-y-1.5 sm:grid-cols-[max-content_1fr]">
                {fields.map(([name, value]) => (
                  <div key={name} className="contents">
                    <dt className="font-mono text-[12px] leading-5 text-muted">{name}</dt>
                    <dd
                      className={
                        value === null || value === undefined
                          ? "text-[13px] leading-5 text-faint"
                          : "text-[13px] leading-5 text-ink"
                      }
                    >
                      {formatValue(value)}
                    </dd>
                  </div>
                ))}
              </dl>
              {/* Said once, here, because it is the question every reader has
                  when this sits near the call's userdata panel. */}
              <p className="mt-1 text-[12px] leading-relaxed text-muted">
                Worked out from the transcript after the {noun} — separate from what the agent&rsquo;s
                tools recorded while it was happening.
              </p>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
