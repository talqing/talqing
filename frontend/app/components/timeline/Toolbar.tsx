"use client";
import { CopyButton, Input, Select } from "../ui";
import type { ExchangeFilter } from "./exchanges";

/* Search, filter and copy over a timeline. Sticks to the top of whatever
   scrolls it, so it stays in reach on a long transcript. */
export function TimelineToolbar({
  query,
  onQuery,
  filter,
  onFilter,
  slowestId,
  onJump,
  copy,
}: {
  query: string;
  onQuery: (query: string) => void;
  filter: ExchangeFilter;
  onFilter: (filter: ExchangeFilter) => void;
  /* The slowest turn, when one was slow enough to be worth a shortcut. */
  slowestId: string | null;
  onJump: (id: string) => void;
  copy: () => string;
}) {
  return (
    <div className="sticky top-0 z-10 grid gap-2 border-b border-line bg-white/95 px-5 py-3 backdrop-blur sm:grid-cols-[minmax(0,1fr)_170px_auto] sm:items-center">
      <Input
        value={query}
        onChange={(e) => onQuery(e.target.value)}
        placeholder="Search the transcript…"
        aria-label="Search the transcript"
      />
      <Select
        value={filter}
        onChange={(e) => onFilter(e.target.value as ExchangeFilter)}
        aria-label="Filter turns"
      >
        <option value="all">All turns</option>
        <option value="tools">With tool calls</option>
        <option value="issues">Slow or failed</option>
      </Select>
      <div className="flex items-center gap-2 justify-self-start sm:justify-self-end">
        {/* Goes through the same jump as the latency chart's columns, so it
            marks the turn on arrival instead of dropping the reader into a
            screenful of turns with no clue which one it meant. */}
        {slowestId && (
          <button
            type="button"
            onClick={() => onJump(slowestId)}
            className="text-[12.5px] text-warn underline underline-offset-2"
          >
            Jump to slowest turn
          </button>
        )}
        <CopyButton value={copy} label="Copy" ariaLabel="Copy the transcript" />
      </div>
    </div>
  );
}
