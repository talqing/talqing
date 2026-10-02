"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import type { CallType, ChatDetailResponse, ConversationSessionResponse } from "@talqing/sdk";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { Badge, Button, Skeleton } from "../components/ui";
import { fmtCost, fmtDuration, SnapshotBand } from "../components/SnapshotBand";
import type { TurnLatencyColumn } from "../components/TurnLatencyChart";
import type { IssueTargets } from "../components/timeline/exchanges";
import { CallAnalysisPanel, OutcomeBadge } from "../calls/CallAnalysis";
import { RawEvents, TYPE_LABEL, UserdataDisclosure } from "../calls/CallDetail";
import { CostBreakdown } from "../calls/CostBreakdown";

/* A conversation is a thread of sessions — a chat, the call that continued it,
   the chat after that. These are the lines between them: what each one was,
   how it ended, and for a chat, everything a call's own page would say. */

type Session = ConversationSessionResponse;

export const isOpen = (session: Session) =>
  session.status === "queued" || session.status === "running";

/* The time alone for today, the day as well for anything older. */
function when(value: string): string {
  const at = new Date(value);
  const today = at.toDateString() === new Date().toDateString();
  return at.toLocaleString(undefined, {
    ...(today ? {} : { month: "short", day: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  });
}

function version(session: Session): string {
  if (session.agent_version === "draft") return " · draft";
  return session.agent_version != null ? ` · v${session.agent_version}` : "";
}

function AgentRan({ session }: { session: Session }) {
  const name = `${session.agent.name ?? "Unknown agent"}${version(session)}`;
  return session.agent.agent_id ? (
    <Link
      href={`/agents/detail?id=${session.agent.agent_id}`}
      className="underline decoration-line-strong underline-offset-2 hover:text-ink"
    >
      {name}
    </Link>
  ) : (
    <span>{name}</span>
  );
}

/** Where a chat begins in the thread. An open one says so here, and is ended
 *  from here. */
export function ChatStart({
  session,
  canEnd,
  ending,
  onEnd,
}: {
  session: Session;
  canEnd: boolean;
  ending: boolean;
  onEnd: () => void;
}) {
  const open = isOpen(session);
  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
      <span className="text-[13px] font-semibold text-ink">Chat</span>
      <span className="text-[12.5px] text-muted">
        started {when(session.started_at ?? session.created_at)}
        {" · "}
        <AgentRan session={session} />
      </span>
      <span className="h-px min-w-6 flex-1 bg-line-strong" aria-hidden />
      {open && (
        <Badge variant="live" dot>
          Open
        </Badge>
      )}
      {open && canEnd && (
        <Button variant="secondary" size="sm" disabled={ending} onClick={onEnd}>
          {ending ? "Ending…" : "End chat"}
        </Button>
      )}
    </div>
  );
}

/** Where a chat ended, what came of it, and — on request — everything else. */
export function ChatEnd({
  session,
  columns,
  issueTarget,
  onJump,
}: {
  session: Session;
  /* This chat's turns, for the latency chart in its details. */
  columns: TurnLatencyColumn[];
  issueTarget: IssueTargets;
  onJump: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
        <span className="text-[12.5px] text-muted">
          Ended{session.ended_at ? ` ${when(session.ended_at)}` : ""}
          {session.close_reason_label ? ` · ${session.close_reason_label}` : ""}
        </span>
        {session.analysis.outcome && <OutcomeBadge outcome={session.analysis.outcome} />}
        {session.cost && (
          <span className="font-mono text-[12px] tabular-nums text-muted">
            {fmtCost(session.cost.total_charge)}
          </span>
        )}
        <span className="h-px min-w-6 flex-1 bg-line-strong" aria-hidden />
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
          className="text-[12.5px] font-medium text-ink-soft underline decoration-line-strong underline-offset-2 hover:text-ink"
        >
          {open ? "Hide details" : "Details"}
        </button>
      </div>
      {session.error?.message && (
        <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger">
          <strong className="font-medium">The chat failed:</strong> {session.error.message}
        </p>
      )}
      {open && (
        <ChatDetails session={session} columns={columns} issueTarget={issueTarget} onJump={onJump} />
      )}
    </div>
  );
}

/* What a call's page says under its transcript, for a chat. Fetched when it is
   asked for: a thread of twenty chats should not load twenty ledgers. */
function ChatDetails({
  session,
  columns,
  issueTarget,
  onJump,
}: {
  session: Session;
  columns: TurnLatencyColumn[];
  issueTarget: IssueTargets;
  onJump: (id: string) => void;
}) {
  const [detail, setDetail] = useState<ChatDetailResponse | null>(null);
  const [error, setError] = useState("");
  const [reruns, setReruns] = useState(0);
  /* Again when the session row moves: the analysis and the final bill land a
     few seconds after the chat ends. */
  useEffect(() => {
    let stale = false;
    api
      .getChatDetail(session.id)
      .then((loaded) => !stale && setDetail(loaded))
      .catch((e) => !stale && setError(apiErrorMessage(e, "Could not load this chat's details")));
    return () => {
      stale = true;
    };
  }, [session.id, session.updated_at, reruns]);

  if (error) {
    return (
      <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger">
        {error}
      </p>
    );
  }
  if (!detail) {
    return (
      <div className="grid gap-3" aria-busy="true" aria-label="Loading chat details">
        <Skeleton className="h-24 w-full rounded-xl" />
        <Skeleton className="h-16 w-full rounded-xl" />
      </div>
    );
  }
  return (
    <div className="grid gap-3">
      <CallAnalysisPanel
        analysis={detail.chat.analysis ?? session.analysis}
        sessionId={session.id}
        agentId={session.agent.agent_id ?? null}
        inProgress={false}
        contentDeletedAt={null}
        onRerun={() => setReruns((n) => n + 1)}
        noun="chat"
      />
      <SnapshotBand
        snapshot={detail.snapshot}
        columns={columns}
        durationLabel="Chat length"
        showMetering={false}
        onJumpToTurn={onJump}
        issueTarget={issueTarget}
      />
      <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
        {detail.cost && <CostBreakdown cost={detail.cost} defaultOpen />}
        <UserdataDisclosure
          summary="Userdata"
          note="What this chat ended holding — the variable bus its tools read and wrote."
          bag={detail.chat.userdata}
        />
        <UserdataDisclosure
          summary="Variables"
          note="What the request that started this chat supplied for {{vars.…}} — read-only for its whole length."
          bag={detail.vars}
        />
        <RawEvents events={detail.events} noun="chat" />
      </div>
    </div>
  );
}

/** A call inside the thread: what it was and what came of it, with its turns
 *  one click away and its own page two. */
export function CallDivider({
  session,
  expanded,
  onToggle,
}: {
  session: Session;
  expanded: boolean;
  onToggle: () => void;
}) {
  const label = TYPE_LABEL[session.type as CallType] ?? session.type;
  return (
    <div className="grid gap-2 rounded-xl border border-line-2 bg-subtle px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
        <span className="text-[13px] font-semibold text-ink">{label} call</span>
        <span className="text-[12.5px] text-muted">
          {when(session.started_at ?? session.created_at)}
          {session.duration_s != null ? ` · ${fmtDuration(session.duration_s)}` : ""}
          {" · "}
          <AgentRan session={session} />
        </span>
        {isOpen(session) && (
          <Badge variant="live" dot>
            In progress
          </Badge>
        )}
        {session.analysis.outcome && <OutcomeBadge outcome={session.analysis.outcome} />}
        {session.cost && (
          <span className="font-mono text-[12px] tabular-nums text-muted">
            {fmtCost(session.cost.total_charge)}
          </span>
        )}
        <span className="ml-auto flex items-center gap-3 text-[12.5px] font-medium text-ink-soft">
          <button
            type="button"
            aria-expanded={expanded}
            onClick={onToggle}
            className="underline decoration-line-strong underline-offset-2 hover:text-ink"
          >
            {expanded ? "Hide turns" : "Show turns"}
          </button>
          <Link
            href={`/calls?id=${session.id}`}
            className="underline decoration-line-strong underline-offset-2 hover:text-ink"
          >
            Open call →
          </Link>
        </span>
      </div>
      {session.analysis.summary && (
        <p className="max-w-[80ch] text-[13px] leading-[1.55] text-ink-soft">
          {session.analysis.summary}
        </p>
      )}
      {session.error?.message && (
        <p className="text-[12.5px] leading-5 text-danger">{session.error.message}</p>
      )}
    </div>
  );
}
