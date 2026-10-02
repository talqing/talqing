"use client";
import type { CallToolResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { AttachmentImages, Badge, Tooltip } from "../ui";
import { fmtMs, LatencyStageBar } from "../LatencyStageBar";
import { EventRow, NoteRow } from "./EventRow";
import { data, type Beat, type Block, type Item } from "./exchanges";
import { TaskBlock } from "./TaskBlock";
import { ToolBeat } from "./ToolBeat";

/* Everything a beat needs that is about the page rather than about the beat. */
export interface TimelineView {
  agentNames: Record<string, string>;
  /* The definitions this session's tools were pinned to. Null where the page
     does not have them, which is not the same as a tool having none. */
  toolsByName: Map<string, CallToolResponse> | null;
  /* Whether this was said aloud. A chat has no audio, so nothing is "spoken
     for" a length of time and a wait is not a silence. */
  spoken: boolean;
  /* What to say of a task that has not returned. */
  openTaskLabel: string;
  slowMs: number;
  slowToolMs: number;
  /* Name the agent over each reply: only worth it when more than one spoke. */
  showAgent: boolean;
  /* Exchanges a search or filter left in. */
  visible: Set<string>;
  slowestId: string | null;
  /* The exchange or beat just jumped to, marked briefly so the reader can see
     which of the several on screen they asked for. */
  flashId: string | null;
  /* Unix seconds the recording began, and where its playhead is now. Null
     without a recording, or when it is not playing. */
  recordingStartedAt: number | null;
  /* Each exchange's [from, until) seconds into the recording. */
  windows: Map<string, [number, number]>;
  playheadS: number | null;
  onSeek: ((offsetS: number) => void) | null;
  /* Re-fetch, so image links are signed again. */
  onReload: () => void;
  collapsedTasks: Set<string>;
  onToggleTask: (id: string) => void;
}

/* `metrics` is free-form JSON on the wire, so what the model chips read out of
   it is narrowed here rather than asserted at each chip. */
const modelMeta = (value: unknown) =>
  (value ?? null) as { model_provider?: string | null; model_name?: string | null } | null;

function clock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* A moment in the call: its place in the recording when there is one to seek,
   the wall clock when there is not. */
function Moment({ at, offsetS, view }: { at: string; offsetS: number | null; view: TimelineView }) {
  if (view.onSeek && offsetS != null) {
    const seek = view.onSeek;
    return (
      <button
        type="button"
        onClick={() => seek(offsetS)}
        className="tabular-nums underline underline-offset-2 hover:text-ink"
        title="Play the recording from here"
      >
        {clock(offsetS)}
      </button>
    );
  }
  return (
    <time className="normal-case" dateTime={at}>
      {new Date(at).toLocaleTimeString()}
    </time>
  );
}

function hasVisible(block: Block, visible: Set<string>): boolean {
  return block.kind === "segment"
    ? visible.has(block.exchange.id)
    : block.blocks.some((inner) => hasVisible(inner, visible));
}

/** The transcript, drawn: exchanges in order, a task as one bracket around
 *  whatever happened inside it. */
export function TimelineBlocks({ blocks, view }: { blocks: Block[]; view: TimelineView }) {
  return (
    <>
      {blocks.map((block, i) => {
        if (!hasVisible(block, view.visible)) return null;
        if (block.kind === "segment") {
          return <ExchangeBlock key={`${block.exchange.id}-${i}`} segment={block} view={view} />;
        }
        const { task } = block;
        const offsetS =
          view.recordingStartedAt == null
            ? null
            : Math.max(0, Date.parse(task.start.created_at) / 1000 - view.recordingStartedAt);
        return (
          <TaskBlock
            key={task.id}
            task={task}
            clock={<Moment at={task.start.created_at} offsetS={offsetS} view={view} />}
            endedInside={view.openTaskLabel}
            collapsed={view.collapsedTasks.has(task.id)}
            onToggle={() => view.onToggleTask(task.id)}
          >
            <TimelineBlocks blocks={block.blocks} view={view} />
          </TaskBlock>
        );
      })}
    </>
  );
}

function ExchangeBlock({
  segment,
  view,
}: {
  segment: Extract<Block, { kind: "segment" }>;
  view: TimelineView;
}) {
  const { exchange, beats, opens } = segment;
  const audioWindow = view.windows.get(exchange.id) ?? null;
  const playing =
    view.playheadS != null &&
    audioWindow != null &&
    view.playheadS >= audioWindow[0] &&
    view.playheadS < audioWindow[1];

  return (
    /* No box around the slowest turn. A persistent amber ring drew the eye to
       one turn for the whole length of the call and read as an error state on
       what is often merely the slowest of several fast turns — the "slowest"
       word in the header below says it without shouting.
       The flash is the opposite kind of mark: transient, and deliberately the
       same hue and shape as the playhead highlight, because both answer "which
       turn is the one I just asked for". Only the background changes, so
       arriving costs no layout shift; `duration-700` is what makes it fade
       rather than blink off. */
    <section
      id={opens ? exchange.id : undefined}
      className={cn(
        "grid gap-2.5 scroll-mt-20 rounded-lg transition-colors duration-700",
        view.flashId === exchange.id ? "bg-info/[0.10]" : playing && "bg-info/[0.04]",
      )}
    >
      {/* Only where the exchange begins: one that carries on after a task
          closes is the same turn, not a new one. */}
      {opens && (
        <div className="flex items-center gap-3 text-[11px] uppercase tracking-[0.08em] text-faint">
          <span>{exchange.label}</span>
          {exchange.id === view.slowestId && <span className="text-warn">slowest</span>}
          <span className="h-px flex-1 bg-line" />
          <Moment
            at={
              exchange.startedAt != null
                ? new Date(exchange.startedAt * 1000).toISOString()
                : exchange.at
            }
            offsetS={audioWindow?.[0] ?? null}
            view={view}
          />
        </div>
      )}
      {beats.map((beat) => (
        <BeatView key={beat.id} beat={beat} view={view} />
      ))}
    </section>
  );
}

function BeatView({ beat, view }: { beat: Beat; view: TimelineView }) {
  const flashing = view.flashId === beat.id;
  switch (beat.kind) {
    case "caller":
      return <CallerBeat item={beat.item} id={beat.id} view={view} />;
    case "agent":
      return <AgentBeat beat={beat} flashing={flashing} view={view} />;
    case "told":
      /* Full width and quiet: not something anyone said, but something the
         model was told, and part of why it answered the way it did. */
      return (
        <div className="rounded-lg border border-dashed border-line-strong bg-subtle px-3 py-2">
          <div className="mb-1 text-[10.5px] font-medium uppercase tracking-[0.08em] text-faint">
            Told to the model
          </div>
          <p className="whitespace-pre-wrap text-[12.5px] leading-5 text-muted">
            {beat.item.text}
          </p>
        </div>
      );
    case "tool":
      return (
        <div className={cn("rounded-lg transition-colors duration-700 sm:pl-6", flashing && "bg-info/[0.10]")}>
          <ToolBeat
            beat={beat}
            tool={
              view.toolsByName
                ? (view.toolsByName.get(String(data(beat.call).name ?? "")) ?? null)
                : undefined
            }
            slowMs={view.slowToolMs}
          />
        </div>
      );
    case "handoff": {
      const pill = (
        <span className="rounded-full border border-line bg-subtle px-3 py-1 text-[12px] font-medium text-muted">
          {handoffLabel(beat.item, view.agentNames)}
        </span>
      );
      return (
        <div className="flex justify-center">
          {/* What the next agent was given to start from, which is the first
              thing asked when it then repeats a question. */}
          {beat.context ? <Tooltip label={beat.context}>{pill}</Tooltip> : pill}
        </div>
      );
    }
    case "event":
      return <EventRow beat={beat} flashing={flashing} />;
    case "note":
      return <NoteRow beat={beat} />;
    // Drawn by the bracket itself.
    case "task_start":
    case "task_end":
      return null;
  }
}

/** What one activity switch reads as.
 *
 *  The server writes the sentence whenever it knows who the switch was to.
 *  The fallback covers the row LiveKit writes when the entry agent takes the
 *  session: nothing has been registered by then, so the server leaves its own
 *  placeholder and the name comes from the id. */
function handoffLabel(item: Item, agentNames: Record<string, string>): string {
  const written = (item.text ?? "").trim();
  if (written && written !== "Agent handoff") return written;
  const name = agentNames[String(data(item).new_agent_id ?? "")] ?? "another agent";
  return data(item).old_agent_id == null ? `▸ entered ${name}` : `→ handed off to ${name}`;
}

/* Same shape and colour as the CoPilot's own chat: the caller's turn is a grey
   card on a hairline, not a black slab. One vocabulary for "a person said this"
   across the product, with the side of the column doing the work of saying who. */
function CallerBeat({ item, id, view }: { item: Item; id: string; view: TimelineView }) {
  const text = item.text ?? "";
  /* Keys are not speech, and reading them as a sentence the caller said is
     wrong to anyone auditing the call. The stored text carries a label for the
     model's benefit; here the row itself is the label. */
  if (item.origin === "keypad") {
    return (
      <div id={id} className="flex justify-end">
        <div className="rounded-lg border border-line bg-subtle px-3 py-1.5 text-[12.5px] text-muted">
          Caller entered{" "}
          <span className="font-mono font-medium tracking-[0.18em] text-ink">
            {text.slice(text.indexOf(":") + 1).trim()}
          </span>
        </div>
      </div>
    );
  }
  return (
    <div id={id} className="flex justify-end">
      <div className="grid max-w-[82%] gap-2 rounded-2xl rounded-br-lg border border-line-2 bg-subtle px-3.5 py-2.5">
        {/* Above the words, in the order they were sent: on a call the caller
            attaches the photo and then says what it is for. */}
        {item.attachments?.length > 0 && (
          <AttachmentImages images={item.attachments} onReload={view.onReload} />
        )}
        {text && <p className="whitespace-pre-wrap text-[14px] leading-[1.5] text-ink">{text}</p>}
        {/* Only worth saying where speech is the norm: on a chat every message
            is typed. */}
        {item.origin === "typed" && view.spoken && (
          <div className="text-[11px] text-faint" title="Sent as text during the call, not spoken">
            Typed
          </div>
        )}
        {/* Nothing else on this bubble. No STT / endpoint timings — the agent's
            response bar carries both as stages of the one number that matters.
            No `transcript_confidence` either: half the providers we run never
            report one, so a badge would fire on every turn of those calls and
            on none of the others. */}
      </div>
    </div>
  );
}

function ModelChip({
  label,
  meta,
}: {
  label: string;
  meta?: { model_provider?: string | null; model_name?: string | null } | null;
}) {
  if (!meta?.model_name) return null;
  /* Lower-cased to the catalog's house style. These come straight off LiveKit's
     per-turn metadata, where each plugin spells its own name however it likes
     ("Sarvam", "xai"), while the cost ledger and provider usage on the same page
     use the canonical catalog id ("sarvam", "xai"). Only the case is touched —
     the name itself is reported, not guessed at. */
  return (
    <span className="inline-flex items-baseline gap-1 rounded-md border border-line bg-white px-1.5 py-0.5 text-[11px] text-muted">
      <span>{label}</span>
      <strong className="font-mono font-medium text-ink-soft">
        {meta.model_provider ? `${meta.model_provider.toLowerCase()}/` : ""}
        {meta.model_name}
      </strong>
    </span>
  );
}

const WAIT_LABEL: Record<NonNullable<Item["latency"]>["kind"], string> = {
  response: "Response time",
  reply: "Replied in",
  after_filler: "Wait before this reply",
};

function AgentBeat({
  beat,
  flashing,
  view,
}: {
  beat: Extract<Beat, { kind: "agent" }>;
  flashing: boolean;
  view: TimelineView;
}) {
  const { item } = beat;
  const metrics = item.metrics;
  const spokeMs =
    typeof metrics?.started_speaking_at === "number" &&
    typeof metrics?.stopped_speaking_at === "number"
      ? (metrics.stopped_speaking_at - metrics.started_speaking_at) * 1000
      : null;
  const agentLabel = view.showAgent && item.agent_id ? view.agentNames[item.agent_id] : undefined;
  const wait = item.latency;
  /* On a chat the headline ends at the first token, and a channel like WhatsApp
     sends nothing until the reply is whole — so how long that took is said
     beside it rather than lost. */
  const fullReplyMs =
    !view.spoken && wait?.kind === "reply" && spokeMs != null ? wait.total_ms + spokeMs : null;

  return (
    <div id={beat.id} className="flex scroll-mt-20 justify-start">
      {/* The CoPilot's answering card: white on the same hairline, wider than
          the caller's because it carries the timing breakdown. */}
      <div
        className={cn(
          "grid max-w-[88%] gap-2.5 rounded-2xl rounded-bl-lg border border-line-2 px-3.5 py-2.5 transition-colors duration-700",
          flashing ? "bg-info/[0.10]" : "bg-white",
        )}
      >
        {/* Who spoke, and — when nobody asked — why. On a call with a handoff
            the bubbles were unattributed after the pill, which is easy to
            scroll past. */}
        {(agentLabel || beat.checkIn) && (
          <div className="text-[11px] font-medium text-faint">
            {[
              agentLabel &&
                `${agentLabel}${
                  item.agent_version === "draft"
                    ? " · draft"
                    : item.agent_version != null
                      ? ` · v${item.agent_version}`
                      : ""
                }`,
              beat.checkIn,
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
        )}
        <p className="whitespace-pre-wrap text-[14px] leading-[1.5] text-ink">{item.text}</p>
        {wait && view.spoken && (
          <LatencyStageBar
            label={wait.kind === "after_filler" ? "Silence before this reply" : WAIT_LABEL[wait.kind]}
            totalMs={wait.total_ms}
            slowMs={view.slowMs}
            stages={wait.stages}
          />
        )}
        {/* A chat reads as a chat: the wait is one quiet line under the reply,
            and where it went is a click away. A slow one opens itself. */}
        {wait && !view.spoken && (
          <details className="group/wait" open={wait.total_ms > view.slowMs}>
            <summary className="flex cursor-pointer list-none flex-wrap items-baseline gap-x-1.5 text-[11.5px] text-faint hover:text-muted [&::-webkit-details-marker]:hidden">
              <span>{WAIT_LABEL[wait.kind]}</span>
              <span
                className={cn(
                  "font-mono tabular-nums",
                  wait.total_ms > view.slowMs ? "font-medium text-danger" : "text-ink-soft",
                )}
              >
                {fmtMs(wait.total_ms)}
              </span>
              {fullReplyMs != null && <span>· full reply after {fmtMs(fullReplyMs)}</span>}
            </summary>
            <LatencyStageBar
              bare
              className="mt-2 min-w-[260px]"
              label={WAIT_LABEL[wait.kind]}
              totalMs={wait.total_ms}
              slowMs={view.slowMs}
              stages={wait.stages}
            />
          </details>
        )}
        <div className="flex flex-wrap items-center gap-1.5 empty:hidden">
          {/* On a call every reply names its stack, because three models made
              it. A chat has one, so it is named when it changes and not
              repeated under every message. */}
          {(view.spoken || beat.newModel) && (
            <ModelChip label="LLM" meta={modelMeta(metrics?.llm_metadata)} />
          )}
          <ModelChip label="TTS" meta={modelMeta(metrics?.tts_metadata)} />
          <ModelChip label="STT" meta={modelMeta(beat.answers?.metrics?.stt_metadata)} />
          {spokeMs != null && view.spoken && (
            <span className="text-[11px] tabular-nums text-faint" title="How long the agent spoke for">
              spoke for {fmtMs(spokeMs)}
            </span>
          )}
          {/* A real barge-in and the agent stopping for a cough are opposite
              findings, and the same badge on both hid which this was. */}
          {beat.falseInterruption ? (
            <Badge
              variant="warn"
              title="The agent stopped for something that was not speech"
            >
              Paused for background noise ·{" "}
              {beat.falseInterruption.resumed ? "resumed" : "did not resume"}
            </Badge>
          ) : (
            Boolean(data(item).interrupted) && <Badge variant="warn">Interrupted</Badge>
          )}
          <Delivery item={item} />
        </div>
      </div>
    </div>
  );
}

/* Whether a reply sent over a messaging channel reached the provider. Nothing
   on a web chat or a call, where there is no provider to reach. */
function Delivery({ item }: { item: Item }) {
  if (item.delivery_status === "not_applicable") return null;
  if (item.delivery_status === "failed") {
    return (
      <span className="text-[11px] font-medium text-danger">
        Not delivered{item.delivery_error ? ` · ${item.delivery_error.message}` : ""}
      </span>
    );
  }
  const label = { sent: "Sent", pending: "Sending…", sending: "Sending…", skipped: "Not sent" }[
    item.delivery_status
  ];
  return <span className="text-[11px] text-faint">{label}</span>;
}

/** A reply as it is being written: an agent beat with a caret, before the item
 *  it will become exists. */
export function StreamingBeat({ text }: { text: string }) {
  return (
    <div className="flex justify-start">
      <div className="max-w-[88%] rounded-2xl rounded-bl-lg border border-line-2 bg-white px-3.5 py-2.5">
        <p className="whitespace-pre-wrap text-[14px] leading-[1.5] text-ink">
          {text}
          <span className="ml-0.5 inline-block h-3.5 w-[3px] animate-pulse bg-ink/40 align-middle" />
        </p>
      </div>
    </div>
  );
}
