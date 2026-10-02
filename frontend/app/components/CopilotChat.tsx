"use client";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CopilotItem, PublicMessage, CopilotFunctionCallItem, CopilotFunctionCallOutputItem, CopilotTextItem } from "@talqing/sdk";
import { api, subscribe, talqing } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { CopilotProse } from "./CopilotProse";

/* The CoPilot chat, in whichever editor it is mounted: AgentCoPilot in the agent
   editor rail, ToolCoPilot in the tool editor sidepane, TaskCoPilot in the task
   editor rail. SSE snapshot + live
   message/turn events. Turns run on the shared text-worker. The conversation is
   keyed by the id of what the CoPilot edits. Any live event reloads that draft
   (no dedicated "changed" event). */

type Subject = "agent" | "tool" | "task";

/* What `subscribe` hands an opener so aborting the subscription aborts the
   request under it. */
type SseInit = { signal: AbortSignal };

/* `greeting` is spoken by the CoPilot, not written about it — it renders as an
   ordinary assistant message, so the panel never switches out of chat and the
   first thing you send simply continues the conversation. It says what the
   CoPilot can reach and what it cannot do without you, which is what someone
   opening an empty panel actually needs to know. */
const SUBJECTS = {
  agent: {
    who: "Agent CoPilot",
    /* Called, never passed: every SDK method reads `this.client`, so handing the
       bare method to `subscribe` would drop the receiver and throw before the
       request is made. */
    stream: (id: string, init: SseInit) =>
      talqing.copilot.agents.stream({ agent_id: id }, init),
    send: (id: string, text: string) =>
      talqing.copilot.agents.send({ agent_id: id, sendMessageRequest: { text } }),
    greeting:
      "Describe the agent you want and I’ll build it — the prompt, the voice, the models, the tools it can call. Everything I change stays a draft until you publish it.",
  },
  tool: {
    who: "Tool CoPilot",
    stream: (id: string, init: SseInit) =>
      talqing.copilot.tools.stream({ tool_id: id }, init),
    send: (id: string, text: string) =>
      talqing.copilot.tools.send({ tool_id: id, sendMessageRequest: { text } }),
    greeting:
      "Tell me what this tool should do and I’ll build it — the arguments the model fills in, and the operations that run when it’s called. Everything I change stays a draft until you publish it.",
  },
  task: {
    who: "Task CoPilot",
    stream: (id: string, init: SseInit) =>
      talqing.copilot.tasks.stream({ task_id: id }, init),
    send: (id: string, text: string) =>
      talqing.copilot.tasks.send({ task_id: id, sendMessageRequest: { text } }),
    greeting:
      "Tell me what this task should work out and I’ll build it — the prompt, the values it takes in, and the fields it has to produce. Everything I change stays a draft until you publish it.",
  },
} as const satisfies Record<Subject, unknown>;

type Action = {
  name: string;
  preview: string;
  status: "ok" | "err" | "pending";
  detail: string;
  href: string | null;
};

type Item =
  | { type: "user"; text: string }
  | { type: "agent"; text: string }
  /* Consecutive calls are one block, not one row each. A single instruction
     routinely costs three or four operations, and as separate cards they push
     the sentence explaining them off the bottom of a rail this narrow. */
  | { type: "actions"; actions: Action[] };

/* A CoPilot has the dashboard's whole API, so what it calls ranges from reading
   a tool to republishing an agent. Those must not look alike: one is it looking
   something up, the other changed the draft under you. The leading verb is the
   tell for most names in `api/dataplane/functions.py`, which is the same list
   the MCP server exposes — why the row keeps the real name rather than
   prettifying it into something you could not then search for. READ_NAMES is
   every read whose name does not lead with a verb, Exa's web tools included; a
   new one belongs there too, or it shows as an edit. */
const READ_VERBS = ["get", "list", "validate", "describe", "search"];
const READ_NAMES = [
  "call_stats",
  "catalog_avatars",
  "catalog_voices",
  "elevenlabs_voice_settings",
  "event_types",
  "integration_catalog",
  "webhook_deliveries",
  "web_search_exa",
  "web_fetch_exa",
];

function isRead(name: string): boolean {
  return READ_VERBS.includes(name.split("_")[0]) || READ_NAMES.includes(name);
}

function AiMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden className={className}>
      <path d="M12 2.5l1.9 4.9 4.9 1.9-4.9 1.9L12 16l-1.9-4.8-4.9-1.9 4.9-1.9L12 2.5Z" fill="currentColor" />
      <path d="M18.5 14.5l.8 2.1 2.1.8-2.1.8-.8 2.1-.8-2.1-2.1-.8 2.1-.8.8-2.1Z" fill="currentColor" opacity="0.7" />
    </svg>
  );
}

/** The display name, for a surface that titles the CoPilot itself — the tool
 *  editor puts it on a tab rather than in a heading. */
export function copilotName(subject: Subject): string {
  return SUBJECTS[subject].who;
}

/* The panel heading. Lives here rather than in each editor because the name and
   the mark are the CoPilot's identity, and three pages spelling them out by hand
   is exactly how "Tool CoPilot" and "AgentCoPilot" ended up in the same product.

   No tagline beside it. Every one of these panels opens with the CoPilot
   itself saying what it does, so a second, shorter version of the same claim
   next to the title is a line of the rail spent saying it twice. */
export function CopilotHeading({ subject }: { subject: Subject }) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className="grid h-[22px] w-[22px] flex-none place-items-center rounded-md bg-ink text-white">
        <AiMark className="h-3.5 w-3.5" />
      </span>
      {/* The same heading its sibling card in the rail wears. */}
      <h2 className="text-[16px] font-semibold leading-6 text-ink">
        {SUBJECTS[subject].who}
      </h2>
    </div>
  );
}

/* A CoPilot calls the same API operations the dashboard does, so a tool call's
   arguments are path/query parameters plus a `body` — show whichever readable
   label is in there. Its web tools take a `query` or a list of `urls` instead. */
function argPreview(argsJson: string): string {
  try {
    const a = JSON.parse(argsJson || "{}");
    if (Array.isArray(a.urls) && typeof a.urls[0] === "string") return a.urls[0].slice(0, 36);
    for (const source of [a, a.body?.config, a.body]) {
      if (!source || typeof source !== "object") continue;
      for (const k of ["name", "display_name", "channel", "provider", "url", "query"]) {
        if (typeof source[k] === "string" && source[k]) return source[k].slice(0, 36);
      }
    }
  } catch {}
  return "";
}

/* A failed operation returns the API's own error body, {detail: {message, errors}}. */
function errorDetail(result: any): string {
  const detail = result?.detail;
  if (!detail) return "";
  const errors: string[] = Array.isArray(detail.errors) ? detail.errors : [];
  return [detail.message, ...errors].filter(Boolean).join(" — ");
}

/* `item` is a discriminated union on the wire, so these are `item.type === …`
   rather than the shape-sniffing they used to be. Kept as named predicates
   because they read better at the call sites than the comparison does. */
const isTextItem = (item: CopilotItem): item is CopilotTextItem => item.type === "message";

const isFunctionCall = (item: CopilotItem): item is CopilotFunctionCallItem =>
  item.type === "function_call";

const isFunctionCallOutput = (item: CopilotItem): item is CopilotFunctionCallOutputItem =>
  item.type === "function_call_output";

function textContent(item: CopilotTextItem): string {
  return typeof item.content === "string" ? item.content : "";
}

function itemize(
  rows: PublicMessage[],
  subject: Subject,
  subjectId: string,
  subjectName: string,
): Item[] {
  const results = new Map<string, any>();
  for (const r of rows) {
    if (isFunctionCallOutput(r.item)) {
      try { results.set(r.item.call_id, JSON.parse(r.item.output || "{}")); }
      catch { results.set(r.item.call_id, {}); }
    }
  }
  const out: Item[] = [];
  for (const r of rows) {
    if (isTextItem(r.item)) {
      const text = textContent(r.item);
      if (r.item.role === "user") out.push({ type: "user", text });
      else if (r.item.role === "assistant" && text) out.push({ type: "agent", text });
      continue;
    }
    if (isFunctionCall(r.item)) {
      const res = results.get(r.item.call_id);
      let status: "ok" | "err" | "pending" = "pending";
      let detail = "";
      let href: string | null = null;
      if (res !== undefined) {
        detail = errorDetail(res);
        status = detail ? "err" : "ok";
        // Link to whatever the operation returned: agent responses carry the
        // agent id as `id`, publish/validate responses name it explicitly.
        // Never link to what this CoPilot edits — that is the page you are on.
        const aid = res?.agent_id ?? (r.item.name.endsWith("_agent") ? res?.id : undefined);
        const tid = res?.tool_id ?? (r.item.name.endsWith("_tool") ? res?.id : undefined);
        if (aid && !(subject === "agent" && aid === subjectId)) href = `/agents/detail?id=${aid}`;
        else if (tid && !(subject === "tool" && tid === subjectId)) href = `/tools/detail?id=${tid}`;
      }
      // The preview names what the operation acted on, which is worth a glance
      // when the CoPilot reaches for something else and nothing at all when it
      // is the draft you are looking at — the same reason `href` never links
      // here. Four rows reading "Meera — night pharmacy line" on Meera's own
      // page is the page title, repeated.
      const preview = argPreview(r.item.arguments);
      const action: Action = {
        name: r.item.name,
        preview: preview.trim().toLowerCase() === subjectName.trim().toLowerCase() ? "" : preview,
        status,
        detail,
        href,
      };
      const last = out[out.length - 1];
      if (last?.type === "actions") last.actions.push(action);
      else out.push({ type: "actions", actions: [action] });
    }
  }
  return out;
}

/* The two voices, told apart the way a chat tells them apart: your words in a
   soft fill on the right, the CoPilot's in a white card on the left. Both are
   shaped like messages, because both are — the reply is not page furniture. */
function Said({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "self-end max-w-[90%] whitespace-pre-wrap rounded-2xl rounded-br-lg border border-line-2 bg-subtle px-3.5 py-2.5 text-[14px] leading-[1.5] text-ink",
        className,
      )}
    >
      {children}
    </div>
  );
}

/* A white card on the same hairline the operation cards wear, so everything the
   CoPilot puts on screen — what it says and what it did — reads as one family
   against your own messages. Wider than yours because it carries the long answer,
   and a narrow column of wrapped list items is hard to read. */
function Replied({ children }: { children: React.ReactNode }) {
  return (
    <div className="self-start max-w-[95%] rounded-2xl rounded-bl-lg border border-line-2 bg-white px-3.5 py-2.5">
      {children}
    </div>
  );
}

/* Every call it made, shown. This is the receipt for edits to your own agent —
   folding it behind "2 edits" would ask you to trust the summary of the thing
   you opened the panel to watch. What it gets instead is a home: one card per
   run, so a turn's worth of operations reads as a block of work rather than as
   loose console output between paragraphs.

   The names stay as `update_agent`, not "Updated the agent" — these are the
   operations `api/operations.py` exposes and the MCP server serves, so this is
   a name you can go and look up. */
function Edits({ actions }: { actions: Action[] }) {
  return (
    /* `flex-none` is load-bearing: the feed is a flex column, `overflow-hidden`
       gives this card an automatic minimum size of zero, and the default
       flex-shrink then crushes it to its own two borders once the transcript
       is taller than the panel. Everything else here is protected by the
       min-content height of its own text. */
    <ol className="self-start w-full flex-none divide-y divide-line overflow-hidden rounded-xl border border-line-2 bg-white">
      {actions.map((a, i) => {
        const row = (
          <>
            <Glyph status={a.status} read={isRead(a.name)} />
            <span
              className={cn(
                "font-mono text-[12px] leading-4",
                a.status === "err" ? "text-danger" : isRead(a.name) ? "text-muted" : "text-ink-soft",
              )}
            >
              {a.name}
            </span>
            {a.preview && (
              <span className="min-w-0 truncate text-[12px] leading-4 text-faint">{a.preview}</span>
            )}
          </>
        );
        return (
          <li key={i} className="px-2.5 py-1.5">
            {a.href ? (
              <a href={a.href} className="flex items-center gap-2 no-underline hover:underline">{row}</a>
            ) : (
              <span className="flex items-center gap-2">{row}</span>
            )}
            {/* The API's own message, in full — the only account of why your
                change did not happen, so never a `title` you have to hunt for. */}
            {a.status === "err" && a.detail && (
              <p className="mt-1 pl-[22px] text-[12px] leading-[1.45] text-danger">{a.detail}</p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/* Reads and writes must not look alike: one is the CoPilot looking something
   up, the other changed the draft under you. A filled tick is a write that
   landed, a hollow ring is a read. */
function Glyph({ status, read }: { status: Action["status"]; read: boolean }) {
  if (status === "pending") {
    return <span aria-hidden className="h-3.5 w-3.5 flex-none animate-pulse rounded-full bg-line-strong" />;
  }
  if (status === "err") {
    return (
      <svg viewBox="0 0 14 14" aria-hidden className="h-3.5 w-3.5 flex-none text-danger">
        <circle cx="7" cy="7" r="6" fill="currentColor" />
        <path d="M4.8 4.8l4.4 4.4M9.2 4.8l-4.4 4.4" stroke="white" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 14 14" aria-hidden className={cn("h-3.5 w-3.5 flex-none", read ? "text-line-strong" : "text-ink")}>
      <circle cx="7" cy="7" r="6" fill={read ? "none" : "currentColor"} stroke="currentColor" strokeWidth="1.25" />
      {!read && <path d="M4.5 7.2l1.8 1.8 3.2-3.6" stroke="white" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />}
    </svg>
  );
}

/* A turn that died says so in the transcript, at the point it died — a card in
   the run of work, not a banner parked at the bottom of the panel, because what
   stopped is the block of operations directly above it and reading the two
   together is the whole explanation.

   The provider's own sentence, verbatim: "Rate limit reached … try again in
   9.312s" tells you both what happened and what to do, and no phrasing of ours
   improves on it. It arrives on the `turn` SSE frame and again in the snapshot,
   so it is still here after a reload. */
function Stopped({ who, text }: { who: string; text: string }) {
  return (
    /* Same border/fill as the panel's own error banner: one danger treatment in
       this rail, not a second, quieter dialect of the same idea. `flex-none`
       for the reason `Edits` documents — the feed crushes anything shrinkable. */
    <div className="self-start w-full flex-none rounded-xl border border-danger/30 bg-danger/10 px-2.5 py-2">
      <p className="text-[12px] font-medium leading-4 text-danger">{who} stopped here</p>
      <p className="mt-1 break-words text-[12.5px] leading-[1.5] text-danger">{text}</p>
    </div>
  );
}

/* The three dots dance rather than blink: each one lifts and brightens in turn,
   so the motion travels left to right the way a typing indicator does. Three
   dots sharing one `animate-pulse` fade in unison, which reads as an element
   that has stalled rather than as someone composing a reply.

   The stagger is a third of the cycle, so the wave is evenly spaced and the
   rest between waves is long enough that it does not read as a jitter.

   `motion-reduce` stops the movement and leaves the dots at rest — visible,
   still, and no longer claiming anything by moving. The `role="status"` label
   is what actually reports progress to a screen reader either way. */
function Working({ who }: { who: string }) {
  return (
    <div className="self-start flex items-center gap-1 py-1.5" role="status" aria-label={`${who} is working`}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          aria-hidden
          className="h-[5px] w-[5px] animate-typing-dot rounded-full bg-muted motion-reduce:animate-none motion-reduce:opacity-40"
          style={{ animationDelay: `${i * 0.383}s` }}
        />
      ))}
    </div>
  );
}

export default function CopilotChat({
  subject,
  subjectId,
  subjectName,
  onChanged,
}: {
  /** Which CoPilot this is — decides its name, its endpoints and its copy. */
  subject: Subject;
  /** The id of what it edits: an agent id, or a tool id. */
  subjectId: string;
  /** Its current name, so a row that just repeats the page title can drop it. */
  subjectName: string;
  onChanged?: () => void;
}) {
  const copilot = SUBJECTS[subject];
  const [rows, setRows] = useState<PublicMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingMsg, setPendingMsg] = useState<string | null>(null);
  /* Two different failures, two different states. `err` is the transport — the
     stream dropped, the send did not go through — and belongs to the panel.
     `turnErr` is the CoPilot's own work stopping, and belongs to the
     transcript, where it survives the reconnect that would clear the other. */
  const [turnErr, setTurnErr] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const feedRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const busyRef = useRef(false);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  busyRef.current = busy || sending;

  // one SDK-owned SSE connection per mounted editor; reconnect and a switch to
  // another subject both re-snapshot from the server.
  useEffect(() => {
    setRows([]); setLoaded(false); setBusy(false); setPendingMsg(null); setTurnErr(null); setErr("");

    // Every CoPilot rail streams the same three events; only the resource
    // differs, so the opener is what the subject table varies.
    const stop = subscribe(
      (init) => SUBJECTS[subject].stream(subjectId, init),
      (event) => {
        switch (event.event) {
          case "snapshot":
            setRows(event.messages);
            setBusy(event.busy);
            if (!event.busy) setPendingMsg(null);
            setTurnErr(event.error ?? null);
            setLoaded(true);
            setErr("");
            break;
          case "message":
            // dedupe by id — snapshot + a live event can both carry the same row
            setRows((prev) => (prev.some((r) => r.id === event.id) ? prev : [...prev, event]));
            if (isTextItem(event.item) && event.item.role === "user") {
              const text = textContent(event.item);
              setPendingMsg((p) => (p === text ? null : p));
            }
            // Reload draft on every timeline event (tools mutate agent config).
            onChangedRef.current?.();
            break;
          case "turn": {
            const running = event.status === "running";
            setBusy(running);
            if (!running) setPendingMsg(null);
            // canceled/done/error all clear busy so a superseding turn can start
            // cleanly. Only `error` leaves something behind: every other status
            // means this turn is no longer the one whose failure the rail reports.
            setTurnErr(
              event.status === "error"
                ? event.error || "The build stopped and gave no reason."
                : null,
            );
            onChangedRef.current?.();
            break;
          }
          default:
            break;
        }
      },
    );

    return () => stop();
  }, [subject, subjectId]);

  const items = useMemo(
    () => itemize(rows, subject, subjectId, subjectName),
    [rows, subject, subjectId, subjectName],
  );

  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length, busy, pendingMsg, turnErr]);

  // A CoPilot instruction is a sentence or two, not a search query, and typing
  // one into a fixed single line means writing it blind past the first row.
  // Measured rather than counted: `scrollHeight` after a reset to `auto` is the
  // only number that knows how the text actually wrapped. The max height is in
  // the class list, so past it this stops growing and starts scrolling.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [input]);

  async function send(text?: string) {
    const msg = (text ?? input).trim();
    // Allow send while busy: newer message supersedes the in-flight turn.
    if (!msg || sending) return;
    setInput("");
    setErr("");
    setSending(true);
    setPendingMsg(msg);
    setBusy(true);
    try {
      // the user row + turn:running arrive back over the SSE stream
      await copilot.send(subjectId, msg);
    } catch (error) {
      setErr(apiErrorMessage(error));
      setPendingMsg(null);
      setBusy(false);
    } finally {
      setSending(false);
    }
  }

  const showPending =
    pendingMsg && !rows.some((r) => isTextItem(r.item) && r.item.role === "user" && textContent(r.item) === pendingMsg);

  return (
    // Sized by its parent, not by itself: in the editor rail the panel is pinned
    // to the viewport, so the feed takes whatever height is left under the chain
    // and scrolls inside it rather than stopping short at a fixed cap.
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={feedRef} className="scroll-thin flex min-h-[200px] flex-1 flex-col gap-2 overflow-y-auto pr-1">
        {!loaded ? (
          <div className="p-4 text-muted">Loading…</div>
        ) : items.length === 0 && !showPending ? (
          /* The CoPilot opens, in the exact shape of every reply it will send
             afterwards, and in the position a first message belongs: the top,
             with the rest of the conversation to fill in underneath. There is no
             separate empty-state design to fall out of. */
          <Replied>
            <CopilotProse text={copilot.greeting} />
          </Replied>
        ) : (
          <>
            {items.map((it, i) => {
              /* Rhythm is what makes a transcript readable: everything you said
                 opens a turn, so it gets air above it, and what follows stays
                 tight against it as one answer. A flat gap between every row
                 reads as one undifferentiated column. */
              const opensTurn = it.type === "user" && i > 0;
              const spacing = opensTurn ? "mt-5" : undefined;
              if (it.type === "user") return <Said key={i} className={spacing}>{it.text}</Said>;
              if (it.type === "agent") return <Replied key={i}><CopilotProse text={it.text} /></Replied>;
              return <Edits key={i} actions={it.actions} />;
            })}
            {showPending && <Said>{pendingMsg}</Said>}
            {busy && <Working who={copilot.who} />}
            {turnErr && <Stopped who={copilot.who} text={turnErr} />}
          </>
        )}
      </div>

      {err && (
        <div className="my-2 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
          {err}
        </div>
      )}

      {/* One composer surface, not a field with a button parked on top of it:
          the border and the focus ring belong to the whole box, and the two
          controls are laid out inside it. Absolute positioning was what left
          the button off-centre — nothing was aligning it to the text, it was
          just sitting 7px off the bottom edge.

          `items-end` is what keeps it right in both states: at one line the
          textarea and the button are both 32px tall so they read as centred,
          and as the box grows the button stays with the last line you typed
          rather than drifting to the middle of a paragraph. */}
      <form className="mt-3 border-t border-line pt-3" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <div className="flex items-end gap-1.5 rounded-2xl border border-line-2 bg-white p-1.5 pl-3 transition-[border-color,box-shadow] focus-within:border-ink focus-within:ring-2 focus-within:ring-ink/10">
          <textarea
            ref={inputRef}
            value={input}
            rows={1}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter inserts a newline
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={busy ? "Send to interrupt…" : "Describe a change…"}
            disabled={sending}
            /* leading 18 + 7 top + 7 bottom = the button's 32px exactly. */
            className="scroll-thin max-h-[168px] min-w-0 flex-1 resize-none bg-transparent py-[7px] text-[14px] leading-[18px] text-ink outline-none placeholder:text-placeholder disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            aria-label="Send"
            title="Send"
            className="grid h-8 w-8 flex-none place-items-center rounded-xl bg-ink text-white transition-colors hover:bg-ink-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/25 disabled:bg-subtle disabled:text-faint"
          >
            <svg viewBox="0 0 16 16" fill="none" aria-hidden className="h-4 w-4">
              <path d="M8 12.5v-9M8 3.5L4.25 7.25M8 3.5l3.75 3.75" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </form>
    </div>
  );
}
