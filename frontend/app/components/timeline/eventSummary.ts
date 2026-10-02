/* One readable sentence for the event types whose payload a person would
   otherwise have to decode. Everything else keeps the raw JSON below, which is
   the right default for a trace — this is only for the handful whose whole point
   is to explain something the reader is already puzzled by. */
export function eventSummary(type: string, payload: Record<string, unknown>): string | null {
  if (type === "caller.check_in") {
    return `The caller went quiet, so the agent checked they were still there (${payload.attempt} of ${payload.of})`;
  }
  if (type === "hold.started") {
    return "The caller was put on hold music while the transfer dialled";
  }
  if (type === "hold.ended") {
    const seconds =
      typeof payload.duration_ms === "number" ? ` after ${(payload.duration_ms / 1000).toFixed(1)}s` : "";
    return payload.returned_to_agent
      ? `Hold ended${seconds} and the caller went back to the agent`
      : `Hold ended${seconds} — the caller was connected to a person`;
  }
  if (type === "screenshare.started") {
    const size =
      typeof payload.width === "number" && typeof payload.height === "number"
        ? ` (${payload.width}\u00d7${payload.height})`
        : "";
    return `The caller started sharing their screen${size} — the agent sees it from their next turn`;
  }
  if (type === "screenshare.stopped") {
    const seconds =
      typeof payload.duration_ms === "number"
        ? ` after ${(payload.duration_ms / 1000).toFixed(1)}s`
        : "";
    return `The caller stopped sharing their screen${seconds} — the agent is told it can no longer see`;
  }
  if (type === "recording.consent_withdrawn") {
    return "The caller asked not to be recorded, and the audio so far was discarded";
  }
  /* The three that explain a stretch of the audio, which is exactly the kind of
     thing a reader is already puzzled by when they come here: they have just
     played the recording and met silence, or found no recording at all. */
  if (type === "recording.paused") {
    return `Recording paused — ${payload.agent_name || "this agent"} has recording turned off. The rest of its turn is silence in the file, so later turns still line up.`;
  }
  if (type === "recording.resumed") {
    return `Recording resumed — the call went back to ${payload.agent_name || "an agent"}, which records`;
  }
  if (type === "recording.unavailable") {
    return `${payload.agent_name || "This agent"} has recording on, but nothing is recording this call — the agent that answered had it off, and recording cannot start part-way through`;
  }
  if (type === "noise_cancellation.failed") {
    return "The audio enhancer disabled itself; the call ran on raw audio";
  }
  if (type === "agent.false_interruption") {
    return payload.resumed
      ? "The agent stopped for something that was not speech, then carried on by itself"
      : "The agent stopped for something that was not speech and did not resume";
  }
  /* Reads as an ordinary turn everywhere else — the reply was spoken, the tools
     that did run are in the transcript. This is the only line that says the
     answer was cut short rather than finished. */
  if (type === "tool.step_limit_reached") {
    const steps = typeof payload.steps === "number" ? payload.steps : null;
    const rounds = steps === null ? "too many rounds" : `${steps} rounds`;
    return typeof payload.limit === "number"
      ? `The agent still wanted tools after ${rounds} of them in one turn, and was made to answer without any. What it said next came from what it already had.`
      : `The agent chained ${rounds} of tool calls in one turn. A realtime call has no ceiling, so nothing stopped it.`;
  }
  if (type === "session.error") {
    const source = payload.source as { provider?: string; model?: string } | undefined;
    const where = source?.provider ? `${source.provider}/${source.model ?? "?"}` : "a provider";
    /* The whole point of the event. LiveKit's own dump excludes the exception,
       so `cause` is the worker reading it off the object by hand — without it
       this line was "Raya/m1 raised an error" and nothing else, on the only
       record a provider failure leaves. Absent on calls that ran before the
       worker started capturing it. */
    const cause = payload.cause as
      | { message?: string; status_code?: number; type?: string }
      | undefined;
    const said = cause?.message || cause?.type;
    const status = cause?.status_code ? ` [${cause.status_code}]` : "";
    /* Recoverable says the call carried on, which is the first thing a reader
       needs — a retried TTS chunk and a dead LLM are not the same finding. */
    const outcome = payload.recoverable ? " The call recovered." : " The call did not recover.";
    return said
      ? `${where}${status}: ${said}.${outcome}`
      : `${where} raised an error, with no detail recorded.${outcome}`;
  }
  /* The sibling of `conversation.context_loaded` below, and the same question:
     what did this start with? The summary text itself stays in the raw payload —
     it can be a paragraph, and the trace is a list. */
  if (type === "agent.handoff") {
    const to = String(payload.to_name ?? "the next agent");
    const turns = Number(payload.recent_turns ?? 0);
    const items = Number(payload.tail_items ?? 0);
    const tail = `the last ${turns === 1 ? "turn" : `${turns} turns`} (${items} message${items === 1 ? "" : "s"})`;
    /* The failure a reader came here to find: the agent was asked for a summary
       and sent an empty string, so the target got everything instead. */
    if (payload.summary_fallback) {
      return `Handed to ${to} — the agent sent an empty summary, so ${to} was given the full transcript instead`;
    }
    if (payload.context === "summary") return `Handed to ${to} — a summary and ${tail}`;
    if (payload.context === "none") {
      return items
        ? `Handed to ${to} — ${tail} and nothing before them`
        : `Handed to ${to} — nothing about the conversation so far`;
    }
    return `Handed to ${to} — the whole conversation so far`;
  }
  /* The only answer to "why does this streamed caller never get their history
     back?": the platform sent something that is not a phone number — usually a
     placeholder nobody filled in — and it was ignored rather than used as a key. */
  if (type === "stream.connected") {
    // Stored as JSONB, which reorders keys; sorted so `from` reads before `to`.
    const unusable = Object.entries((payload.unusable_numbers ?? {}) as Record<string, string>).sort(
      ([a], [b]) => a.localeCompare(b),
    );
    if (!unusable.length) return null;
    const sent = `The platform sent ${unusable.map(([field, value]) => `${field} "${value}"`).join(" and ")}, ${unusable.length === 1 ? "which is not a phone number" : "which are not phone numbers"}`;
    const callerField = payload.direction === "outbound" ? "to" : "from";
    return unusable.some(([field]) => field === callerField)
      ? `${sent} — the caller could not be recognised, so this call had no earlier calls or saved details`
      : `${sent} — the business number is unknown on this call`;
  }
  if (type !== "conversation.context_loaded") return null;
  const withUserdata = payload.userdata_initialized
    ? ", and this caller's saved details"
    : "";
  if (payload.context === "none") {
    return "Started clean — no context from earlier calls";
  }
  if (payload.context === "summary") {
    const count = Number(payload.summaries ?? 0);
    // The line that explains a config which looks like it should have worked.
    if (!count) return "Started clean — no earlier calls had summaries";
    return `Started with summaries of ${count} earlier call${count === 1 ? "" : "s"}${withUserdata}`;
  }
  const items = Number(payload.history_items ?? 0);
  if (!items) return `Started a new conversation with this caller${withUserdata}`;
  return `Continued an earlier conversation — ${items} earlier message${items === 1 ? "" : "s"} in context${withUserdata}`;
}
