"use client";
import { useCallback, useMemo, useState } from "react";
import type {
  ConversationItemResponse,
  ConversationSessionResponse,
  ConversationTraceEventResponse,
  HealthSnapshotResponse,
} from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { Button } from "../components/ui";
import { SnapshotBand } from "../components/SnapshotBand";
import type { TurnLatencyColumn } from "../components/TurnLatencyChart";
import { StreamingBeat, TimelineBlocks } from "../components/timeline/ExchangeBlock";
import {
  buildExchanges,
  exchangeMatches,
  issueTargets,
  plainText,
  slowestExchange,
  type Exchange,
  type ExchangeFilter,
  type Timeline,
} from "../components/timeline/exchanges";
import { TimelineToolbar } from "../components/timeline/Toolbar";
import { useJump } from "../components/timeline/useJump";
import { CallDivider, ChatEnd, ChatStart, isOpen } from "./SessionDividers";

/* One conversation, as the thread of sessions it is. What a transcript IS lives
   in `components/timeline/`; this is the frame a thread puts around it — the
   line where one session ends and the next begins, and the search across them. */

type Session = ConversationSessionResponse;

/* One session's items, or a run of items that belong to none (a message nobody
   answered, outreach). Sessions that overlapped — a call placed while a chat was
   open — are still drawn one after the other, in the order they began: each is
   one story, and interleaving them line by line tells neither. */
interface Group {
  key: string;
  session: Session | null;
  timeline: Timeline;
  multiAgent: boolean;
}

/* LiveKit's default `false_interruption_timeout`. A call's own page reads the
   agent's setting; a thread can hold calls from several agents. */
const FALSE_INTERRUPTION_GRACE_S = 2;

function columnsOf(exchanges: Exchange[]): TurnLatencyColumn[] {
  return exchanges.flatMap((exchange) =>
    exchange.response && exchange.turn != null
      ? [
          {
            id: exchange.id,
            index: exchange.turn,
            totalMs: exchange.response.total_ms,
            stages: exchange.response.stages,
          },
        ]
      : [],
  );
}

export function Thread({
  items,
  events,
  sessions,
  snapshot,
  agentNames,
  draft,
  canEnd,
  endingChat,
  onEndChat,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  onReload,
  scrollRef,
  onScroll,
}: {
  items: ConversationItemResponse[];
  events: ConversationTraceEventResponse[];
  sessions: Session[];
  snapshot: HealthSnapshotResponse | null;
  agentNames: Record<string, string>;
  /* The reply being written right now, before its item exists. */
  draft: string | null;
  canEnd: boolean;
  endingChat: string | null;
  onEndChat: (chatId: string) => void;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  onReload: () => void;
  scrollRef: React.RefObject<HTMLDivElement>;
  onScroll: () => void;
}) {
  const slowMs = snapshot?.slow_response_threshold_ms ?? Number.POSITIVE_INFINITY;
  const slowToolMs = snapshot?.slow_tool_threshold_ms ?? Number.POSITIVE_INFINITY;

  const groups = useMemo(() => {
    const byId = new Map(sessions.map((session) => [session.id, session]));
    const runs: ConversationItemResponse[][] = [];
    const runOf = new Map<string, ConversationItemResponse[]>();
    for (const item of items) {
      const last = runs[runs.length - 1];
      const run = item.session_id
        ? runOf.get(item.session_id)
        : last && !last[0].session_id
          ? last
          : undefined;
      if (run) run.push(item);
      else {
        runs.push([item]);
        if (item.session_id) runOf.set(item.session_id, runs[runs.length - 1]);
      }
    }
    /* Turns count on through the thread, so "Turn 7" names one moment in it. */
    let turns = 0;
    const built: Group[] = runs.map((run, i) => {
      const sessionId = run[0].session_id ?? null;
      const key = `s${i}`;
      const timeline = buildExchanges(
        run,
        sessionId ? events.filter((event) => event.session_id === sessionId) : [],
        {
          falseInterruptionGraceS: FALSE_INTERRUPTION_GRACE_S,
          idPrefix: `${key}-`,
          turnOffset: turns,
        },
      );
      turns += timeline.exchanges.filter((x) => x.turn != null).length;
      return {
        key,
        session: sessionId ? (byId.get(sessionId) ?? null) : null,
        timeline,
        multiAgent:
          new Set(
            timeline.exchanges
              .flatMap((x) => x.beats.map((b) => (b.kind === "agent" ? b.item.agent_id : null)))
              .filter(Boolean),
          ).size > 1,
      };
    });
    return built;
  }, [items, events, sessions]);

  const exchanges = useMemo(() => groups.flatMap((group) => group.timeline.exchanges), [groups]);
  /* Which call each anchor lives inside, so a jump can open the call first. */
  const callOf = useMemo(() => {
    const owner = new Map<string, string>();
    for (const group of groups) {
      if (!group.session || group.session.channel === "text") continue;
      for (const exchange of group.timeline.exchanges) {
        owner.set(exchange.id, group.session.id);
        for (const beat of exchange.beats) owner.set(beat.id, group.session.id);
      }
    }
    return owner;
  }, [groups]);

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ExchangeFilter>("all");
  const needle = query.trim().toLowerCase();
  const narrowed = needle !== "" || filter !== "all";
  const visible = useMemo(
    () =>
      new Set(
        exchanges
          .filter((x) => exchangeMatches(x, needle, filter, slowMs, slowToolMs))
          .map((x) => x.id),
      ),
    [exchanges, needle, filter, slowMs, slowToolMs],
  );
  const slowestId = useMemo(() => slowestExchange(exchanges, slowMs), [exchanges, slowMs]);
  const issueTarget = useMemo(
    () => issueTargets(exchanges, slowMs, slowToolMs),
    [exchanges, slowMs, slowToolMs],
  );

  const [collapsedTasks, setCollapsedTasks] = useState<Set<string>>(new Set());
  const toggleTask = useCallback(
    (id: string) =>
      setCollapsedTasks((current) => {
        const next = new Set(current);
        if (!next.delete(id)) next.add(id);
        return next;
      }),
    [],
  );
  /* A call is one line and its summary until asked for: the thread is the
     story of a person, and a forty-turn call in the middle of it buries the
     rest. Its own page is where it is inspected. */
  const [expandedCalls, setExpandedCalls] = useState<Set<string>>(new Set());
  const toggleCall = useCallback(
    (id: string) =>
      setExpandedCalls((current) => {
        const next = new Set(current);
        if (!next.delete(id)) next.add(id);
        return next;
      }),
    [],
  );

  const { flashId, jumpTo } = useJump();
  const jump = useCallback(
    (id: string) => {
      const call = callOf.get(id);
      if (!call || expandedCalls.has(call)) return jumpTo(id);
      setExpandedCalls((current) => new Set(current).add(call));
      // Once its turns are on the page to scroll to.
      requestAnimationFrame(() => requestAnimationFrame(() => jumpTo(id)));
    },
    [callOf, expandedCalls, jumpTo],
  );

  const columns = useMemo(() => columnsOf(exchanges), [exchanges]);

  return (
    <>
      {snapshot && (
        <details className="group shrink-0 border-b border-line">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-5 py-2.5 text-[12.5px] transition-colors hover:bg-hover [&::-webkit-details-marker]:hidden">
            <span
              className={cn("h-1.5 w-1.5 flex-none rounded-full", snapshot.ok ? "bg-live" : "bg-danger")}
              aria-hidden
            />
            <span className="min-w-0 flex-1 truncate font-medium text-ink">
              {snapshot.ok ? "This conversation looks healthy" : snapshot.issues[0].message}
            </span>
            <span className="flex-none text-[11.5px] text-faint">
              {snapshot.issues.length > 1 ? `+${snapshot.issues.length - 1} more · ` : ""}
              <span className="group-open:hidden">details</span>
              <span className="hidden group-open:inline">hide</span>
            </span>
          </summary>
          <div className="px-5 pb-4">
            <SnapshotBand
              snapshot={snapshot}
              columns={columns}
              durationLabel="Total time"
              onJumpToTurn={jump}
              issueTarget={issueTarget}
            />
          </div>
        </details>
      )}

      <div ref={scrollRef} onScroll={onScroll} className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <TimelineToolbar
          query={query}
          onQuery={setQuery}
          filter={filter}
          onFilter={setFilter}
          slowestId={slowestId}
          onJump={jump}
          copy={() => plainText(exchanges, "Customer")}
        />

        {hasOlder && (
          <div className="border-b border-line px-5 py-2">
            <Button
              variant="ghost"
              size="sm"
              className="w-full"
              disabled={loadingOlder}
              onClick={onLoadOlder}
            >
              {loadingOlder ? "Loading…" : "Load older messages"}
            </Button>
          </div>
        )}

        {narrowed && visible.size === 0 ? (
          <div className="px-5 py-12 text-center">
            <p className="text-[13px] font-medium text-ink">No matching turns</p>
            <p className="mt-1 text-[12px] text-muted">Clear the search or widen the filter.</p>
          </div>
        ) : (
          <div className="grid gap-8 px-5 py-5">
            {groups.map((group) => {
              const { session, timeline } = group;
              const matches = timeline.exchanges.some((x) => visible.has(x.id));
              if (narrowed && !matches) return null;
              const isCall = session != null && session.channel !== "text";
              /* A search that lands inside a call opens it: a match nobody can
                 see is not a match. */
              const shown = !isCall || expandedCalls.has(session.id) || (narrowed && matches);
              return (
                <section key={group.key} className="grid gap-5">
                  {session &&
                    (isCall ? (
                      <CallDivider
                        session={session}
                        expanded={shown}
                        onToggle={() => toggleCall(session.id)}
                      />
                    ) : (
                      <ChatStart
                        session={session}
                        canEnd={canEnd}
                        ending={endingChat === session.id}
                        onEnd={() => onEndChat(session.id)}
                      />
                    ))}
                  {shown && matches && (
                    <div className="grid gap-6">
                      <TimelineBlocks
                        blocks={timeline.blocks}
                        view={{
                          agentNames,
                          toolsByName: null,
                          spoken: isCall,
                          openTaskLabel: isCall
                            ? "The call ended during this task"
                            : session && isOpen(session)
                              ? "Still in this task"
                              : "The chat ended during this task",
                          slowMs,
                          slowToolMs,
                          showAgent: group.multiAgent,
                          visible,
                          slowestId,
                          flashId,
                          recordingStartedAt: null,
                          windows: NO_WINDOWS,
                          playheadS: null,
                          onSeek: null,
                          onReload,
                          collapsedTasks,
                          onToggleTask: toggleTask,
                        }}
                      />
                    </div>
                  )}
                  {session && !isCall && !isOpen(session) && (
                    <ChatEnd
                      session={session}
                      columns={columnsOf(timeline.exchanges)}
                      issueTarget={issueTargets(timeline.exchanges, slowMs, slowToolMs)}
                      onJump={jump}
                    />
                  )}
                </section>
              );
            })}
            {draft && <StreamingBeat text={draft} />}
          </div>
        )}
      </div>
    </>
  );
}

/* A thread has no recording to sync with; that lives on the call's own page. */
const NO_WINDOWS = new Map<string, [number, number]>();
