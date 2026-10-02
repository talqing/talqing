"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "@/app/components/AppShell";
import {
  Badge,
  Button,
  EmptyState,
  Modal,
  Skeleton,
  useToast,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { useActiveRegion } from "@/lib/regions";
import type {
  AgentResponse,
  CallSummaryResponse,
  StreamConnectionResponse,
} from "@talqing/sdk";
import { ConnectionRow, ROW_GRID } from "./ConnectionRow";
import { CreateConnection } from "./CreateConnection";
import {
  DIALECTS,
  DIALECT_ORDER,
  needsAttention,
  isVoiceAgent,
  type StreamDialect,
} from "./shared";
import { cn } from "@/lib/cn";

/* Traffic is decoration on this page, and the window is named wherever a figure
   is: /v1/calls answers for the last 30 days when asked for none, so a total
   that looks like "this week" silently would not be. */
const WINDOW_DAYS = 7;
const CALLS_PAGE = 200;
const CALLS_MAX = 1000;

const Icon = {
  plus: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  stream: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M4 12h2m12 0h2" />
      <path d="M8.5 8.5v7M12 6v12m3.5-9.5v7" />
    </svg>
  ),
};

export default function StreamsPage() {
  const region = useActiveRegion();
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [connections, setConnections] = useState<StreamConnectionResponse[]>([]);
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [calls, setCalls] = useState<CallSummaryResponse[] | null>(null);
  const [loadError, setLoadError] = useState("");

  const [creating, setCreating] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<StreamConnectionResponse | null>(null);
  const [confirming, setConfirming] = useState(false);

  const notify = useCallback((msg: string) => toast({ kind: "ok", msg }), [toast]);
  const fail = useCallback((msg: string) => toast({ kind: "err", msg }), [toast]);

  const voiceAgents = useMemo(() => agents.filter(isVoiceAgent), [agents]);
  const agentsById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);

  const load = useCallback(async () => {
    try {
      const [page, nextAgents] = await Promise.all([
        api.listStreamConnections(),
        api.listAllAgents(),
      ]);
      setConnections(page.items);
      setAgents(nextAgents);
      setLoadError("");
    } catch (error: unknown) {
      setLoadError(apiErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    /* Traffic decorates the page; a failure leaves the counts blank rather than
       taking the connections down with it. */
    const start = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString();
    void (async () => {
      const items: CallSummaryResponse[] = [];
      try {
        for (let offset = 0; offset < CALLS_MAX; offset += CALLS_PAGE) {
          const page = await api.listCalls({ start, type: "STREAM", limit: CALLS_PAGE, offset });
          items.push(...page.items);
          if (!page.has_more) break;
        }
      } catch {
        /* leave the counts blank */
      }
      setCalls(items);
    })();
  }, [load]);

  /* Arrived from a call's "Stream connection" link. Opens that row's setup
     panel and tints it, so the reader lands on the thing they clicked rather
     than on a list they now have to search. */
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("id");
    if (!id) return;
    setHighlighted(id);
    setExpanded(id);
  }, []);

  const callsByConnection = useMemo(() => {
    const counts = new Map<string, number>();
    for (const call of calls ?? []) {
      if (!call.stream_connection_id) continue;
      counts.set(call.stream_connection_id, (counts.get(call.stream_connection_id) ?? 0) + 1);
    }
    return counts;
  }, [calls]);

  /** Run a row action, reporting either way and always refreshing. */
  const run = useCallback(
    async (id: string, work: () => Promise<string>) => {
      setBusyId(id);
      try {
        notify(await work());
      } catch (error: unknown) {
        fail(apiErrorMessage(error));
      } finally {
        setBusyId("");
        await load();
      }
    },
    [fail, load, notify],
  );

  const groups = useMemo(() => {
    /* Grouped by platform, and ordered so anything a person has to act on
       floats: a workspace with one live SparkTG connection and one that points
       at an unpublished agent should not have to scan for the second. */
    const byDialect = new Map<StreamDialect, StreamConnectionResponse[]>();
    for (const connection of connections) {
      const dialect = connection.dialect as StreamDialect;
      byDialect.set(dialect, [...(byDialect.get(dialect) ?? []), connection]);
    }
    return DIALECT_ORDER.filter((dialect) => byDialect.has(dialect))
      .map((dialect) => ({
        dialect,
        rows: (byDialect.get(dialect) ?? []).sort(
          (a, b) =>
            Number(needsAttention(b.readiness)) - Number(needsAttention(a.readiness)) ||
            a.name.localeCompare(b.name),
        ),
      }))
      .sort(
        (a, b) =>
          Number(a.rows.every((r) => !needsAttention(r.readiness))) -
            Number(b.rows.every((r) => !needsAttention(r.readiness))) ||
          b.rows.length - a.rows.length,
      );
  }, [connections]);

  async function confirmDelete() {
    if (!deleteTarget) return;
    setConfirming(true);
    try {
      await api.deleteStreamConnection(deleteTarget.id);
      notify(`${deleteTarget.name} deleted. Its calls are kept.`);
      setDeleteTarget(null);
    } catch (error: unknown) {
      fail(apiErrorMessage(error));
    } finally {
      setConfirming(false);
      await load();
    }
  }

  const totalCalls = calls?.length ?? 0;
  const attention = connections.filter((c) => needsAttention(c.readiness)).length;

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line py-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="font-display text-[26px] font-semibold leading-8 tracking-tight text-ink">
              Media streams
            </h1>
            <p className="mt-1.5 text-[14px] leading-5 text-muted">
              Let a contact-centre platform keep its numbers and stream the audio to one of your
              agents.
            </p>
          </div>
          <Button onClick={() => setCreating(true)} className="min-h-[38px] self-start sm:self-auto">
            {Icon.plus}
            New connection
          </Button>
        </header>

        {loadError && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger [overflow-wrap:anywhere]">
            {loadError}
          </div>
        )}

        {loading ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            <div className="flex items-center gap-3.5 border-b border-line bg-canvas px-4 py-3.5">
              <Skeleton className="h-4 w-[140px]" />
            </div>
            {[0, 1, 2].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[26%]" />
                  <Skeleton className="mt-2 h-3 w-[38%]" />
                </div>
                <Skeleton className="h-9 w-[160px] rounded-[10px]" />
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : connections.length === 0 ? (
          <EmptyState
            icon={Icon.stream}
            title={`No media streams in ${region.name}`}
            body="A partner's platform — SparkTG, a Twilio or Plivo shop, Exotel, Vonage — keeps the number, the queue and the CRM, and streams the caller's audio to one of your agents over a WebSocket. They configure one URL; nothing about their setup moves."
            cta={
              <Button onClick={() => setCreating(true)}>
                {Icon.plus}
                Create your first connection
              </Button>
            }
          />
        ) : (
          <>
            {(totalCalls > 0 || attention > 0) && (
              <div className="mb-4 flex flex-wrap items-center gap-2.5 text-[13px] leading-5 text-muted">
                {totalCalls > 0 && (
                  <span>
                    <span className="font-medium text-ink">{totalCalls}</span> streamed{" "}
                    {totalCalls === 1 ? "call" : "calls"} in the last {WINDOW_DAYS} days
                  </span>
                )}
                {totalCalls > 0 && attention > 0 && <span className="text-line-strong">·</span>}
                {attention > 0 && (
                  <Badge variant="warn">
                    {attention} {attention === 1 ? "connection needs" : "connections need"} a
                    published agent
                  </Badge>
                )}
              </div>
            )}

            <div className="grid gap-4">
              {groups.map(({ dialect, rows }) => (
                <section
                  key={dialect}
                  className="overflow-hidden rounded-xl border border-line-2 bg-white"
                >
                  <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-canvas px-4 py-3">
                    <h2 className="text-[14px] font-semibold leading-5 text-ink">
                      {DIALECTS[dialect].label}
                    </h2>
                    <span className="text-[13px] leading-5 text-muted">
                      {DIALECTS[dialect].connects}
                    </span>
                  </header>

                  {/* Column heads only where the columns exist: below `lg` the
                      calls figure is gone, and a head for a column that is not
                      there is worse than none. */}
                  <div
                    className={cn(
                      "hidden gap-3 border-b border-line px-4 py-2 text-[12px] font-semibold uppercase tracking-[0.06em] text-faint lg:grid",
                      ROW_GRID,
                    )}
                  >
                    <span>Connection</span>
                    <span>Answered by</span>
                    <span>Calls</span>
                    <span>Status</span>
                    <span />
                  </div>

                  {rows.map((connection) => {
                    return (
                      <ConnectionRow
                        key={connection.id}
                        connection={connection}
                        agents={voiceAgents}
                        assignedAgent={agentsById.get(connection.agent_id)}
                        callsThisWeek={callsByConnection.get(connection.id) ?? 0}
                        busy={busyId === connection.id}
                        highlighted={highlighted === connection.id}
                        expanded={expanded === connection.id}
                        onToggle={() =>
                          setExpanded((current) =>
                            current === connection.id ? null : connection.id,
                          )
                        }
                        onAssign={(agentId) =>
                          void run(connection.id, async () => {
                            const updated = await api.patchStreamConnection(connection.id, {
                              agent_id: agentId,
                            });
                            return `${updated.name} now answers with ${
                              updated.agent_name ?? "the selected agent"
                            }.`;
                          })
                        }
                        onDisable={() =>
                          void run(connection.id, async () => {
                            await api.patchStreamConnection(connection.id, { status: "disabled" });
                            return `${connection.name} disabled. Calls already running are unaffected.`;
                          })
                        }
                        onEnable={() =>
                          void run(connection.id, async () => {
                            await api.patchStreamConnection(connection.id, { status: "active" });
                            return `${connection.name} is answering again.`;
                          })
                        }
                        onDelete={() => setDeleteTarget(connection)}
                      />
                    );
                  })}
                </section>
              ))}
            </div>

            {voiceAgents.length === 0 && (
              <p className="mt-3 text-[13px] leading-5 text-muted">
                No voice agents yet.{" "}
                <Link href="/agents" className="font-medium text-ink underline underline-offset-2">
                  Create one
                </Link>{" "}
                and these connections start answering.
              </p>
            )}
          </>
        )}
      </div>

      {creating && (
        <CreateConnection
          agents={voiceAgents}
          existing={connections}
          onClose={() => setCreating(false)}
          onCreated={async (connection) => {
            setExpanded(connection.id);
            setCreating(false);
            notify(`${connection.name} is ready. Send the URL to the partner.`);
            await load();
          }}
        />
      )}

      {deleteTarget && (
        <Modal
          title="Delete this connection?"
          sub="The partner's URL stops working immediately. The calls it already carried are kept, along with their recordings and transcripts."
          width="max-w-[460px]"
          onClose={() => !confirming && setDeleteTarget(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={confirming}>
                Cancel
              </Button>
              <Button variant="danger" onClick={() => void confirmDelete()} disabled={confirming}>
                {confirming ? "Deleting…" : "Delete connection"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="text-[14px] font-semibold leading-5 text-ink">{deleteTarget.name}</div>
              <div className="mt-1 text-[13px] leading-5 text-muted">
                This cannot be undone — but the URL is derived from the platform and the agent, so a
                new connection for the same agent on the same platform gets the same URL back. To
                stop a URL for good, disable the connection or point it at a different agent.
              </div>
            </div>
          </div>
        </Modal>
      )}
    </AppShell>
  );
}
