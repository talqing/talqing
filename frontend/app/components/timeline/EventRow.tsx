"use client";
import { cn } from "@/lib/cn";
import { CopyButton } from "../ui";
import type { Beat, EventTone } from "./exchanges";

const TONE: Record<EventTone, string> = {
  danger: "border-danger/25 bg-danger/[0.05] text-danger",
  warn: "border-warn/25 bg-warn/[0.06] text-warn",
  neutral: "border-line bg-subtle text-muted",
};

/* How a turn ended when it did not end in a reply. Same shape as a platform
   event: it is not anything anybody said. */
export function NoteRow({ beat }: { beat: Extract<Beat, { kind: "note" }> }) {
  return (
    <div
      id={beat.id}
      className={cn("scroll-mt-20 rounded-lg border px-3 py-1.5 text-[12.5px] leading-5", TONE[beat.tone])}
    >
      {beat.text}
    </div>
  );
}

/* Something the platform did, at the moment it did it. Full width and quiet,
   like "Told to the model": it is not anything anybody said. */
export function EventRow({
  beat,
  flashing,
}: {
  beat: Extract<Beat, { kind: "event" }>;
  flashing: boolean;
}) {
  const { event } = beat;
  const cause = event.payload.cause as { request_id?: unknown } | undefined;
  const requestId = typeof cause?.request_id === "string" ? cause.request_id : null;
  const shell = cn(
    "scroll-mt-20 rounded-lg border px-3 py-1.5 text-[12.5px] leading-5 transition-colors duration-700",
    TONE[beat.tone],
    flashing && "bg-info/[0.10]",
  );
  const time = (
    <time className="flex-none text-[11px] tabular-nums text-faint" dateTime={beat.at}>
      {new Date(beat.at).toLocaleTimeString()}
    </time>
  );

  if (event.type === "transfer.briefing") {
    const lines = Array.isArray(event.payload.items) ? event.payload.items : [];
    const reason = event.payload.reason;
    return (
      <details id={beat.id} className={shell}>
        <summary className="flex cursor-pointer list-none items-baseline justify-between gap-3 [&::-webkit-details-marker]:hidden">
          <span className="underline decoration-line-strong underline-offset-2">{beat.text}</span>
          {time}
        </summary>
        {/* The caller could not hear this, it is not in the recording, and it is
            the only answer to "what did we say about this customer to somebody
            else?" — so it reads as the conversation it was. */}
        <div className="mt-2 grid gap-2 border-t border-line pt-2">
          <p className="text-[12px] leading-4 text-faint">
            Spoken on a separate line while the caller was on hold. Not recorded.
          </p>
          {typeof reason === "string" && reason.trim() !== "" && (
            <p className="text-ink-soft">
              They gave the reason: “{reason}”. The caller was not told it.
            </p>
          )}
          {lines.length === 0 ? (
            <p>Nothing was said before it ended.</p>
          ) : (
            <dl className="grid gap-1.5">
              {lines.map((line, i) => {
                const said = line as { role?: string; text?: string };
                return (
                  <div key={i} className="grid grid-cols-[52px_minmax(0,1fr)] gap-2">
                    <dt className="text-[12px] text-faint">
                      {said.role === "operator" ? "Them" : "Agent"}
                    </dt>
                    <dd className="whitespace-pre-wrap break-words text-[13px] text-ink">
                      {said.text}
                    </dd>
                  </div>
                );
              })}
            </dl>
          )}
        </div>
      </details>
    );
  }

  return (
    <div id={beat.id} className={cn(shell, "flex items-baseline justify-between gap-3")}>
      <span className="min-w-0">
        {beat.text}
        {requestId && (
          <span className="ml-2 inline-flex items-baseline gap-1 text-[11.5px] opacity-80">
            <span className="font-mono">{requestId}</span>
            <CopyButton value={requestId} ariaLabel="Copy the provider's request id" />
          </span>
        )}
      </span>
      {time}
    </div>
  );
}
