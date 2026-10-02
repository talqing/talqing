"use client";
import { useCallback, useMemo, useState } from "react";
import type { CallToolResponse } from "@talqing/sdk";
import { TimelineBlocks } from "../components/timeline/ExchangeBlock";
import {
  exchangeMatches,
  plainText,
  slowestExchange,
  type ExchangeFilter,
  type Timeline,
} from "../components/timeline/exchanges";
import { TimelineToolbar } from "../components/timeline/Toolbar";

/* The call's own frame around the shared timeline: search, filter, copy, and
   the sync with the recording. What a transcript IS lives in
   `components/timeline/`. */

export function Conversation({
  timeline,
  agentNames,
  tools,
  slowMs,
  slowToolMs,
  recordingStartedAt,
  playheadS,
  flashId,
  onJump,
  onSeek,
  onReload,
  contentDeletedAt,
}: {
  timeline: Timeline;
  agentNames: Record<string, string>;
  tools: CallToolResponse[];
  slowMs: number;
  slowToolMs: number;
  /* Unix seconds the recording began. The migration comment calls this "the
     origin for transcript-synced seeking"; this is the use it was added for. */
  recordingStartedAt: number | null;
  /* Where the player is now, in seconds into the file. Null when not playing. */
  playheadS: number | null;
  /* The exchange or beat just jumped to — from the latency chart, or from an
     issue in the health band. */
  flashId: string | null;
  /* Scroll to an exchange and mark it. Shared with the chart and the band, so
     every route to a moment lands the same way. */
  onJump: (id: string) => void;
  onSeek: ((offsetS: number) => void) | null;
  /* Re-fetch the call, so its image links are signed again. A presigned URL
     lives an hour and a call detail can be left open longer. */
  onReload: () => void;
  /* When this call's content was erased, by retention or by a delete. Null on a
     call that still has its own. It is the difference between an empty
     transcript that means "nobody spoke" and one that means "this was deleted"
     — the same zero rows, opposite meanings, and only one of them is a call
     worth re-listening to. */
  contentDeletedAt: string | null;
}) {
  const { exchanges, blocks } = timeline;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ExchangeFilter>("all");
  /* Tasks open by default: a task is where the call's substance usually is, and
     a collapsed bracket hides the very thing a reader came to check. */
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

  const toolsByName = useMemo(() => new Map(tools.map((t) => [t.name, t])), [tools]);

  /* Only worth labelling when more than one agent spoke. On a single-agent call
     the same name over every bubble is noise; after a handoff it is the answer
     to "which of them said that", and the pill alone is easy to scroll past. */
  const multiAgent = useMemo(
    () =>
      new Set(
        exchanges.flatMap((x) => x.beats.map((b) => (b.kind === "agent" ? b.item.agent_id : null))).filter(Boolean),
      ).size > 1,
    [exchanges],
  );

  /* Each exchange's window in the audio file: it owns the playhead from where it
     starts until the next one does. Computed over ALL exchanges, not the
     filtered ones, so a search does not stretch a window over the gaps. */
  const windows = useMemo(() => {
    const map = new Map<string, [number, number]>();
    if (recordingStartedAt == null) return map;
    const starts = exchanges.map((x) =>
      x.startedAt == null ? null : Math.max(0, x.startedAt - recordingStartedAt),
    );
    exchanges.forEach((x, i) => {
      const from = starts[i];
      if (from == null) return;
      const next = starts.slice(i + 1).find((s) => s != null);
      map.set(x.id, [from, next ?? Number.POSITIVE_INFINITY]);
    });
    return map;
  }, [exchanges, recordingStartedAt]);

  const slowestId = useMemo(() => slowestExchange(exchanges, slowMs), [exchanges, slowMs]);

  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () =>
      new Set(
        exchanges
          .filter((x) => exchangeMatches(x, needle, filter, slowMs, slowToolMs))
          .map((x) => x.id),
      ),
    [exchanges, needle, filter, slowMs, slowToolMs],
  );

  if (exchanges.length === 0) {
    return (
      <div className="px-5 py-14 text-center">
        <p className="text-[14px] font-semibold text-ink">
          {contentDeletedAt ? "Transcript deleted" : "No transcript"}
        </p>
        <p className="mt-1 text-[13px] text-muted">
          {contentDeletedAt
            ? `Erased on ${new Date(contentDeletedAt).toLocaleDateString()} under your organization's data retention policy.`
            : "Nothing was said before this call ended."}
        </p>
      </div>
    );
  }

  return (
    <div className="grid">
      <TimelineToolbar
        query={query}
        onQuery={setQuery}
        filter={filter}
        onFilter={setFilter}
        slowestId={slowestId}
        onJump={onJump}
        copy={() => plainText(exchanges)}
      />

      {visible.size === 0 ? (
        <div className="px-5 py-12 text-center">
          <p className="text-[13px] font-medium text-ink">No matching turns</p>
          <p className="mt-1 text-[12px] text-muted">Clear the search or widen the filter.</p>
        </div>
      ) : (
        <div className="grid gap-6 px-5 py-5">
          <TimelineBlocks
            blocks={blocks}
            view={{
              agentNames,
              toolsByName,
              spoken: true,
              openTaskLabel: "The call ended during this task",
              slowMs,
              slowToolMs,
              showAgent: multiAgent,
              visible,
              slowestId,
              flashId,
              recordingStartedAt,
              windows,
              playheadS,
              onSeek,
              onReload,
              collapsedTasks,
              onToggleTask: toggleTask,
            }}
          />
        </div>
      )}
    </div>
  );
}
