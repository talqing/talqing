"use client";
import { useCallback, useMemo, useState } from "react";
import Link from "next/link";
import type {
  CallDetailResponse,
  CallType,
  SessionEventResponse,
  StreamResponse,
  TransferResponse,
} from "@talqing/sdk";
import { cn } from "@/lib/cn";
import {
  Badge,
  CopyButton,
  DescriptionItem,
  DescriptionList,
  Disclosure,
  Input,
  Panel,
  Select,
  Skeleton,
} from "../components/ui";
import { fmtCount, fmtDuration, SnapshotBand } from "../components/SnapshotBand";
import { RecordingPlayer } from "../components/RecordingPlayer";
import { CallAnalysisPanel, OutcomeBadge } from "./CallAnalysis";
import { CallConfiguration } from "./CallConfiguration";
import { CostBreakdown } from "./CostBreakdown";
import { Conversation } from "./CallTranscript";
import { eventSummary } from "../components/timeline/eventSummary";
import { buildExchanges, issueTargets } from "../components/timeline/exchanges";
import { useJump } from "../components/timeline/useJump";
import { type TurnLatencyColumn } from "../components/TurnLatencyChart";

/* ── formatting ─────────────────────────────────────────────────────────── */

/* A transferred call is short by design, and unlabelled "2m 04s" reads as a
   call that went nowhere. Both the duration and the recording cover only the
   part the agent was on — the caller may have stayed with a person for another
   fifteen minutes — so the ending has to say so. */
/* What the audio on a streamed call actually was. Every one of these is
   negotiated per call by the partner's platform rather than configured here, so
   this is the only place it is knowable — and it is the first thing anyone asks
   when a call sounds wrong. Codec names are the internal ones on purpose: they
   carry the byte order, which is the difference between a clean call and static.

   Deliberately not a "Stream" panel of its own. It is one sentence about how the
   call arrived, which is what the row above it already says for every other
   channel. */
const CODEC_LABEL: Record<string, string> = {
  pcm_s16le: "16-bit linear PCM",
  pcm_s16be: "16-bit linear PCM (big-endian)",
  pcmu: "G.711 μ-law",
};

function streamSentence(stream: StreamResponse): string {
  const platform = stream.dialect ? stream.dialect[0].toUpperCase() + stream.dialect.slice(1) : null;
  const codec = stream.codec ? (CODEC_LABEL[stream.codec] ?? stream.codec) : null;
  const rate = stream.sample_rate ? `${stream.sample_rate / 1000} kHz` : null;
  const audio = [codec, rate].filter(Boolean).join(" at ");
  if (platform && audio) return `Streamed from ${platform} as ${audio}.`;
  if (platform) return `Streamed from ${platform}.`;
  return audio ? `Streamed as ${audio}.` : "Streamed from a partner platform.";
}

function transferSentence(transfer: TransferResponse): string {
  const destination = transfer.destination || "another number";
  if (transfer.outcome === "connected") {
    const briefed =
      transfer.mode === "warm"
        ? " The agent briefed them privately first, while the caller held."
        : "";
    return `Transferred to ${destination}.${briefed} The duration and recording below cover the agent's part of the call only.`;
  }
  /* The reason we actually gave the agent, not a guess at one: "could not reach
     anyone" is wrong for a person who answered and declined. */
  const because = transfer.detail ? `: ${transfer.detail}` : " and could not reach anyone";
  /* What the tool's `on_failure` policy did next. This used to assert that the
     call carried on, which is false under `end_call` — that branch hangs up on
     the caller, and telling a tenant their customer stayed with the agent is
     the opposite of what happened. Null on records written before the policy
     was stored, where saying nothing beats guessing. */
  const next =
    transfer.on_failure === "end_call"
      ? " The call ended there."
      : transfer.on_failure === "continue"
        ? " The call carried on with the agent."
        : "";
  return `Tried to transfer to ${destination}${because}.${next}`;
}

/* No local map of close reasons. There used to be one of thirteen sentences
   against roughly forty real strings — three of which (`agent_end_call`,
   `room_finished`, `max_duration`) no worker has ever written, while the whole
   `sip_*_failed` family, the commonest way a phone call fails, fell through to
   "Sip user unavailable failed". It also had `user_initiated` as "Ended by the
   caller", which is backwards: LiveKit names that reason after whoever called
   `session.shutdown()`, and on this platform that is only ever the agent's own
   `end_call` operation. `services/close_reasons.py` owns the list and ships the
   sentence as `close_reason_label`. */

/* How a call reached the agent, in words. Exported because the list rows say
   the same thing about the same column, and two spellings of one fact ("Inbound
   phone" in the list, `SIP_INBOUND` in the detail) makes a reader wonder whether
   they are looking at two different things. */
export const TYPE_LABEL: Record<CallType, string> = {
  WEB: "Web",
  SIP_INBOUND: "Inbound phone",
  SIP_OUTBOUND: "Outbound phone",
  /* Not "Inbound stream". A partner owns the dialling, so the direction is
     something we are told or do not know, and a label that asserted one would
     be wrong on exactly the calls a partner cares most about. */
  STREAM: "Media stream",
  WHATSAPP_INBOUND: "WhatsApp",
};

/* A WhatsApp user who hides their number reaches us with no phone number at
   all, only an id WhatsApp made up for them. */
export function callerLabel(type: string, from: string | null | undefined): string | null {
  return from || (type === "WHATSAPP_INBOUND" ? "WhatsApp user" : null);
}

/* `+A → +B` is only decodable if you already know that `from` means the caller
   on an inbound call and us on an outbound one — the exact mapping an agent's
   prompt reads as `{{system_vars.human_phone_number}}` so its author never has to
   think about it. Say which is which here too, and name the token that holds it,
   so the vocabulary is discoverable while looking at a real call.

   Deriving the roles from `type` rather than asking the API for them: the rule
   is definitional (an inbound call is one where they rang us), so this copy of
   it cannot drift. A non-SIP call with numbers on it gets the bare arrow — there
   is no caller and no DID to label. */
function PhonePair({
  type,
  from,
  to,
}: {
  type: string;
  from: string | null | undefined;
  to: string | null | undefined;
}) {
  const roles =
    type === "SIP_INBOUND" || type === "WHATSAPP_INBOUND"
      ? ({ from: "them", to: "you" } as const)
      : type === "SIP_OUTBOUND"
        ? ({ from: "you", to: "them" } as const)
        : null;
  const token = (role: "them" | "you") =>
    role === "them"
      ? "{{system_vars.human_phone_number}}"
      : "{{system_vars.agent_phone_number}}";

  const leg = (number: string, role: "them" | "you" | null) => (
    <span className="inline-flex items-baseline gap-1" title={role ? token(role) : undefined}>
      <span className="font-mono text-[12.5px]">{number}</span>
      {role && <span className="text-[11.5px] text-muted">({role})</span>}
    </span>
  );

  return (
    <span className="inline-flex flex-wrap items-baseline gap-1.5">
      {from && leg(from, roles?.from ?? null)}
      {from && to && <span className="text-muted">→</span>}
      {to && leg(to, roles?.to ?? null)}
    </span>
  );
}

/* ── raw event log ──────────────────────────────────────────────────────── */

type EventGroup = "all" | "errors" | "provider" | "tools" | "connection";

function matchesGroup(event: SessionEventResponse, group: EventGroup): boolean {
  if (group === "all") return true;
  if (group === "errors") return event.type.endsWith(".error") || event.type.endsWith(".failed");
  if (group === "provider") return event.type.startsWith("provider.");
  if (group === "tools") return event.type.startsWith("tool.");
  return event.type.startsWith("telemetry.rtc.") || event.type.startsWith("livekit.");
}

function eventTone(type: string): string {
  if (type.endsWith(".error") || type.endsWith(".failed")) return "text-danger";
  if (type.endsWith(".recovered") || type === "agent.ready") return "text-live";
  return "text-ink";
}

export function RawEvents({
  events,
  noun = "call",
}: {
  events: SessionEventResponse[];
  noun?: "call" | "chat";
}) {
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<EventGroup>("all");
  const needle = query.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      events.filter(
        (e) =>
          matchesGroup(e, group) &&
          (!needle ||
            `${e.type} ${e.seq} ${JSON.stringify(e.payload)}`.toLowerCase().includes(needle)),
      ),
    [events, group, needle],
  );
  return (
    <Disclosure
      summary="Raw session events"
      meta={`${filtered.length}/${events.length}`}
    >
      <div className="grid gap-3">
        <p className="text-[12px] leading-4 text-faint">
          What the platform did during this {noun} —{" "}
          {noun === "call"
            ? "lifecycle, provider failovers, tool timing and connection quality"
            : "lifecycle, provider errors and tool timing"}
          . Not the transcript. Newest first, and ordered by sequence rather than
          by clock: a couple of events are recorded later than the moment they describe and carry
          their real time.
        </p>
        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_190px_auto] sm:items-center">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type, sequence or payload…"
            aria-label="Search session events"
          />
          <Select
            value={group}
            onChange={(e) => setGroup(e.target.value as EventGroup)}
            aria-label="Filter session events"
          >
            <option value="all">All events</option>
            <option value="errors">Errors and failures</option>
            <option value="provider">Provider failover</option>
            <option value="tools">Tool calls</option>
            <option value="connection">Connection</option>
          </Select>
          <CopyButton
            value={() => JSON.stringify(filtered, null, 2)}
            label="Copy JSON"
            className="justify-self-start sm:justify-self-end"
          />
        </div>
        {filtered.length === 0 ? (
          <div className="rounded-lg border border-dashed border-line-strong px-4 py-8 text-center">
            <p className="text-[13px] font-medium text-ink">No matching events</p>
            <p className="mt-1 text-[12px] text-muted">Clear the search or widen the group.</p>
          </div>
        ) : (
          <div className="scroll-thin max-h-[30rem] divide-y divide-line overflow-auto rounded-lg border border-line-2">
            {[...filtered].reverse().map((event) => (
              <article key={event.seq} className="grid gap-2 px-3 py-2.5">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className={cn("font-mono text-[12.5px] font-medium", eventTone(event.type))}>
                    {event.type}
                  </span>
                  <time className="text-[11px] tabular-nums text-faint">
                    #{event.seq} · {new Date(event.created_at).toLocaleTimeString()}
                  </time>
                </div>
                {eventSummary(event.type, event.payload) && (
                  <p className="text-[12.5px] leading-5 text-muted">
                    {eventSummary(event.type, event.payload)}
                  </p>
                )}
                {Object.keys(event.payload).length > 0 && (
                  <pre className="scroll-thin max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-md bg-subtle px-2.5 py-2 font-mono text-[11.5px] leading-5 text-ink-soft">
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                )}
              </article>
            ))}
          </div>
        )}
      </div>
    </Disclosure>
  );
}

/* ── header ─────────────────────────────────────────────────────────────── */

/* Every identifier on this page used to be a dead end: bare mono text you could
   read but not follow. `/conversations` has always linked TO `/calls?id=…`; the
   reverse simply did not exist. */
function IdLink({ href, children }: { href: string; children: string }) {
  return (
    <Link
      href={href}
      className="font-mono text-[12.5px] text-ink-soft underline decoration-line-strong underline-offset-2 hover:text-ink hover:decoration-ink"
    >
      {children}
    </Link>
  );
}

/* Four decimals, always. A call costs a couple of cents, so two places throw
   away most of the number — and the health band and the cost ledger below state
   the same figure to four and six places, which a two-place header contradicted
   on screen. */
function money(v: number | null | undefined): string {
  return v == null ? "—" : `$${v.toFixed(4)}`;
}

/**
 * One labelled fact.
 *
 * `lead` is the whole point of the component: eight facts rendered at identical
 * weight is a wall, and the two a person is actually scanning for — how long,
 * how much — end up looking exactly like the correlation id. Local rather than
 * a variant on the shared `DescriptionItem`, because this emphasis belongs to a
 * page header and nowhere else.
 */
function Fact({
  term,
  lead,
  children,
}: {
  term: string;
  lead?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] font-medium leading-4 text-muted">{term}</dt>
      {/* Wraps rather than truncating: the longest value here is a pair of
          timestamps, and half a clipped end time is worse than two lines. */}
      <dd
        className={cn(
          "mt-0.5 min-w-0 text-ink",
          lead
            ? "font-mono text-[17px] font-semibold leading-7 tracking-tight tabular-nums"
            : "text-[13.5px] leading-6",
        )}
      >
        {children}
      </dd>
    </div>
  );
}

/**
 * Lead with the answer.
 *
 * The largest thing on this page used to be the raw session UUID, with a badge
 * beside it reading `completed` — the *transport* status. A tenant scanning a
 * call read "completed" and moved on, while the call's actual verdict (Failure)
 * sat in a panel two scrolls down.
 *
 * Three tiers, because these are three different kinds of fact and flattening
 * them into one grid was the bug: what the call *was* (measured, emphasised),
 * when it ran (supporting), and the ids that identify it (a quiet footer with
 * the affordances — link, copy — that identifiers need and nothing else does).
 */
function CallHeader({ call }: { call: CallDetailResponse }) {
  const s = call.session;
  const statusVariant =
    s.status === "in_progress" ? "live" : s.status === "failed" ? "danger" : "default";
  const started = new Date(s.started_at);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2.5">
        <h2 className="font-display text-[19px] font-semibold tracking-tight text-ink">
          {s.agent.agent_id ? (
            <Link
              href={`/agents/detail?id=${s.agent.agent_id}`}
              className="decoration-line-strong underline-offset-4 hover:underline"
            >
              {s.agent.name || "Unknown agent"}
            </Link>
          ) : (
            (s.agent.name ?? "Unknown agent")
          )}
        </h2>
        {s.analysis.outcome && <OutcomeBadge outcome={s.analysis.outcome} />}
        <Badge variant={statusVariant} dot={statusVariant === "live"}>
          {s.status}
        </Badge>
      </div>

      {/* Four cells, so the row fills rather than leaving an orphan, and the two
          measured ones carry the weight. */}
      <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* The snapshot's duration, not the session column's. `duration_s` is
            written at finalize, so on a live call it is null and this printed
            "—" two inches above a health band already counting the call up in
            real time. `snapshot.duration_seconds` falls back to
            now − started_at while a call is running (`RunFacts.from_session`),
            which is what makes the two agree at every moment of the call. */}
        <Fact term="Duration" lead>
          {fmtDuration(call.snapshot.duration_seconds)}
        </Fact>
        <Fact term="Cost" lead>
          {money(call.cost?.total_charge)}
        </Fact>
        <Fact term="Channel">
          {s.agent.channel} · {TYPE_LABEL[s.type as CallType] ?? s.type}
          {s.agent_version === "draft" ? " · draft" : s.agent_version != null ? ` · v${s.agent_version}` : ""}
        </Fact>
        {/* No end time: `Duration` sits two cells away and start + duration is
            the end, so printing it was restating one fact as two. */}
        <Fact term="Started at">{started.toLocaleString()}</Fact>
      </dl>

      {(s.from_e164 || s.to_e164) && (
        <div className="text-[13px]">
          <PhonePair type={s.type} from={callerLabel(s.type, s.from_e164)} to={s.to_e164} />
        </div>
      )}

      {/* Identifiers, demoted. They are not facts about the call, they are
          handles for finding it again — so they get the affordances (a link to
          this caller's other calls, a copy button) and none of the emphasis. */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-line pt-3 text-[12px]">
        <span className="inline-flex items-center gap-1.5">
          <span className="text-muted">Call id</span>
          <span className="font-mono text-ink-soft">{s.id}</span>
          <CopyButton value={s.id} ariaLabel="Copy call id" />
        </span>
        {s.contact_key && (
          <span className="inline-flex items-center gap-1.5">
            {/* The stable identity of the person on the call. The webhook has
                always carried it; now the dashboard can group on it too.
                Labelled by what following it DOES rather than as "Caller": the
                value is an opaque key (`sip:<did>:<their number>`), and calling
                it the caller invited a reader to parse it as a name when the
                actual numbers are already spelled out above. */}
            <span className="text-muted">All calls with this person</span>
            <IdLink href={`/calls?contact_key=${encodeURIComponent(s.contact_key)}`}>
              {s.contact_key}
            </IdLink>
          </span>
        )}
      </div>
    </div>
  );
}

/* ── the pane ───────────────────────────────────────────────────────────── */

export function CallDetailSkeleton() {
  return (
    <div className="grid content-start gap-4 px-5 py-6" role="status" aria-live="polite">
      <span className="sr-only">Loading call</span>
      <Skeleton className="h-6 w-1/2" />
      <Skeleton className="h-3.5 w-1/3" />
      <Skeleton className="mt-3 h-24 w-full rounded-xl" />
      <Skeleton className="h-16 w-3/4 justify-self-end rounded-xl" />
      <Skeleton className="h-20 w-4/5 rounded-xl" />
    </div>
  );
}

export function CallDetail({
  call,
  agentNames,
  onRecordingDeleted,
  onAnalysisChanged,
  onReload,
}: {
  call: CallDetailResponse;
  agentNames: Record<string, string>;
  onRecordingDeleted: () => void;
  onAnalysisChanged: () => void;
  /** Re-fetch this call. Its image links are presigned for an hour, and a
   *  detail page can sit open longer than that. */
  onReload: () => void;
}) {
  const s = call.session;
  const snapshot = call.snapshot;
  /* How long after the agent stops a false interruption is still reported
     against that speech: the entry agent's own setting, which defaults to
     LiveKit's two seconds. */
  const falseInterruptionGraceS =
    call.agent_config?.turn_handling?.interruption?.false_interruption_timeout ?? 2;
  const timeline = useMemo(
    () => buildExchanges(call.transcript, call.events, { falseInterruptionGraceS }),
    [call.transcript, call.events, falseInterruptionGraceS],
  );

  const columns: TurnLatencyColumn[] = useMemo(
    () =>
      timeline.exchanges.flatMap((exchange) =>
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
      ),
    [timeline],
  );

  const issueTarget = useMemo(
    () =>
      issueTargets(
        timeline.exchanges,
        snapshot.slow_response_threshold_ms,
        snapshot.slow_tool_threshold_ms,
      ),
    [timeline, snapshot.slow_response_threshold_ms, snapshot.slow_tool_threshold_ms],
  );

  const contextLine = useMemo(() => {
    const event = call.events.find((e) => e.type === "conversation.context_loaded");
    return event ? eventSummary(event.type, event.payload) : null;
  }, [call.events]);

  /* The one line that says what this call actually ran on. It was only ever
     visible as the raw payload of `agent.ready` in the trace at the bottom. */
  const modelStack = useMemo(() => {
    const ready = call.events.find((e) => e.type === "agent.ready");
    if (!ready) return null;
    return (["realtime", "llm", "stt", "tts"] as const)
      .map((slot) => (typeof ready.payload[slot] === "string" ? String(ready.payload[slot]) : null))
      .filter((v): v is string => Boolean(v))
      .join(" · ");
  }, [call.events]);

  /* Transcript ↔ audio, the two halves that have always been unrelated
     components. `recording_started_at` exists precisely for this: an item's
     offset into the file is its timestamp minus this origin. */
  const recordingStartedAt = useMemo(() => {
    /* The audio's origin when there is audio, and the screen video's when there
       is not — an agent can record the screen with call recording off, and the
       transcript still wants to seek. Whichever is driving the player is the one
       its offsets are measured from. */
    const source = s.recording.state === "available" ? s.recording : s.screen_recording;
    if (source.state !== "available" || !source.started_at) return null;
    const parsed = Date.parse(source.started_at);
    return Number.isNaN(parsed) ? null : parsed / 1000;
  }, [s.recording, s.screen_recording]);
  /* Held here rather than in the chart or the transcript because it is the
     handshake between them. */
  const { flashId, jumpTo } = useJump();

  const [playheadS, setPlayheadS] = useState<number | null>(null);
  const [seekTo, setSeekTo] = useState<{ at: number; nonce: number } | null>(null);
  const seek = useCallback(
    (at: number) => setSeekTo((current) => ({ at, nonce: (current?.nonce ?? 0) + 1 })),
    [],
  );

  return (
    <div className="grid gap-5 px-5 py-5">
      <CallHeader call={call} />

      {/* One card, two rows. These are the two halves of a single narrative —
          what this call knew walking in and how it left — and two identical
          floating grey bars read as a pair of alerts instead. Beside the header
          rather than buried in the trace below, because "why did this call not
          know the caller?" is asked while looking at it. */}
      {(contextLine || s.stream || s.ended_at) && (
        <dl className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-subtle text-[12.5px] leading-5">
          {contextLine && (
            <div className="grid gap-0.5 px-3 py-2 sm:grid-cols-[110px_minmax(0,1fr)] sm:gap-3">
              <dt className="font-medium text-ink">How it started</dt>
              <dd className="text-muted">{contextLine}</dd>
            </div>
          )}
          {s.stream && (
            <div className="grid gap-0.5 px-3 py-2 sm:grid-cols-[110px_minmax(0,1fr)] sm:gap-3">
              <dt className="font-medium text-ink">Media</dt>
              <dd className="text-muted">{streamSentence(s.stream)}</dd>
            </div>
          )}
          {s.ended_at && (
            <div className="grid gap-0.5 px-3 py-2 sm:grid-cols-[110px_minmax(0,1fr)] sm:gap-3">
              <dt className="font-medium text-ink">How it ended</dt>
              <dd className="text-muted">
                {s.close_reason_label ?? "No ending was recorded"}
                {s.close_reason ? ` (${s.close_reason})` : ""}
                {s.transfer && <span className="block pt-1">{transferSentence(s.transfer)}</span>}
                {/* The raw carrier verdict, which is exactly what an operator
                    debugging a trunk needs and what the agent must never be told. */}
                {s.transfer?.sip_status && (
                  <span className="block pt-1 font-mono text-[12px] text-faint">
                    Carrier said {String(s.transfer.sip_status.code ?? "?")}{" "}
                    {String(s.transfer.sip_status.phrase ?? "")}
                  </span>
                )}
              </dd>
            </div>
          )}
        </dl>
      )}
      {/* One sentence, off the same typed model the conversations page renders.
          This used to pretty-print the raw JSON blob — the worst failure on the
          page getting the worst treatment on the page. */}
      {s.error && (
        <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger">
          <strong className="font-medium">The run failed:</strong>{" "}
          {s.error.message || "no message was recorded"}
          {s.error.type ? ` (${s.error.type})` : ""}
        </p>
      )}

      {/* `showMetering` off: the Cost section below states every token and
          character against the rate it was charged at, which is the same
          quantities plus the answer. The conversations page has no such ledger
          and keeps them. */}
      <SnapshotBand
        snapshot={snapshot}
        columns={columns}
        showMetering={false}
        onJumpToTurn={jumpTo}
        issueTarget={issueTarget}
      />

      {/* Above the transcript: the point of a summary is not having to read the
          transcript, so putting it below would defeat it. */}
      <CallAnalysisPanel
        analysis={s.analysis}
        sessionId={s.id}
        agentId={s.agent.agent_id ?? null}
        inProgress={s.status === "in_progress" || s.status === "queued"}
        contentDeletedAt={s.content_deleted_at ?? null}
        onRerun={onAnalysisChanged}
      />

      <RecordingPlayer
        sessionId={s.id}
        recording={s.recording}
        screenRecording={s.screen_recording}
        onDeleted={onRecordingDeleted}
        onPosition={setPlayheadS}
        seekTo={seekTo}
      />

      <Panel className="p-0">
        <Conversation
          timeline={timeline}
          agentNames={agentNames}
          tools={call.tools}
          slowMs={snapshot.slow_response_threshold_ms}
          slowToolMs={snapshot.slow_tool_threshold_ms}
          recordingStartedAt={recordingStartedAt}
          playheadS={playheadS}
          flashId={flashId}
          onJump={jumpTo}
          onSeek={recordingStartedAt != null ? seek : null}
          onReload={onReload}
          contentDeletedAt={s.content_deleted_at ?? null}
        />
      </Panel>

      <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
        {/* Open by default and merged: "what did this call cost and why" used to
            need two disclosures, and still had no answer at the end of it. */}
        {call.cost && <CostBreakdown cost={call.cost} defaultOpen />}
        {!call.cost && <UsageDisclosure usage={call.usage} />}

        <Disclosure
          summary="Starting prompt"
          meta={
            s.agent_version === "draft"
              ? "draft"
              : s.agent_version != null
                ? `v${s.agent_version}`
                : undefined
          }
        >
          <div className="grid gap-3">
            {/* The old copy claimed this was "the exact instructions this call
                ran with". It is the template: the model received
                `personalize(template, userdata, system)`, resolved once when
                the agent was built, and that resolved text is not kept. */}
            <p className="text-[12px] leading-4 text-faint">
              The published template this call ran, not the agent&rsquo;s current draft. Any{" "}
              <code className="font-mono">{"{{…}}"}</code> below was substituted when the agent was
              built — from the userdata, variables and phone numbers shown further down, and from
              the clock at that moment. The resolved text is not stored.
            </p>
            <pre className="scroll-thin max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-ink px-4 py-3 font-sans text-[13px] leading-6 text-white">
              {call.starting_prompt || "(no prompt recorded)"}
            </pre>
            {call.greeting && (
              <div>
                <div className="mb-1 text-[12px] font-medium text-muted">Greeting</div>
                <p className="rounded-lg border border-line bg-subtle px-3 py-2 text-[13px] leading-5 text-ink">
                  {call.greeting}
                </p>
              </div>
            )}
          </div>
        </Disclosure>

        {call.tools.length > 0 && (
          /* "Attached", not "Available". These come from the published
             version's `config.tools`, and the agent could also call things that
             are not in it: the built-ins the compiler injects (`stop_recording`
             when recording is on), the provider's own server-side tools, and
             every tool from a connected MCP integration — whose definitions are
             fetched live and frozen nowhere, so there is nothing truthful to
             list for them here.
             Calling the panel "available" made its omissions read as absences. */
          <Disclosure summary="Attached tools" meta={`${call.tools.length}`}>
            <p className="mb-2 text-[12px] leading-4 text-faint">
              Pinned to the version this call ran. Built-in tools and any from a connected
              integration are not listed — their definitions are not frozen with the version.
            </p>
            <div className="divide-y divide-line rounded-lg border border-line-2">
              {call.tools.map((tool) => (
                <div key={tool.id} className="grid gap-1 px-3 py-3 sm:grid-cols-[200px_minmax(0,1fr)] sm:gap-5">
                  <div className="min-w-0">
                    <IdLink href={`/tools/detail?id=${tool.id}`}>{tool.name}</IdLink>
                    <div className="mt-1 text-[11px] text-faint">
                      {[
                        tool.long_running_task ? "long-running" : null,
                        tool.silent ? "silent" : null,
                        tool.disable_interruptions ? "blocks interruption" : null,
                      ]
                        .filter(Boolean)
                        .join(" · ") || "standard"}
                    </div>
                  </div>
                  <p className="text-[13px] leading-5 text-muted">
                    {tool.description || "No description."}
                  </p>
                </div>
              ))}
            </div>
          </Disclosure>
        )}

        <CallConfiguration call={call} />

        <UserdataDisclosure
          summary="Userdata"
          note="What this call ended holding — the variable bus its tools read and wrote."
          bag={s.userdata}
        />
        <UserdataDisclosure
          summary="What we know about this caller"
          note="Carried across every call from the same person, not just this one. The agent starts each call already holding it."
          bag={s.contact_userdata}
        />
        {/* Beside the two userdata bags, and third because it is the one nothing
            on the call could write: what the request that started this call
            supplied for {{vars.…}}, before anybody said anything. The declared
            defaults it overrode are on the agent, under Configuration. */}
        <UserdataDisclosure
          summary="Variables"
          note="What the request that started this call supplied for {{vars.…}} — read-only for its whole length, and never written onto the caller's record. Anything this agent declares but the request left out resolved to its own default instead."
          bag={call.vars}
        />

        <RawEvents events={call.events} />

        <Disclosure summary="Runtime identifiers" meta={modelStack ?? undefined}>
          <DescriptionList columns={2}>
            {modelStack && (
              <DescriptionItem term="Model stack">
                <span className="font-mono text-[12.5px]">{modelStack}</span>
              </DescriptionItem>
            )}
            <DescriptionItem term="LiveKit room" mono copyValue={s.livekit_room ?? undefined}>
              {s.livekit_room || "—"}
            </DescriptionItem>
            <DescriptionItem term="Conversation" mono copyValue={s.conversation_id ?? undefined}>
              {s.conversation_id ? (
                <IdLink href={`/conversations?id=${s.conversation_id}`}>{s.conversation_id}</IdLink>
              ) : (
                "—"
              )}
            </DescriptionItem>
            <DescriptionItem term="Contact" mono>
              {s.contact_key ? (
                <IdLink href={`/calls?contact_key=${encodeURIComponent(s.contact_key)}`}>
                  {s.contact_key}
                </IdLink>
              ) : (
                "—"
              )}
            </DescriptionItem>
            <DescriptionItem term="Conversation ref" mono>
              {s.conversation_ref_id || "—"}
            </DescriptionItem>
            <DescriptionItem term="Phone number" mono>
              {s.phone_number_id ? (
                <IdLink href={`/telephony/phone-numbers?id=${s.phone_number_id}`}>{s.phone_number_id}</IdLink>
              ) : (
                "—"
              )}
            </DescriptionItem>
            <DescriptionItem term="Telephony account" mono>
              {s.telephony_account_id || "—"}
            </DescriptionItem>
            {/* A streamed call has neither of the two above — no number and no
                carrier account — so without these the panel is a row of nulls
                and nothing says where the audio came from. */}
            {s.stream && (
              <>
                <DescriptionItem term="Stream connection" mono>
                  {s.stream.connection_id ? (
                    <IdLink href={`/telephony/streams?id=${s.stream.connection_id}`}>
                      {s.stream.connection_name || s.stream.connection_id}
                    </IdLink>
                  ) : (
                    s.stream.connection_name || "—"
                  )}
                </DescriptionItem>
                <DescriptionItem
                  term="Partner call id"
                  mono
                  copyValue={s.stream.platform_call_id ?? undefined}
                >
                  {s.stream.platform_call_id || "—"}
                </DescriptionItem>
              </>
            )}
            <DescriptionItem term="Trigger" mono>
              {s.trigger_id || "—"}
            </DescriptionItem>
            <DescriptionItem term="Integration" mono>
              {s.integration_id ? (
                <IdLink href={`/integrations?id=${s.integration_id}`}>{s.integration_id}</IdLink>
              ) : (
                "—"
              )}
            </DescriptionItem>
            <DescriptionItem term="Billing" mono>
              {s.billing_status}
              {s.billed_at ? ` · ${new Date(s.billed_at).toLocaleString()}` : ""}
            </DescriptionItem>
          </DescriptionList>
        </Disclosure>
      </div>
    </div>
  );
}

export function UserdataDisclosure({
  summary,
  note,
  bag,
}: {
  summary: string;
  note: string;
  bag: Record<string, unknown> | null | undefined;
}) {
  if (!bag || Object.keys(bag).length === 0) return null;
  const entries = Object.entries(bag);
  return (
    <Disclosure summary={summary} meta={`${entries.length} keys`}>
      <div className="mb-2 flex items-center justify-between gap-3">
        <p className="text-[12px] leading-4 text-faint">{note}</p>
        <CopyButton value={() => JSON.stringify(bag, null, 2)} label="Copy JSON" />
      </div>
      <dl className="divide-y divide-line rounded-lg border border-line-2">
        {entries.map(([k, v]) => (
          <div key={k} className="grid gap-1 px-3 py-2.5 sm:grid-cols-[200px_minmax(0,1fr)] sm:gap-5">
            <dt className="break-all font-mono text-[12px] font-medium text-muted">{k}</dt>
            <dd className="min-w-0 whitespace-pre-wrap break-words font-mono text-[12.5px] leading-5 text-ink">
              {typeof v === "string" ? v : JSON.stringify(v, null, 2)}
            </dd>
          </div>
        ))}
      </dl>
    </Disclosure>
  );
}

/**
 * Raw provider usage.
 *
 * Only shown when the call has no priced breakdown — an unpriced or unpriceable
 * call, where the cost panel cannot appear. Once there is a breakdown it says
 * everything this does and states the money too, so showing both was two
 * partial views of one fact.
 */
export function UsageDisclosure({ usage }: { usage: CallDetailResponse["usage"] }) {
  const rows: { kind: string; provider: string; model: string; detail: string }[] = [
    ...usage.llm.map((u) => ({
      kind: "LLM",
      provider: u.provider,
      model: u.model,
      detail: [
        `${fmtCount(u.input_tokens)} in · ${fmtCount(u.input_cached_tokens)} cached · ${fmtCount(u.output_tokens)} out`,
        u.purpose === "analysis" ? "post-call analysis" : null,
        u.priority ? "priority lane" : null,
      ]
        .filter(Boolean)
        .join(" · "),
    })),
    ...usage.stt.map((u) => ({
      kind: "STT",
      provider: u.provider,
      model: u.model,
      detail: [
        `${u.audio_duration.toFixed(1)}s audio`,
        u.input_tokens || u.output_tokens
          ? `${fmtCount(u.input_tokens)} in · ${fmtCount(u.output_tokens)} out`
          : null,
      ]
        .filter(Boolean)
        .join(" · "),
    })),
    ...usage.tts.map((u) => ({
      kind: "TTS",
      provider: u.provider,
      model: u.model,
      detail: [
        `${fmtCount(u.characters_count)} chars · ${u.audio_duration.toFixed(1)}s audio`,
        u.input_tokens || u.output_tokens
          ? `${fmtCount(u.input_tokens)} in · ${fmtCount(u.output_tokens)} out`
          : null,
      ]
        .filter(Boolean)
        .join(" · "),
    })),
    ...usage.realtime.map((u) => ({
      kind: "Realtime",
      provider: u.provider,
      model: u.model,
      // Audio and text are listed apart because a realtime model charges them
      // at very different rates — one token count would hide the bill.
      detail: `${fmtCount(u.input_audio_tokens)} audio in · ${fmtCount(u.output_audio_tokens)} audio out · ${fmtCount(u.input_text_tokens)} text in · ${fmtCount(u.output_text_tokens)} text out · ${u.session_seconds.toFixed(1)}s connected`,
    })),
    ...usage.avatar.map((u) => ({
      kind: "Avatar",
      provider: u.provider,
      model: u.model,
      detail: [
        `${u.seconds.toFixed(1)}s`,
        u.avatar_id,
        u.avatar_session_id ? `session ${u.avatar_session_id}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    })),
  ];
  if (rows.length === 0) return null;
  return (
    <Disclosure summary="Provider usage" meta={`${rows.length} lines`}>
      <div className="divide-y divide-line rounded-lg border border-line-2">
        {rows.map((row, i) => (
          <div key={i} className="grid grid-cols-[70px_minmax(0,1fr)] items-baseline gap-3 px-3 py-2.5">
            <span className="font-mono text-[11px] font-medium uppercase tracking-[0.06em] text-muted">
              {row.kind}
            </span>
            <div className="min-w-0">
              <div className="text-[13px]">
                <span className="text-muted">{row.provider}</span>{" "}
                <strong className="font-mono font-medium text-ink">{row.model}</strong>
              </div>
              <div className="mt-0.5 font-mono text-[11.5px] tabular-nums text-faint">
                {row.detail}
              </div>
            </div>
          </div>
        ))}
      </div>
    </Disclosure>
  );
}
