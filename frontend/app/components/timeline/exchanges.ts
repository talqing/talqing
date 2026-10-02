import type { ConversationItemResponse, ReplyLatency, SessionEventResponse } from "@talqing/sdk";
import { eventSummary } from "./eventSummary";

/* A transcript, as the order things happened in.
 *
 * The API returns items in the right order already. What this adds is shape:
 * which caller turn each item answers (an exchange), which stretch was a task,
 * and where the platform's own events — a provider error, a hold, a keypress —
 * fall between them. Nothing here re-sorts an item. */

export type Item = ConversationItemResponse;

/* One trace row. A call's own trace needs no session on it; a conversation's
   spans several, each numbering its events from one. */
export type TraceEvent = SessionEventResponse & { session_id?: string };

/** LiveKit's own fields for one item: a tool call's name and arguments, a
 *  handoff's kind, whether a reply was interrupted. */
export const data = (item: Item) => (item.metadata.data ?? {}) as Record<string, unknown>;

const eventId = (event: TraceEvent) =>
  event.session_id ? `event-${event.session_id.slice(0, 8)}-${event.seq}` : `event-${event.seq}`;

/* What `tool.ended` recorded for one call_id. The event trace is the only place
   a tool's outcome is *stated*; the transcript only holds what it sent back to
   the model, which for a great many tools is nothing at all. */
export interface ToolTrace {
  status: "done" | "error" | "cancelled";
  /* "Result or error text; None when there is nothing to voice" — the payload
     the card falls back to when there is no output row to render. */
  message: string | null;
  durationMs: number | null;
  /* The tool handed the turn back the moment it was dispatched, because it is
     `long_running_task` and this call ran on voice or video. `durationMs` is
     then how long the WORK took, which is not how long the caller waited — the
     only tool call where those two numbers come apart. */
  background: boolean;
}

export type ToolVerdict = "ok" | "failed" | "cancelled" | "silent" | "unfinished";

/**
 * What actually happened to a tool call, from both readers at once.
 *
 * A missing `function_call_output` row is the ORDINARY case, not a truncated
 * call: LiveKit passes `None` for any tool that produced nothing to send back to
 * the model — one that raises `StopResponse`, or ends the call — and we
 * deliberately persist no row for it. Judging on the output row alone therefore
 * accused every silent tool of never returning, while the health band, reading
 * the trace, counted the same call as a success. The trace decides.
 *
 * Cancellation is checked first for the same reason: a background tool DOES have
 * an output row — the line the model was handed when the work was dispatched —
 * so a run the caller then cancelled would otherwise read "ok" off a row written
 * before anything happened.
 */
export function toolVerdict(output: Item | null, trace: ToolTrace | null): ToolVerdict {
  if (trace?.status === "cancelled") return "cancelled";
  if (output) return data(output).is_error || trace?.status === "error" ? "failed" : "ok";
  if (!trace) return "unfinished";
  if (trace.status === "error") return "failed";
  return "silent";
}

export type EventTone = "danger" | "warn" | "neutral";

export interface ToolBeat {
  kind: "tool";
  id: string;
  call: Item;
  output: Item | null;
  trace: ToolTrace | null;
  verdict: ToolVerdict;
  /* For a cancelled tool: whether the caller cut it off, or the call moved on
     without it. The trace says only "cancelled". */
  cancelledBy: "caller" | "call";
}

export interface TimelineTask {
  /* The tool call that entered it, which both bracketing rows carry. */
  id: string;
  name: string;
  start: Item;
  /* Null when the call ended inside the task. */
  end: Item | null;
  output: Item | null;
  trace: ToolTrace | null;
  /* Caller and agent lines spoken inside it. */
  messages: number;
}

export type Beat =
  | { kind: "caller"; id: string; item: Item }
  | {
      kind: "agent";
      id: string;
      item: Item;
      /* The caller message this was the first reply to, which carries the
         half of the turn's metrics measured on the way in. */
      answers: Item | null;
      /* "Check-in 1 of 2": why the agent spoke unprompted. */
      checkIn: string | null;
      /* The agent stopped for something that was not speech. Null when it did
         not; otherwise whether it carried on by itself. */
      falseInterruption: { resumed: boolean } | null;
      /* Whether a different model wrote this than wrote the reply before it. */
      newModel: boolean;
    }
  | { kind: "told"; id: string; item: Item }
  | ToolBeat
  | {
      kind: "handoff";
      id: string;
      item: Item;
      /* What the next agent was given, from the `agent.handoff` trace event. */
      context: string | null;
    }
  | { kind: "task_start"; id: string; task: TimelineTask }
  | { kind: "task_end"; id: string; task: TimelineTask }
  | {
      kind: "event";
      id: string;
      at: string;
      tone: EventTone;
      text: string;
      /* The event behind the row, for the two that carry more than a sentence:
         a provider error's request id, a briefing's lines. */
      event: TraceEvent;
    }
  /* How the turn a message opened ended, when it did not end in a reply. */
  | { kind: "note"; id: string; tone: EventTone; text: string };

export interface Exchange {
  /* The DOM anchor: what the latency chart and the health band jump to. */
  id: string;
  /* "Turn 3" for a caller's turn. What the agent did unprompted is named for
     why it happened instead, and never numbered. */
  label: string;
  /* The caller's turn number, null on an agent-initiated exchange. */
  turn: number | null;
  at: string;
  /* When it began, as unix seconds, for syncing with the recording. */
  startedAt: number | null;
  beats: Beat[];
  /* The caller's wait for the first thing said back. */
  response: ReplyLatency | null;
}

/* What draws: an exchange's beats, split wherever a task opens or closes, so a
   task can be one bracket around everything that happened inside it — including
   the caller turns that began there. */
export type Block =
  | { kind: "segment"; exchange: Exchange; beats: Beat[]; opens: boolean }
  | { kind: "task"; task: TimelineTask; blocks: Block[] };

export interface Timeline {
  exchanges: Exchange[];
  blocks: Block[];
}

const handoffKind = (item: Item) => data(item).kind;

/* A real change of agent, as opposed to the row LiveKit writes when the entry
   agent takes the session, or the pair that brackets a task. */
const isRealHandoff = (item: Item) =>
  item.type === "agent_handoff" &&
  data(item).old_agent_id != null &&
  handoffKind(item) !== "task" &&
  handoffKind(item) !== "task_return";

/** When this item began, as unix seconds: its speech window when it has one,
 *  else when it was produced. */
function startedAt(item: Item): number {
  const speech = item.metrics?.started_speaking_at;
  return typeof speech === "number" ? speech : Date.parse(item.created_at) / 1000;
}

export function toolTracesFromEvents(events: TraceEvent[]): Map<string, ToolTrace> {
  const map = new Map<string, ToolTrace>();
  for (const event of events) {
    if (event.type !== "tool.ended") continue;
    const {
      call_id: callId,
      status,
      message,
      duration_ms: duration,
      background,
    } = event.payload;
    if (typeof callId !== "string") continue;
    if (status !== "done" && status !== "error" && status !== "cancelled") continue;
    map.set(callId, {
      status,
      message: typeof message === "string" ? message : null,
      durationMs: typeof duration === "number" ? duration : null,
      background: background === true,
    });
  }
  return map;
}

const seconds = (ms: unknown) => (typeof ms === "number" ? `${Math.round(ms / 1000)}s` : null);

/**
 * The trace events worth a row in the transcript, as one sentence each.
 *
 * Curated: lifecycle, tool timing and participant events are already said
 * elsewhere on the page, and a row per connection-quality reading would bury the
 * conversation. Events that describe one stretch are folded into one row — a
 * hold is its start and its end, a keypad entry is its digits, and a
 * connection that stays poor is one drop, not a row per reading.
 */
function inlineEvents(events: TraceEvent[]): Extract<Beat, { kind: "event" }>[] {
  const rows: Extract<Beat, { kind: "event" }>[] = [];
  const row = (event: TraceEvent, tone: EventTone, text: string) =>
    rows.push({ kind: "event", id: eventId(event), at: event.created_at, tone, text, event });
  const degraded = new Set<string>();
  /* Whether this event carries on what the last row already says: the same
     kind of thing, moments later. */
  let lastAt = 0;
  const continues = (event: TraceEvent) => {
    const at = Date.parse(event.created_at);
    const same = rows[rows.length - 1]?.event.type === event.type && at - lastAt < 10_000;
    lastAt = at;
    return same;
  };

  events.forEach((event, i) => {
    const { type, payload } = event;
    const previous = rows[rows.length - 1];
    const summary = eventSummary(type, payload);
    if (type === "session.error") {
      row(event, payload.recoverable ? "warn" : "danger", summary!);
    } else if (type === "provider.failed" || type === "provider.recovered") {
      const who = `${payload.provider}/${payload.model} (${String(payload.component).toUpperCase()})`;
      row(
        event,
        type === "provider.failed" ? "warn" : "neutral",
        type === "provider.failed"
          ? `${who} stopped responding, so the call moved to its backup`
          : `${who} recovered`,
      );
    } else if (type === "tool.step_limit_reached") {
      row(event, "warn", summary!);
    } else if (type === "hold.started") {
      const ended = events.slice(i + 1).find((e) => e.type === "hold.ended");
      const length = seconds(ended?.payload.duration_ms);
      row(
        event,
        "neutral",
        ended
          ? `On hold${length ? ` ${length}` : ""} · ${
              ended.payload.returned_to_agent ? "back to the agent" : "connected to a person"
            }`
          : "Put on hold while the transfer dialled",
      );
    } else if (type === "transfer.briefing") {
      const lines = Array.isArray(payload.items) ? payload.items.length : 0;
      row(
        event,
        "neutral",
        `Briefed ${payload.destination ?? "the person answering"} privately · ${lines} ${
          lines === 1 ? "line" : "lines"
        }`,
      );
    } else if (type.startsWith("recording.") || type.startsWith("screenshare.")) {
      if (summary) row(event, "neutral", summary);
    } else if (type === "dtmf.received" || type === "dtmf.sent") {
      const verb = type === "dtmf.received" ? "Caller pressed" : "Agent sent";
      // One entry, not a row per key.
      if (continues(event)) previous.text += ` ${payload.digit}`;
      else row(event, "neutral", `${verb} ${payload.digit}`);
    } else if (type === "stream.underrun" || type === "stream.inbound_dropped") {
      const text =
        type === "stream.underrun"
          ? `The agent's audio fell ${seconds(payload.behind_ms) ?? "a moment"} behind — the caller heard gaps`
          : `Late caller audio was discarded (${seconds(payload.dropped_ms) ?? "a moment"}) — the agent may have missed words`;
      // A second reading of the same stretch replaces the first.
      if (continues(event)) previous.text = text;
      else row(event, "warn", text);
    } else if (type === "telemetry.rtc.quality") {
      const side = String(payload.side ?? payload.identity ?? "");
      const bad = payload.quality === "poor" || payload.quality === "lost";
      if (bad && !degraded.has(side)) {
        const whose =
          payload.side === "agent" ? "our side" : payload.side === "caller" ? "the caller's side" : "one side";
        row(
          event,
          "warn",
          payload.quality === "lost"
            ? `The connection was lost on ${whose}`
            : `Connection quality dropped to poor on ${whose}`,
        );
      }
      if (bad) degraded.add(side);
      else degraded.delete(side);
    }
  });
  return rows;
}

/**
 * Fold a call's transcript and trace into exchanges.
 *
 * An exchange opens on a caller message and holds everything up to the next
 * one, IN STORED ORDER — a line the agent said before calling a tool stays
 * before it, and what it said afterwards is a second beat in the same exchange
 * rather than a new turn. Anything that happens before the caller speaks, or
 * because the agent spoke unprompted, opens an agent-initiated exchange.
 *
 * `falseInterruptionGraceS` is how long after the agent stops a false
 * interruption is still reported against that speech: the agent's
 * `false_interruption_timeout`. A thread draws several timelines on one page:
 * `idPrefix` keeps their anchors apart, and `turnOffset` carries the turn count
 * on from the one before.
 */
export function buildExchanges(
  items: Item[],
  events: TraceEvent[],
  {
    falseInterruptionGraceS,
    idPrefix = "",
    turnOffset = 0,
  }: { falseInterruptionGraceS: number; idPrefix?: string; turnOffset?: number },
): Timeline {
  const traces = toolTracesFromEvents(events);
  const outputs = new Map<string, Item>();
  const taskEnds = new Map<string, Item>();
  const taskIds = new Set<string>();
  for (const item of items) {
    if (item.type === "function_call_output") outputs.set(String(data(item).call_id), item);
    const entry = data(item).entry_call_id;
    if (item.type !== "agent_handoff" || typeof entry !== "string") continue;
    if (handoffKind(item) === "task") taskIds.add(entry);
    if (handoffKind(item) === "task_return") taskEnds.set(entry, item);
  }
  /* The row LiveKit writes when the entry agent takes the session repeats the
     name already in the page header. Worth drawing only on a call that really
     changes agents, where "which one was on at this point" is a question. */
  const showEntry = items.some(isRealHandoff);
  const lastCallerAt = Math.max(
    ...items.filter((i) => i.role === "user").map((i) => Date.parse(i.created_at)),
  );

  /* One stream, in time order. Both clocks are the worker's, so a stable merge
     is exact: an event lands in front of the first item produced after it. */
  const inline = inlineEvents(events);
  const checkIns = events.filter((e) => e.type === "caller.check_in");
  const handoffContexts = events.filter((e) => e.type === "agent.handoff");
  type Mark =
    | { at: string; row: Extract<Beat, { kind: "event" }> }
    | { at: string; checkIn: TraceEvent }
    | { at: string; handoff: TraceEvent };
  const marks: Mark[] = [
    ...inline.map((beat) => ({ at: beat.at, row: beat })),
    ...checkIns.map((e) => ({ at: e.created_at, checkIn: e })),
    ...handoffContexts.map((e) => ({ at: e.created_at, handoff: e })),
  ].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));

  const exchanges: Exchange[] = [];
  let current: Exchange | null = null;
  let turn = turnOffset;
  let pendingCheckIn: string | null = null;
  let pendingContext: string | null = null;
  const open = (at: string, label: string, numbered: boolean, started: number | null) => {
    if (numbered) turn += 1;
    current = {
      id: `${idPrefix}${numbered ? `turn-${turn}` : `exchange-${exchanges.length + 1}`}`,
      label: numbered ? `Turn ${turn}` : label,
      turn: numbered ? turn : null,
      at,
      startedAt: started,
      beats: [],
      response: null,
    };
    exchanges.push(current);
    return current;
  };
  /* The first thing to happen is the greeting; anything unprompted after that
     has no better name unless a check-in said why. */
  const ambient = (at: string, started: number | null) =>
    current ?? open(at, exchanges.length === 0 ? "Greeting" : "Agent", false, started);

  let m = 0;
  const drainMarks = (before: number) => {
    for (; m < marks.length && Date.parse(marks[m].at) < before; m += 1) {
      const mark = marks[m];
      if ("row" in mark) ambient(mark.at, null).beats.push(mark.row);
      else if ("checkIn" in mark) {
        open(mark.at, "Check-in", false, null);
        pendingCheckIn = `Check-in ${mark.checkIn.payload.attempt} of ${mark.checkIn.payload.of}`;
      } else {
        pendingContext = eventSummary(mark.handoff.type, mark.handoff.payload);
      }
    }
  };

  const tasks = new Map<string, TimelineTask>();
  let lastModel: unknown = null;
  for (const item of items) {
    if (item.type === "function_call_output" || item.type === "agent_config_update") continue;
    drainMarks(Date.parse(item.created_at));
    const callId = String(data(item).call_id ?? "");

    if (item.type === "message" && item.role === "user") {
      const opened = open(item.created_at, "", true, startedAt(item));
      opened.beats.push({ kind: "caller", id: `beat-${item.id}`, item });
      /* On text, the message that opened a turn carries how the turn ended.
         Said here, under the message, because a failure with no reply is
         otherwise a customer line followed by nothing. */
      if (item.turn_status === "error") {
        opened.beats.push({
          kind: "note",
          id: `note-${item.id}`,
          tone: "danger",
          text: `The agent did not reply: ${item.turn_error ?? "the turn failed"}`,
        });
      } else if (item.turn_status === "canceled") {
        opened.beats.push({
          kind: "note",
          id: `note-${item.id}`,
          tone: "neutral",
          text: "Superseded by the next message",
        });
      }
      continue;
    }
    // The call that entered a task is the task's own header, not a tool card.
    if (item.type === "function_call" && taskIds.has(callId)) continue;
    if (item.type === "agent_handoff" && data(item).old_agent_id == null && !showEntry) continue;

    const exchange = ambient(item.created_at, startedAt(item));
    if (item.type === "message" && item.role === "assistant") {
      exchange.startedAt ??= startedAt(item);
      const first = item.latency && item.latency.kind !== "after_filler" ? item.latency : null;
      if (first) exchange.response ??= first;
      exchange.beats.push({
        kind: "agent",
        id: `beat-${item.id}`,
        item,
        answers: first
          ? (exchange.beats.findLast((b) => b.kind === "caller")?.item ?? null)
          : null,
        checkIn: pendingCheckIn,
        falseInterruption: null,
        newModel: item.metrics?.llm_metadata?.model_name !== lastModel,
      });
      lastModel = item.metrics?.llm_metadata?.model_name;
      pendingCheckIn = null;
    } else if (item.type === "message") {
      exchange.beats.push({ kind: "told", id: `beat-${item.id}`, item });
    } else if (item.type === "function_call") {
      const output = outputs.get(callId) ?? null;
      const trace = traces.get(callId) ?? null;
      exchange.beats.push({
        kind: "tool",
        id: `beat-${item.id}`,
        call: item,
        output,
        trace,
        verdict: toolVerdict(output, trace),
        // A caller who spoke again after this is what cut it off; with nobody
        // speaking after it, the call simply moved on or ended.
        cancelledBy:
          !trace?.background && Date.parse(item.created_at) < lastCallerAt ? "caller" : "call",
      });
    } else if (item.type === "agent_handoff") {
      const entry = data(item).entry_call_id;
      if (handoffKind(item) === "task" && typeof entry === "string") {
        const task: TimelineTask = {
          id: entry,
          name: (item.text ?? "").replace(/^Started /, ""),
          start: item,
          end: taskEnds.get(entry) ?? null,
          output: outputs.get(entry) ?? null,
          trace: traces.get(entry) ?? null,
          messages: 0,
        };
        tasks.set(entry, task);
        exchange.beats.push({ kind: "task_start", id: `beat-${item.id}`, task });
      } else if (handoffKind(item) === "task_return" && typeof entry === "string" && tasks.has(entry)) {
        exchange.beats.push({ kind: "task_end", id: `beat-${item.id}`, task: tasks.get(entry)! });
      } else {
        exchange.beats.push({ kind: "handoff", id: `beat-${item.id}`, item, context: pendingContext });
        pendingContext = null;
      }
    }
  }
  drainMarks(Number.POSITIVE_INFINITY);

  /* A false interruption belongs to the speech it cut into, which was added to
     the transcript when it BEGAN — so it is matched by speech window, not by
     position. With no window to match it is said as a row of its own. */
  const agentBeats = exchanges.flatMap((x) => x.beats).filter((b) => b.kind === "agent");
  for (const event of events) {
    if (event.type !== "agent.false_interruption") continue;
    const at = Date.parse(event.created_at) / 1000;
    const spoken = agentBeats.findLast((b) => {
      const from = b.item.metrics?.started_speaking_at;
      const until = b.item.metrics?.stopped_speaking_at;
      return (
        typeof from === "number" &&
        typeof until === "number" &&
        at >= from &&
        at <= until + falseInterruptionGraceS
      );
    });
    if (spoken) {
      spoken.falseInterruption = { resumed: event.payload.resumed === true };
      continue;
    }
    const home = exchanges.findLast((x) => Date.parse(x.at) <= Date.parse(event.created_at));
    home?.beats.push({
      kind: "event",
      id: eventId(event),
      at: event.created_at,
      tone: "warn",
      text: eventSummary(event.type, event.payload)!,
      event,
    });
  }

  return { exchanges, blocks: toBlocks(exchanges) };
}

/** Nest the flat beats into brackets: a task holds everything between its two
 *  rows, across however many exchanges that took. */
function toBlocks(exchanges: Exchange[]): Block[] {
  const root: Block[] = [];
  const stack: { task: TimelineTask; blocks: Block[] }[] = [];
  const into = () => (stack.length ? stack[stack.length - 1].blocks : root);
  for (const exchange of exchanges) {
    let segment: Extract<Block, { kind: "segment" }> | null = null;
    let opened = false;
    const add = (beat: Beat) => {
      if (!segment) {
        segment = { kind: "segment", exchange, beats: [], opens: !opened };
        opened = true;
        into().push(segment);
      }
      segment.beats.push(beat);
    };
    for (const beat of exchange.beats) {
      if (beat.kind === "task_start") {
        /* A task that ended without handing back (cancelled with its session's
           window) wrote no return row. It is over all the same, and what comes
           next did not happen inside it. */
        while (stack.length && !stack[stack.length - 1].task.end && stack[stack.length - 1].task.trace) {
          stack.pop();
        }
        const node = { kind: "task" as const, task: beat.task, blocks: [] as Block[] };
        into().push(node);
        stack.push(node);
        segment = null;
      } else if (beat.kind === "task_end") {
        // Tasks nest and return in order, so the one ending is the innermost.
        stack.pop();
        segment = null;
      } else {
        if (stack.length && (beat.kind === "caller" || beat.kind === "agent")) {
          stack[stack.length - 1].task.messages += 1;
        }
        add(beat);
      }
    }
  }
  return root;
}

/** Whether a tool the caller actually waited on was slow. A background tool is
 *  never slow, whatever it reads: it gave the turn back at dispatch, so its
 *  duration is the work's and nobody sat through it. */
export const slowTool = (beat: ToolBeat, slowToolMs: number) =>
  !beat.trace?.background && (beat.trace?.durationMs ?? 0) > slowToolMs;

/** Whether something in this exchange went wrong, or went slowly. */
export function hasIssue(exchange: Exchange, slowMs: number, slowToolMs: number): boolean {
  if ((exchange.response?.total_ms ?? 0) > slowMs) return true;
  return exchange.beats.some((beat) => {
    if (beat.kind === "tool") {
      /* Failed and unfinished count for a background tool too — one that broke
         is exactly what this filter is for. */
      return beat.verdict === "failed" || beat.verdict === "unfinished" || slowTool(beat, slowToolMs);
    }
    if (beat.kind === "agent") {
      return (
        Boolean(data(beat.item).interrupted) ||
        beat.item.delivery_status === "failed" ||
        beat.falseInterruption !== null ||
        (beat.item.latency?.kind === "after_filler" && beat.item.latency.total_ms > slowMs)
      );
    }
    if (beat.kind === "event") {
      return beat.event.type === "session.error" || beat.event.type === "tool.step_limit_reached";
    }
    if (beat.kind === "note") return beat.tone === "danger";
    return false;
  });
}

/** The transcript as text, telling the same story the page does. `who` is what
 *  the person is on this surface: a caller, or a customer writing in. */
export function plainText(exchanges: Exchange[], who = "Caller"): string {
  const line = (beat: Beat): string | null => {
    switch (beat.kind) {
      case "caller":
        return `${who}: ${beat.item.text ?? ""}`;
      case "agent":
        return `Agent: ${beat.item.text ?? ""}`;
      case "told":
        return `System: ${beat.item.text ?? ""}`;
      case "tool":
        return `Tool ${String(data(beat.call).name ?? "")}(${String(data(beat.call).arguments ?? "")})`;
      case "handoff":
        return `[${beat.item.text ?? "Agent handoff"}]`;
      case "task_start":
        return `[Task: ${beat.task.name}]`;
      case "task_end":
        return `[${beat.task.end?.text ?? "Back"}]`;
      case "event":
      case "note":
        return `[${beat.text}]`;
    }
  };
  return exchanges
    .map((x) => x.beats.map(line).filter(Boolean).join("\n"))
    .join("\n\n");
}

export type ExchangeFilter = "all" | "tools" | "issues";

/** Whether an exchange survives the toolbar's search and filter. `needle` is
 *  already lower-cased. */
export function exchangeMatches(
  exchange: Exchange,
  needle: string,
  filter: ExchangeFilter,
  slowMs: number,
  slowToolMs: number,
): boolean {
  if (needle && !plainText([exchange]).toLowerCase().includes(needle)) return false;
  if (filter === "tools") {
    return exchange.beats.some((b) => b.kind === "tool" || b.kind === "task_start");
  }
  if (filter === "issues") return hasIssue(exchange, slowMs, slowToolMs);
  return true;
}

/** The slowest exchange, when it was slow enough to be worth pointing at. */
export function slowestExchange(exchanges: Exchange[], slowMs: number): string | null {
  const slowest = exchanges.reduce<Exchange | null>(
    (worst, x) => ((x.response?.total_ms ?? 0) > (worst?.response?.total_ms ?? 0) ? x : worst),
    null,
  );
  return (slowest?.response?.total_ms ?? 0) > slowMs ? slowest!.id : null;
}

type IssueKind = import("@talqing/sdk").HealthIssue["kind"];

/** Where each kind of issue first shows in a transcript. */
export type IssueTargets = Partial<Record<IssueKind, string>>;

/**
 * Where each kind of health issue first shows in the transcript.
 *
 * The band says a call had "2 tool calls failed"; this is what lets it take the
 * reader to the first one. Kinds with no moment of their own — the call could
 * not be priced, noise cancellation failed — have no entry.
 */
export function issueTargets(
  exchanges: Exchange[],
  slowMs: number,
  slowToolMs: number,
): IssueTargets {
  const targets: IssueTargets = {};
  const mark = (kind: IssueKind, id: string) => (targets[kind] ??= id);
  for (const exchange of exchanges) {
    if ((exchange.response?.total_ms ?? 0) > slowMs) mark("slow_responses", exchange.id);
    for (const beat of exchange.beats) {
      if (beat.kind === "tool") {
        if (beat.verdict === "failed") mark("tool_failures", beat.id);
        if (beat.verdict === "unfinished") mark("tools_unfinished", beat.id);
        if (slowTool(beat, slowToolMs)) mark("slow_tools", beat.id);
      } else if (beat.kind === "agent") {
        const wait = beat.item.latency;
        if (wait?.kind === "after_filler" && wait.total_ms > slowMs) mark("slow_after_filler", beat.id);
      } else if (beat.kind === "event") {
        if (beat.event.type === "session.error") mark("provider_errors", beat.id);
        if (beat.event.type === "provider.failed") mark("provider_failover", beat.id);
        if (beat.event.type === "telemetry.rtc.quality") mark("connection", beat.id);
      }
    }
  }
  return targets;
}
