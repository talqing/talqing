"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ChatResponse,
  ConversationAttachment,
  ConversationItemResponse,
  CreateChatRequest,
  VarDeclaration,
} from "@talqing/sdk";
import { subscribe, talqing } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { ACTIVE_REGION } from "@/lib/regions";
import { OutcomeBadge } from "../calls/CallAnalysis";
import { talqingImageDataUrl } from "@talqing/client";
import { AttachButton, NO_VISION_HINT, useImageDropzone } from "./ImageAttach";
import {
  SessionVarFields,
  missingVars,
  missingVarsSentence,
  suppliedVars,
} from "./SessionVars";
import {
  AttachmentImages,
  Button,
  ChatBubble,
  ImageTray,
  Input,
  Label,
  Modal,
  SessionStatus,
  Transcript,
} from "./ui";

type ChatMessage = {
  id: string;
  role: "user" | "agent";
  text: string;
  images: ConversationAttachment[];
};

/** An image staged in the composer: what the API will take, plus what the
    thumbnail renders. The data URL is both, so there is no object URL to
    create and revoke. */
type Staged = { id: string; name: string; dataUrl: string };

/** How many images one message may carry — the API's own limit, enforced here
    so the fifth file is refused before it is read rather than after. */
const MAX_IMAGES = 4;

/** How often an ended chat is re-read while its analysis is still being written. */
const ANALYSIS_POLL_MS = 1500;

/** End a chat on the way out of the page. `keepalive` is what lets the request
    outlive the document that sent it; nothing can be awaited here. */
function endOnLeave(chatId: string) {
  void fetch(`${ACTIVE_REGION.api_url}/v1/chats/${chatId}/end`, {
    method: "POST",
    credentials: "include",
    keepalive: true,
  });
}

export default function WebChat({
  agentId,
  agentVersion = null,
  vars,
  vision,
  disabledReason = null,
  beforeStart,
}: {
  agentId: string;
  /** Which version of `agentId` to run; null runs the published one. The chat
   *  keeps it for its whole life, so a later edit needs a new chat. */
  agentVersion?: CreateChatRequest["agent_version"];
  /** What the version this chat runs declares. */
  vars: VarDeclaration[];
  vision: boolean;
  /** Why this person cannot start a test chat, or null when they can. Starting
   *  one runs the agent and bills the organization, so it needs the editor role
   *  — said on the button rather than as a 403 after the click. */
  disabledReason?: string | null;
  /** Runs before the chat starts, and it goes no further when this resolves
   *  false — how the agent editor saves the draft it is about to test. */
  beforeStart?: () => Promise<boolean>;
}) {
  /** The chat on screen. It stays here after it ends, so the transcript and
   *  what the analysis made of it stay readable until the next one starts. */
  const [chat, setChat] = useState<ChatResponse | null>(null);
  const active = chat?.status === "open";
  const conversationId = chat?.conversation_id ?? null;
  /** The open chat's id, for the one place that cannot read state: leaving. */
  const openChatId = useRef<string | null>(null);
  openChatId.current = active ? chat.id : null;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState<{ triggerItemId: string; text: string } | null>(null);
  const [working, setWorking] = useState(false);
  const [streamReady, setStreamReady] = useState(false);
  const [starting, setStarting] = useState(false);
  const [text, setText] = useState("");
  const [staged, setStaged] = useState<Staged[]>([]);
  const [ending, setEnding] = useState(false);
  const [err, setErr] = useState("");
  const [setup, setSetup] = useState(false);
  const [varValues, setVarValues] = useState<Record<string, string>>({});

  /* A test chat is never left open behind the person who started it: an open
     chat is never analysed, never reported and never purged. */
  useEffect(() => {
    const leave = () => {
      if (openChatId.current) endOnLeave(openChatId.current);
    };
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      leave();
    };
  }, []);

  /* An ended chat is finished by the worker a moment after it is marked ended:
     the exit hook speaks, then the analysis is written. Re-read until it is. */
  const analysing = chat?.status === "ended" && chat.analysis?.status === "pending";
  const chatId = chat?.id;
  useEffect(() => {
    if (!analysing || !chatId) return;
    const timer = setInterval(() => {
      void talqing.chats.get({ chat_id: chatId }).then(setChat, () => undefined);
    }, ANALYSIS_POLL_MS);
    return () => clearInterval(timer);
  }, [analysing, chatId]);

  // Open for as long as the chat is on screen, ended or not: the exit hook
  // speaks AFTER the chat is marked ended, and its message arrives here.
  useEffect(() => {
    if (!conversationId) return;
    setStreamReady(false);

    const toMessage = (item: ConversationItemResponse): ChatMessage | null => {
      if (item.type !== "message") return null;
      if (item.role !== "user" && item.role !== "assistant") return null;
      const images = item.attachments ?? [];
      // A photo with no caption is a whole message; only an item with neither
      // is nothing to show.
      if (!item.text && images.length === 0) return null;
      return {
        id: item.id,
        role: item.role === "user" ? "user" : "agent",
        text: item.text ?? "",
        images,
      };
    };

    const stop = subscribe(
      (init) => talqing.conversations.events({ conversation_id: conversationId }, init),
      (event) => {
        switch (event.event) {
          case "conversation.snapshot":
            setMessages(event.items.map(toMessage).filter((m): m is ChatMessage => m !== null));
            setWorking(event.activity.state === "running");
            setStreamReady(true);
            break;
          case "item.created": {
            const next = toMessage(event);
            if (!next) break;
            setMessages((current) => {
              const index = current.findIndex((message) => message.id === next.id);
              if (index === -1) return [...current, next];
              return current.map((message, i) => (i === index ? next : message));
            });
            break;
          }
          case "turn":
            if (event.status === "running") {
              setWorking(true);
              break;
            }
            setWorking(false);
            setDraft(null);
            if (event.status === "error" && event.error) setErr(event.error);
            break;
          case "assistant.started":
            setDraft({ triggerItemId: event.trigger_item_id, text: "" });
            break;
          case "assistant.delta":
            setDraft((current) =>
              current?.triggerItemId === event.trigger_item_id
                ? { ...current, text: current.text + event.text }
                : { triggerItemId: event.trigger_item_id, text: event.text },
            );
            break;
          case "assistant.completed":
            setDraft(null);
            break;
          case "turn.failed":
            setDraft(null);
            setWorking(false);
            setErr(event.message || "Message failed");
            break;
          default:
            break;
        }
      },
    );

    return () => {
      stop();
      setStreamReady(false);
    };
  }, [conversationId]);

  const attach = useCallback(
    async (files: File[]) => {
      setErr("");
      const room = MAX_IMAGES - staged.length;
      if (files.length > room) setErr(`A message can carry ${MAX_IMAGES} images.`);
      if (room <= 0) return;
      const accepted: Staged[] = [];
      for (const file of files.slice(0, room)) {
        try {
          // Validated, downscaled and read as a data URL in one step.
          const dataUrl = await talqingImageDataUrl(file);
          accepted.push({ id: crypto.randomUUID(), name: file.name, dataUrl });
        } catch (error: unknown) {
          setErr(error instanceof Error ? error.message : "That image could not be attached");
          break;
        }
      }
      if (accepted.length) setStaged((current) => [...current, ...accepted]);
    },
    [staged.length],
  );

  const removeStaged = useCallback(
    (id: string) => setStaged((current) => current.filter((image) => image.id !== id)),
    [],
  );

  const canAttach = active && streamReady && vision;
  const { dragging, dropProps } = useImageDropzone({ enabled: canAttach, onFiles: attach });

  async function start() {
    setErr("");
    setSetup(false);
    setStarting(true);
    try {
      const supplied = suppliedVars(vars, varValues);
      const created = await talqing.chats.create({
        createChatRequest: {
          agent_id: agentId,
          agent_version: agentVersion,
          vars: Object.keys(supplied).length ? supplied : null,
        },
      });
      setMessages([]);
      setDraft(null);
      setWorking(false);
      setStaged([]);
      setChat(created);
    } catch (error: unknown) {
      // With every reason under the sentence: a draft that cannot run is
      // refused with a list.
      setErr([apiErrorMessage(error, "Could not start chat"), ...apiErrorList(error)].join("\n"));
    } finally {
      setStarting(false);
    }
  }

  /* The click. Unlike the voice surface there is no setup step unless the agent
     declares variables: that dialog also collects userdata, which a test chat on
     a throwaway contact has no use for, so an extra click before every chat
     with an agent that declares nothing would be a regression. */
  async function begin() {
    setErr("");
    if (beforeStart) {
      setStarting(true);
      const ready = await beforeStart();
      setStarting(false);
      if (!ready) return;
    }
    if (vars.length) setSetup(true);
    else void start();
  }

  async function end() {
    if (!chat) return;
    setEnding(true);
    setErr("");
    try {
      setChat(await talqing.chats.end({ chat_id: chat.id }));
      setDraft(null);
      setWorking(false);
      setStaged([]);
    } catch (error: unknown) {
      setErr(apiErrorMessage(error, "Could not end the chat"));
    } finally {
      setEnding(false);
    }
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const message = text.trim();
    if ((!message && staged.length === 0) || !streamReady || !chat) return;

    const images = staged;
    setText("");
    setStaged([]);
    setErr("");
    setWorking(true);
    /* Not awaited. The request resolves when the turn is over, and the input
       must not wait for that: a second message sent meanwhile supersedes the
       first. What is said in between arrives on the stream above. */
    talqing.chats.messages
      .create({
        chat_id: chat.id,
        chatMessageRequest: {
          message,
          images: images.map((image) => ({ data_url: image.dataUrl, filename: image.name })),
          client_message_id: crypto.randomUUID(),
        },
      })
      .then(async (turn) => {
        // The agent ended the chat itself (`end_call`).
        if (turn.chat_status === "ended") setChat(await talqing.chats.get({ chat_id: chat.id }));
      })
      .catch((error: unknown) => {
        setWorking(false);
        // Put the message back rather than losing what was typed and attached —
        // unless something newer has been typed since.
        setText((current) => current || message);
        setStaged((current) => (current.length ? current : images));
        setErr(apiErrorMessage(error, "Message failed"));
      });
  }

  const displayedMessages = draft?.text
    ? [
        ...messages,
        {
          id: `draft:${draft.triggerItemId}`,
          role: "agent" as const,
          text: draft.text,
          images: [] as ConversationAttachment[],
        },
      ]
    : messages;

  // Before the first message the panel is a centered hero, matching the voice
  // surface and the "Not published yet" state. Once a transcript exists it stays
  // on the working layout even after the chat ends, so ending does not wipe it.
  if (!chat) {
    return (
      <div className="flex flex-col items-center px-6 py-10 text-center">
        <span className="mb-3 grid h-12 w-12 place-items-center rounded-xl border border-line-2 bg-surface text-ink" aria-hidden>
          <svg className="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6a8 8 0 0 1 8-8h2a8 8 0 0 1 8 3Z" />
          </svg>
        </span>
        <h3 className="mb-1 text-[17px] font-semibold leading-6 text-ink">Start a test chat</h3>
        <p className="mb-4 max-w-[340px] text-[13px] leading-relaxed text-muted">
          Type to the agent right here — same prompt and tools a real chat gets.
        </p>
        <Button
          onClick={() => void begin()}
          disabled={starting || Boolean(disabledReason)}
          title={disabledReason ?? undefined}
        >
          {starting ? "Starting…" : "Start text chat"}
        </Button>
        {disabledReason && (
          <div className="mt-3 max-w-[340px] text-[13px] leading-5 text-muted">{disabledReason}</div>
        )}
        {err && (
          <div className="mt-3 max-w-[460px] whitespace-pre-line text-[13.5px] leading-5 text-danger">
            {err}
          </div>
        )}
        {setup && (
          <ChatSetup
            vars={vars}
            values={varValues}
            onChange={setVarValues}
            onStart={start}
            onClose={() => setSetup(false)}
          />
        )}
      </div>
    );
  }

  return (
    <div className={cn("relative grid gap-4 p-4", dragging && "bg-info/[0.04]")} {...dropProps}>
      {dragging && (
        <div className="pointer-events-none absolute inset-2 z-10 grid place-items-center rounded-xl border-2 border-dashed border-info/40">
          <span className="rounded-lg bg-surface px-3 py-1.5 text-[13px] font-medium text-ink shadow-sm">
            Drop to attach
          </span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        {active ? (
          <Button variant="danger" onClick={() => void end()} disabled={ending}>
            {ending ? "Ending…" : "End chat"}
          </Button>
        ) : (
          <Button onClick={() => void begin()} disabled={starting}>
            {starting ? "Starting…" : "Start new chat"}
          </Button>
        )}
        <SessionStatus tone={!active ? "idle" : !streamReady || working ? "busy" : "live"}>
          {active
            ? !streamReady
              ? "connecting…"
              : working
                ? "agent is responding"
                : "live — type below"
            : "ended"}
        </SessionStatus>
      </div>

      {err && <div className="whitespace-pre-line text-[13.5px] leading-5 text-danger">{err}</div>}

      {setup && (
        <ChatSetup
          vars={vars}
          values={varValues}
          onChange={setVarValues}
          onStart={start}
          onClose={() => setSetup(false)}
        />
      )}

      {displayedMessages.length > 0 && (
        <Transcript
          className="max-h-[420px] rounded-xl border border-line bg-surface p-3 shadow-sm"
          data-testid="chat-transcript"
        >
          {displayedMessages.map((message) => (
            <ChatBubble
              key={message.id}
              role={message.role === "user" ? "user" : "agent"}
              who={message.role === "user" ? "You" : "Agent"}
            >
              {message.images.length > 0 && (
                <AttachmentImages images={message.images} className={message.text ? "mb-2" : ""} />
              )}
              {message.text}
            </ChatBubble>
          ))}
        </Transcript>
      )}

      {chat.status === "ended" && <ChatEnding chat={chat} />}

      {active && (
        <form onSubmit={submit} className="mt-1 grid gap-2">
          <ImageTray
            images={staged.map((image) => ({
              id: image.id,
              name: image.name,
              previewUrl: image.dataUrl,
            }))}
            onRemove={removeStaged}
          />
          <div className="flex items-center gap-3">
            <AttachButton
              onFiles={attach}
              disabledReason={
                !vision
                  ? NO_VISION_HINT
                  : !streamReady
                    ? "Wait for the chat to connect."
                    : staged.length >= MAX_IMAGES
                      ? `A message can carry ${MAX_IMAGES} images.`
                      : null
              }
            />
            <Input
              data-testid="chat-input"
              placeholder="Type a message…"
              value={text}
              onChange={(event) => setText(event.target.value)}
              className="flex-1"
              disabled={!streamReady}
              autoFocus
            />
            <Button disabled={!streamReady || (!text.trim() && staged.length === 0)}>Send</Button>
          </div>
        </form>
      )}
    </div>
  );
}

const ENDED_BY: Record<string, string> = {
  end_call: "The agent ended this chat",
  api_end: "You ended this chat",
  replaced: "A newer chat took its place",
};

/* What the chat came to, once it has ended: who ended it, what it cost, and what
   the analysis made of it. This is the same analysis `session.completed`
   carries, so a test chat shows its author what their webhook will receive. */
function ChatEnding({ chat }: { chat: ChatResponse }) {
  const analysis = chat.analysis;
  const pending = analysis?.status === "pending";
  return (
    <div className="grid gap-2.5 rounded-xl border border-line-2 bg-white p-3.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="text-[13.5px] font-medium leading-5 text-ink">
          {ENDED_BY[chat.close_reason ?? ""] ?? "Chat ended"}
        </span>
        {analysis?.outcome && <OutcomeBadge outcome={analysis.outcome} />}
        {chat.cost && (
          <span className="ml-auto text-[12.5px] leading-5 tabular-nums text-muted">
            ${chat.cost.total_charge.toFixed(4)}
          </span>
        )}
      </div>
      {pending && <p className="text-[13px] leading-5 text-muted">Analysing…</p>}
      {analysis?.status === "completed" && analysis.summary && (
        <p className="text-[13.5px] leading-6 text-ink-soft">{analysis.summary}</p>
      )}
      {analysis?.status === "completed" && analysis.outcome_rationale && (
        <p className="text-[12.5px] leading-5 text-muted">{analysis.outcome_rationale}</p>
      )}
      {analysis?.status === "skipped" && (
        <p className="text-[13px] leading-5 text-muted">Too short to analyse.</p>
      )}
      {analysis?.status === "failed" && (
        <p className="text-[13px] leading-5 text-muted">Analysis did not complete.</p>
      )}
    </div>
  );
}

/* The one thing a test chat can be set up with. The voice surface always shows
   its dialog because it also collects userdata; here there is nothing else to
   ask for, so this appears only when the agent declares variables. */
function ChatSetup({
  vars,
  values,
  onChange,
  onStart,
  onClose,
}: {
  vars: VarDeclaration[];
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  onStart: () => void;
  onClose: () => void;
}) {
  const missing = missingVars(vars, values);
  return (
    <Modal
      title="Start a test chat"
      sub="Fixed for the life of the chat — every message in it reads the same values."
      width="max-w-[520px]"
      onClose={onClose}
      footer={
        <>
          {missing.length > 0 && (
            <span className="mr-auto text-[12.5px] leading-5 text-warn">
              {missingVarsSentence(missing)}
            </span>
          )}
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="web-chat-setup" disabled={missing.length > 0}>
            Start chat
          </Button>
        </>
      }
    >
      <form
        id="web-chat-setup"
        className="grid gap-4 pb-6"
        onSubmit={(e) => {
          e.preventDefault();
          onStart();
        }}
      >
        <div className="flex flex-col gap-1.5">
          <Label>Variables</Label>
          <SessionVarFields
            declared={vars}
            values={values}
            onChange={onChange}
            idPrefix="web-chat-var"
          />
          <p className="text-[13px] leading-5 text-muted">
            What this chat knows about the deployment rather than about the person. The agent
            reads them as{" "}
            <code className="font-mono text-[12.5px] text-ink">{"{{vars.name}}"}</code> in its
            prompt and its tools.
          </p>
        </div>
      </form>
    </Modal>
  );
}
