"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AppShell } from "../components/AppShell";
import { Button, EmptyState, Select, btn, Skeleton } from "../components/ui";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api, controlApi, subscribe, talqing } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import type {
  ItemDeliveryUpdatedEvent,
  AgentResponse,
  HealthSnapshotResponse,
  ConversationItemResponse,
  ConversationResponse,
  ConversationSessionResponse,
  ConversationTraceEventResponse,
} from "@talqing/sdk";
import { Thread } from "./Thread";

const LIST_GRID = "grid-cols-[minmax(0,1fr)_86px]";
const ITEMS_PAGE = 80;
const SESSIONS_PAGE = 50;
const LIST_LIMIT = 100;

function formatTime(value?: string | null): string {
  if (!value) return "No activity";
  return new Date(value).toLocaleString();
}

function formatClock(value?: string | null): string {
  if (!value) return "";
  return new Date(value).toLocaleTimeString();
}

function contactLabel(conversation: ConversationResponse): string {
  const ref = conversation.ref;
  return (
    ref?.display_name ||
    ref?.contact_key ||
    conversation.contact_key ||
    "Unknown participant"
  );
}

function surfaceLabel(surface?: string | null): string {
  if (!surface) return "web";
  if (surface === "text_api") return "web";
  return surface;
}

/** How long after a chat is ended its analysis and final cost take to land. */
const CHAT_SETTLE_MS = 8000;

function previewLine(conversation: ConversationResponse): string {
  const text = conversation.last_message_text?.trim();
  if (text) return text.length > 120 ? `${text.slice(0, 117)}…` : text;
  return "No messages yet.";
}

function mergeById(
  current: ConversationItemResponse[],
  incoming: ConversationItemResponse[],
): ConversationItemResponse[] {
  const map = new Map<string, ConversationItemResponse>();
  for (const item of current) map.set(item.id, item);
  for (const item of incoming) map.set(item.id, item);
  return Array.from(map.values()).sort((a, b) => {
    const ta = new Date(a.created_at).getTime();
    const tb = new Date(b.created_at).getTime();
    if (ta !== tb) return ta - tb;
    return a.id.localeCompare(b.id);
  });
}

/* A conversation's trace spans sessions, each numbering its events from one. */
function mergeEvents(
  current: ConversationTraceEventResponse[],
  incoming: ConversationTraceEventResponse[],
): ConversationTraceEventResponse[] {
  const map = new Map<string, ConversationTraceEventResponse>();
  for (const event of [...current, ...incoming]) map.set(`${event.session_id}:${event.seq}`, event);
  return Array.from(map.values()).sort(
    (a, b) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.seq - b.seq,
  );
}

function upsertItem(
  current: ConversationItemResponse[],
  item: ConversationItemResponse,
): ConversationItemResponse[] {
  return mergeById(current, [item]);
}

function patchItemDelivery(
  current: ConversationItemResponse[],
  data: ItemDeliveryUpdatedEvent,
): ConversationItemResponse[] {
  const ids = new Set(data.item_ids);
  if (!ids.size) return current;
  return current.map((item) => {
    if (!ids.has(item.id)) return item;
    return {
      ...item,
      delivery_status: data.delivery_status as ConversationItemResponse["delivery_status"],
      delivery_error: data.error
        ? { message: data.error }
        : data.delivery_status === "failed"
          ? item.delivery_error
          : null,
      provider_message_id: data.provider_message_id || item.provider_message_id,
    };
  });
}

function ConversationsPageInner() {
  const region = useActiveRegion();
  // Query param, not a path segment, for the reason the calls page gives. Read
  // once: it is where a batch row or a call lands, and how a conversation that
  // is not listed (nobody has replied yet) is reached at all.
  const linkedId = useSearchParams().get("id");
  const [agentFilter, setAgentFilter] = useState<string>("all");
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [conversations, setConversations] = useState<ConversationResponse[]>([]);
  const [listHasMore, setListHasMore] = useState(false);
  const [listOffset, setListOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [items, setItems] = useState<ConversationItemResponse[]>([]);
  const [itemsHasMore, setItemsHasMore] = useState(false);
  const [itemsOffset, setItemsOffset] = useState(0);
  const [sessions, setSessions] = useState<ConversationSessionResponse[]>([]);
  const [events, setEvents] = useState<ConversationTraceEventResponse[]>([]);
  const [snapshot, setSnapshot] = useState<HealthSnapshotResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [loadingMoreList, setLoadingMoreList] = useState(false);
  const [live, setLive] = useState(false);
  const [working, setWorking] = useState(false);
  const [draft, setDraft] = useState<{ triggerItemId: string; text: string } | null>(null);
  const [err, setErr] = useState("");
  /** Ending a chat finalizes and bills it, so it needs the editor role. */
  const [canEnd, setCanEnd] = useState(false);
  const [endingChat, setEndingChat] = useState<string | null>(null);

  useEffect(() => {
    void controlApi.me().then((ctx) => setCanEnd(ctx.user.role !== "VIEWER"));
  }, []);

  const selectedIdRef = useRef<string | null>(null);
  const detailSeq = useRef(0);
  const timelineRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  useEffect(() => {
    selectedIdRef.current = selectedId;
    const url = new URL(window.location.href);
    if (selectedId) url.searchParams.set("id", selectedId);
    else url.searchParams.delete("id");
    window.history.replaceState(null, "", url);
  }, [selectedId]);

  const selected = useMemo(
    () => conversations.find((c) => c.id === selectedId) || null,
    [conversations, selectedId],
  );

  /* Who each reply was from, for a thread that changed agents. */
  const agentNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const agent of agents) names[agent.id] = agent.config.name;
    for (const session of sessions) {
      if (session.agent.agent_id && session.agent.name) {
        names[session.agent.agent_id] ??= session.agent.name;
      }
    }
    return names;
  }, [agents, sessions]);

  const scrollTimelineToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const el = timelineRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  useEffect(() => {
    if (!stickToBottom.current) return;
    scrollTimelineToBottom();
  }, [items, draft, scrollTimelineToBottom]);

  const loadAgents = useCallback(async () => {
    try {
      const page = await api.listAgents({ limit: 200, offset: 0 });
      setAgents(page.items);
    } catch {
      // Agent filter is optional; ignore load failures.
    }
  }, []);

  const loadList = useCallback(
    async (opts?: {
      nextAgent?: string;
      offset?: number;
      append?: boolean;
      silent?: boolean;
    }) => {
      const nextAgent = opts?.nextAgent ?? agentFilter;
      const offset = opts?.offset ?? 0;
      const append = opts?.append ?? false;
      if (!opts?.silent) {
        if (append) setLoadingMoreList(true);
        else setLoading(true);
      }
      setErr("");
      try {
        const page = await api.listConversations({
          limit: LIST_LIMIT,
          offset,
          agent_id: nextAgent === "all" ? null : nextAgent,
        });
        setListHasMore(page.has_more);
        setListOffset(offset);
        setConversations((current) => {
          if (append) {
            const seen = new Set(current.map((c) => c.id));
            return [...current, ...page.items.filter((c) => !seen.has(c.id))];
          }
          // The open conversation stays, on top, when this page does not carry
          // it: one reached by a link, or one further down than the first page.
          const open = current.find((c) => c.id === selectedIdRef.current);
          return open && !page.items.some((c) => c.id === open.id)
            ? [open, ...page.items]
            : page.items;
        });
        setSelectedId((current) => current ?? page.items[0]?.id ?? null);
      } catch (error) {
        setErr(apiErrorMessage(error, "Failed to load conversations"));
      } finally {
        if (!opts?.silent) {
          setLoading(false);
          setLoadingMoreList(false);
        }
      }
    },
    [agentFilter],
  );

  const loadDetail = useCallback(
    async (conversationId: string | null) => {
      if (!conversationId) {
        setItems([]);
        setSessions([]);
        setEvents([]);
        setSnapshot(null);
        setItemsHasMore(false);
        setItemsOffset(0);
        setWorking(false);
        setDraft(null);
        return;
      }
      const seq = ++detailSeq.current;
      setDetailLoading(true);
      setErr("");
      stickToBottom.current = true;
      try {
        const [itemPage, sessionPage, health] = await Promise.all([
          api.listConversationItems(conversationId, ITEMS_PAGE, 0, "desc"),
          api.listConversationSessions(conversationId, SESSIONS_PAGE, 0),
          // A thread with no completed session has nothing to judge yet; keep
          // the timeline usable rather than failing the whole detail load.
          api.getConversationSnapshot(conversationId).catch(() => null),
        ]);
        // The trace for the same window of the thread the items cover.
        const trace = await api.listConversationTrace(
          conversationId,
          itemPage.has_more && itemPage.items[0] ? { after: itemPage.items[0].created_at } : {},
        );
        if (seq !== detailSeq.current || selectedIdRef.current !== conversationId) return;
        setItems(itemPage.items);
        setEvents(trace.events);
        setSnapshot(health);
        setItemsHasMore(itemPage.has_more);
        setItemsOffset(0);
        setSessions(sessionPage.items);
      } catch (error) {
        if (seq !== detailSeq.current) return;
        setErr(apiErrorMessage(error, "Failed to load conversation"));
      } finally {
        if (seq === detailSeq.current) setDetailLoading(false);
      }
    },
    [],
  );

  const reloadSessions = useCallback(async (conversationId: string) => {
    const page = await api.listConversationSessions(conversationId, SESSIONS_PAGE, 0);
    if (selectedIdRef.current !== conversationId) return;
    setSessions(page.items);
  }, []);

  /* What a finished turn changed, re-read. A turn can start a chat, end one
     (`end_call`) or move its cost; it adds to the trace; and the live frames it
     sent carry no reply latency, which is worked out over the stored thread. */
  const refreshAfterTurn = useCallback(
    async (conversationId: string) => {
      const [itemPage, trace, health] = await Promise.all([
        api.listConversationItems(conversationId, ITEMS_PAGE, 0, "desc"),
        api.listConversationTrace(conversationId),
        api.getConversationSnapshot(conversationId).catch(() => null),
        reloadSessions(conversationId),
      ]);
      if (selectedIdRef.current !== conversationId) return;
      setItems((current) => mergeById(current, itemPage.items));
      setEvents((current) => mergeEvents(current, trace.events));
      setSnapshot(health);
    },
    [reloadSessions],
  );

  /* Ended at once, finished a moment later: the worker runs the exit hook, then
     the analysis, then the bill. One re-read after that is what brings the
     summary and the final cost onto the card. */
  const endChat = useCallback(
    async (chatId: string, conversationId: string) => {
      setEndingChat(chatId);
      setErr("");
      try {
        await talqing.chats.end({ chat_id: chatId });
        await reloadSessions(conversationId);
        setTimeout(() => void refreshAfterTurn(conversationId), CHAT_SETTLE_MS);
      } catch (error) {
        setErr(apiErrorMessage(error, "Could not end the chat"));
      } finally {
        setEndingChat(null);
      }
    },
    [reloadSessions, refreshAfterTurn],
  );

  const loadOlderItems = useCallback(async () => {
    if (!selectedId || !itemsHasMore || loadingOlder) return;
    const el = timelineRef.current;
    const prevHeight = el?.scrollHeight ?? 0;
    const prevTop = el?.scrollTop ?? 0;
    setLoadingOlder(true);
    setErr("");
    try {
      const nextOffset = itemsOffset + ITEMS_PAGE;
      const page = await api.listConversationItems(selectedId, ITEMS_PAGE, nextOffset, "desc");
      // The older window's trace: from where this page starts (or the very
      // beginning, on the last page) up to where the loaded thread did.
      const trace = await api.listConversationTrace(selectedId, {
        after: page.has_more ? page.items[0]?.created_at : undefined,
        before: items[0]?.created_at,
      });
      if (selectedIdRef.current !== selectedId) return;
      setItems((current) => mergeById(current, page.items));
      setEvents((current) => mergeEvents(current, trace.events));
      setItemsHasMore(page.has_more);
      setItemsOffset(nextOffset);
      stickToBottom.current = false;
      requestAnimationFrame(() => {
        if (!timelineRef.current) return;
        const delta = timelineRef.current.scrollHeight - prevHeight;
        timelineRef.current.scrollTop = prevTop + delta;
      });
    } catch (error) {
      setErr(apiErrorMessage(error, "Failed to load older messages"));
    } finally {
      setLoadingOlder(false);
    }
  }, [selectedId, itemsHasMore, loadingOlder, itemsOffset, items]);

  useEffect(() => {
    void loadAgents();
    void (async () => {
      let linkErr = "";
      if (linkedId) {
        try {
          const linked = await api.getConversation(linkedId);
          // The ref as well as the state: `loadList` reads it to keep this row.
          selectedIdRef.current = linked.id;
          setConversations([linked]);
          setSelectedId(linked.id);
        } catch (error) {
          linkErr = apiErrorMessage(error, "Failed to load conversation");
        }
      }
      await loadList();
      if (linkErr) setErr(linkErr);
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- initial mount only

  useEffect(() => {
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // Live SSE for the selected conversation
  useEffect(() => {
    if (!selectedId) {
      setLive(false);
      return;
    }
    const conversationId = selectedId;
    const softRefreshList = () => {
      void loadList({ silent: true, offset: 0, append: false });
    };

    const stop = subscribe(
      (init) => talqing.conversations.events({ conversation_id: conversationId }, init),
      (event) => {
        switch (event.event) {
          case "conversation.snapshot": {
            const patch = event.conversation;
            setConversations((current) =>
              current.map((c) => (c.id === conversationId ? { ...c, ...patch } : c)),
            );
            setItems(event.items);
            setItemsHasMore(false);
            setItemsOffset(0);
            // The snapshot carries the whole thread, so the trace is the whole
            // trace too.
            void api.listConversationTrace(conversationId).then((trace) => {
              if (selectedIdRef.current === conversationId) setEvents(trace.events);
            });
            stickToBottom.current = true;
            setWorking(event.activity.state === "running");
            setLive(true);
            break;
          }
          case "item.created":
            setItems((current) => upsertItem(current, event));
            setConversations((current) =>
              current.map((c) =>
                c.id === conversationId
                  ? {
                      ...c,
                      last_message_text: event.text?.trim() || c.last_message_text,
                      last_message_at: event.created_at || c.last_message_at,
                    }
                  : c,
              ),
            );
            // Not set on a customer's message: one nobody is assigned to answer
            // starts no turn. The `turn` event says when an agent picks it up.
            if (event.type === "message" && event.role === "assistant") setWorking(false);
            break;
          case "item.delivery_updated":
            setItems((current) => patchItemDelivery(current, event));
            break;
          case "turn":
            if (event.status === "running") {
              setWorking(true);
              break;
            }
            setWorking(false);
            setDraft(null);
            if (event.status === "done" || event.status === "error") {
              softRefreshList();
              void refreshAfterTurn(conversationId);
            }
            break;
          case "assistant.started":
            setDraft({ triggerItemId: event.trigger_item_id, text: "" });
            setWorking(true);
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
            setWorking(false);
            break;
          case "turn.failed":
            setDraft(null);
            setWorking(false);
            break;
          default:
            break;
        }
      },
    );

    return () => {
      stop();
      setLive(false);
    };
  }, [selectedId, loadList, refreshAfterTurn]);

  function onTimelineScroll() {
    const el = timelineRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottom.current = distance < 48;
  }

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-6 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line bg-white py-5 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h1 className="text-[26px] font-semibold leading-8">Conversations</h1>
            <p className="mt-2 max-w-[72ch] text-[14px] leading-5 text-muted">
              Customer conversations from browser sessions and messaging integrations.
            </p>
          </div>
          <div className="w-full sm:w-[200px]">
            <Select
              value={agentFilter}
              onChange={(e) => {
                const next = e.target.value;
                setAgentFilter(next);
                // A different list: do not carry the open conversation into it.
                selectedIdRef.current = null;
                setSelectedId(null);
                void loadList({ nextAgent: next, offset: 0, append: false });
              }}
            >
              <option value="all">All agents</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.config?.name || agent.id.slice(0, 8)}
                </option>
              ))}
            </Select>
          </div>
        </header>

        {err && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {/* The two panes take what is left of the window and scroll inside it,
            so the thread is as tall as the screen allows. */}
        <div className="grid gap-5 lg:h-[calc(100vh-9.75rem)] lg:min-h-[560px] lg:grid-cols-[340px_minmax(0,1fr)]">
          <section className="flex max-h-[70vh] flex-col overflow-hidden lg:max-h-none rounded-xl border border-line bg-white shadow-[0_1px_2px_rgba(15,15,16,0.025)]">
            <div
              className={cn(
                "hidden shrink-0 gap-3 border-b border-line bg-white px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
                LIST_GRID,
              )}
            >
              <span>Thread</span>
              <span className="text-right">Time</span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {loading ? (
                <div aria-busy="true" aria-label="Loading conversations">
                  {["w-[46%]", "w-[58%]", "w-[38%]", "w-[52%]", "w-[42%]"].map((w) => (
                    <div key={w} className={cn("grid gap-3 border-b border-line px-4 py-3.5 last:border-b-0 sm:items-center", LIST_GRID)}>
                      <span className="flex min-w-0 flex-col gap-1.5">
                        <Skeleton className={cn("h-4", w)} />
                        <Skeleton className="h-3 w-[80%]" />
                        <Skeleton className="h-3 w-[36%]" />
                      </span>
                      <Skeleton className="h-3 w-10 justify-self-end" />
                    </div>
                  ))}
                </div>
              ) : conversations.length === 0 ? (
                <div className="p-4">
                  <EmptyState
                    title={`No conversations in ${region.name}`}
                    body="Customer threads from web chat and Telegram will show up here once someone messages a published text agent."
                    cta={
                      <div className="flex flex-wrap justify-center gap-2">
                        <Link href="/integrations" className={btn("secondary", "sm")}>
                          Connect messaging
                        </Link>
                        <Link href="/agents" className={btn("primary", "sm")}>
                          View agents
                        </Link>
                      </div>
                    }
                  />
                </div>
              ) : (
                <>
                  {conversations.map((conversation) => {
                    const active = conversation.id === selectedId;
                    return (
                      <button
                        key={conversation.id}
                        type="button"
                        onClick={() => setSelectedId(conversation.id)}
                        className={cn(
                          "grid w-full gap-3 border-b border-line px-4 py-3.5 text-left transition-colors last:border-b-0 hover:bg-canvas sm:items-center",
                          LIST_GRID,
                          active && "bg-subtle",
                        )}
                      >
                        <span className="min-w-0">
                          <span className="flex min-w-0 items-center gap-2">
                            <span className="block truncate text-[14px] font-semibold leading-5">
                              {contactLabel(conversation)}
                            </span>
                            <span className="shrink-0 rounded-md bg-subtle px-1.5 py-0.5 text-[10.5px] font-medium uppercase tracking-[0.04em] text-ink-soft">
                              {surfaceLabel(conversation.surface)}
                            </span>
                          </span>
                          <span className="mt-1 block truncate text-[12.5px] leading-4 text-muted">
                            {previewLine(conversation)}
                          </span>
                          <span className="mt-0.5 block truncate text-[11.5px] leading-4 text-faint">
                            {conversation.agent_name || "Unassigned"}
                            {" · "}
                            {formatTime(conversation.last_message_at || conversation.updated_at)}
                          </span>
                        </span>
                        <span className="flex justify-end text-[11.5px] text-faint">
                          {formatClock(conversation.last_message_at || conversation.updated_at)}
                        </span>
                      </button>
                    );
                  })}
                  {listHasMore && (
                    <div className="p-3">
                      <Button
                        variant="secondary"
                        size="sm"
                        className="w-full"
                        disabled={loadingMoreList}
                        onClick={() =>
                          void loadList({
                            offset: listOffset + LIST_LIMIT,
                            append: true,
                          })
                        }
                      >
                        {loadingMoreList ? "Loading…" : "Load more threads"}
                      </Button>
                    </div>
                  )}
                </>
              )}
            </div>
          </section>

          <section className="flex h-[80vh] min-w-0 flex-col overflow-hidden lg:h-auto rounded-xl border border-line bg-white shadow-[0_1px_2px_rgba(15,15,16,0.025)]">
            {!selected ? (
              <div className="grid min-h-[420px] flex-1 place-items-center px-4 text-[13px] leading-5 text-muted">
                {!loading && "Select a conversation."}
              </div>
            ) : (
              <>
                <div className="flex shrink-0 flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-line px-5 py-3.5">
                  <h2 className="truncate text-[15px] font-semibold leading-5">
                    {contactLabel(selected)}
                  </h2>
                  <span className="rounded-md bg-subtle px-1.5 py-0.5 text-[10.5px] font-medium uppercase tracking-[0.04em] text-ink-soft">
                    {surfaceLabel(selected.surface)}
                  </span>
                  <span className="text-[12.5px] text-muted">
                    {selected.agent_name || "No agent assigned"}
                  </span>
                  <span className="ml-auto flex items-center gap-3 text-[11.5px]">
                    {working && <span className="font-medium text-warn">Agent working…</span>}
                    {live ? (
                      <span className="inline-flex items-center gap-1.5 font-medium text-live">
                        <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
                        Live
                      </span>
                    ) : (
                      <span className="text-faint">Connecting…</span>
                    )}
                  </span>
                </div>

                {detailLoading ? (
                  <div className="flex flex-col gap-3 p-5" aria-busy="true" aria-label="Loading messages">
                    <Skeleton className="h-12 w-3/5 rounded-xl" />
                    <Skeleton className="h-10 w-1/2 self-end rounded-xl" />
                    <Skeleton className="h-16 w-2/3 rounded-xl" />
                    <Skeleton className="h-10 w-2/5 self-end rounded-xl" />
                  </div>
                ) : items.length === 0 && !draft ? (
                  <div className="py-14 text-center">
                    <p className="text-[14px] font-semibold text-ink">No messages yet</p>
                    <p className="mt-1 text-[13px] text-muted">New messages appear here as they arrive.</p>
                  </div>
                ) : (
                  <Thread
                    items={items}
                    events={events}
                    sessions={sessions}
                    snapshot={snapshot}
                    agentNames={agentNames}
                    draft={draft?.text || null}
                    canEnd={canEnd}
                    endingChat={endingChat}
                    onEndChat={(chatId) => void endChat(chatId, selected.id)}
                    hasOlder={itemsHasMore}
                    loadingOlder={loadingOlder}
                    onLoadOlder={() => void loadOlderItems()}
                    onReload={() => void loadDetail(selectedId)}
                    scrollRef={timelineRef}
                    onScroll={onTimelineScroll}
                  />
                )}
              </>
            )}
          </section>
        </div>
      </div>
    </AppShell>
  );
}

// useSearchParams() suspends during prerender, so the exported page provides the
// boundary, as the calls page does.
export default function ConversationsPage() {
  return (
    <Suspense fallback={null}>
      <ConversationsPageInner />
    </Suspense>
  );
}
