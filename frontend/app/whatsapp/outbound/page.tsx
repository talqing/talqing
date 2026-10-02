"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { AgentResponse, IntegrationResponse, WhatsAppBatchResponse } from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { DeleteBatchModal } from "@/app/components/DeleteBatchModal";
import {
  Badge,
  btn,
  Button,
  Container,
  EmptyState,
  Menu,
  PageHead,
  Skeleton,
  Tooltip,
} from "@/app/components/ui";
import { api, talqing } from "@/lib/api";
import { useActiveRegion } from "@/lib/regions";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { timeAgo } from "@/app/telephony/outbound-calling/shared";
import { CreateBatch } from "./CreateBatch";
import { WhatsAppIcon } from "./icons";
import { Progress } from "./Progress";
import {
  BATCH_STATUS_LABEL,
  BATCH_STATUS_VARIANT,
  formatPhone,
  isLive,
  pausedItself,
  whatsAppSenders,
} from "./shared";

const PAGE_SIZE = 50;

const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(220px,0.5fr)_minmax(112px,0.22fr)_36px]";

export default function WhatsAppOutboundPage() {
  const region = useActiveRegion();
  const router = useRouter();
  const [creating, setCreating] = useState(false);
  const [batches, setBatches] = useState<WhatsAppBatchResponse[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [senders, setSenders] = useState<IntegrationResponse[] | null>(null);
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [err, setErr] = useState("");
  const [deleting, setDeleting] = useState<WhatsAppBatchResponse | null>(null);

  const load = useCallback((quiet = false) => {
    if (!quiet) setBatches(null);
    talqing.whatsapp.batches
      .list({ limit: PAGE_SIZE })
      .then((page) => {
        setBatches(page.items);
        setHasMore(page.has_more);
        setErr("");
      })
      .catch((e) => {
        setBatches([]);
        setErr(apiErrorMessage(e, "Could not load your batches."));
      });
  }, []);

  useEffect(() => load(), [load]);
  useEffect(() => {
    Promise.all([api.listIntegrations(), api.listAllAgents()])
      .then(([integrations, allAgents]) => {
        setSenders(whatsAppSenders(integrations.items));
        setAgents(allAgents);
      })
      .catch(() => setSenders([]));
  }, []);

  // Only while something is going out.
  const anySending = (batches ?? []).some((b) => isLive(b.status));
  useEffect(() => {
    if (!anySending || creating) return;
    const timer = window.setInterval(() => load(true), 5000);
    return () => window.clearInterval(timer);
  }, [anySending, creating, load]);

  async function loadMore() {
    try {
      const page = await talqing.whatsapp.batches.list({
        limit: PAGE_SIZE,
        offset: (batches ?? []).length,
      });
      setBatches((current) => [...(current ?? []), ...page.items]);
      setHasMore(page.has_more);
    } catch (e) {
      setErr(apiErrorMessage(e, "Could not load more batches."));
    }
  }

  const noSender = senders !== null && !senders.some((s) => s.status === "active");
  const all = batches ?? [];

  return (
    <AppShell>
      <Container>
        <PageHead
          title="WhatsApp"
          sub="Send an approved template to everyone on a list. Replies go to the number's agent, which knows the row it came from."
          actions={
            noSender ? (
              <Tooltip label="Connect a WhatsApp number under Numbers first." className="cursor-not-allowed">
                <Button disabled>New batch</Button>
              </Tooltip>
            ) : (
              <Button onClick={() => setCreating(true)} disabled={senders === null}>
                New batch
              </Button>
            )
          }
        />

        {err && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {batches === null ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            {[0, 1, 2].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <Skeleton className="h-9 w-9 rounded-[10px]" />
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[24%]" />
                  <Skeleton className="mt-2 h-3 w-[36%]" />
                </div>
                <Skeleton className="h-1.5 w-40 rounded-full" />
              </div>
            ))}
          </div>
        ) : all.length === 0 ? (
          <EmptyState
            icon={<WhatsAppIcon className="h-[22px] w-[22px]" />}
            title={`No WhatsApp batches in ${region.name}`}
            body={
              noSender
                ? "Batches send from a WhatsApp number you have at Twilio or Gupshup. Connect one first."
                : "Upload a list, pick an approved template, and check every row before anything goes out."
            }
            cta={
              noSender ? (
                <Link href="/whatsapp/numbers?connect=1" className={btn("primary", "sm")}>
                  Connect WhatsApp
                </Link>
              ) : (
                <Button onClick={() => setCreating(true)} disabled={senders === null}>
                  Create your first batch
                </Button>
              )
            }
          />
        ) : (
          <>
            <section className="min-w-0 overflow-hidden rounded-xl border border-line-2 bg-white">
              <div
                className={cn(
                  "hidden gap-x-4 border-b border-line px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
                  ROW_GRID,
                )}
              >
                <span>Batch</span>
                <span>Progress</span>
                <span>Status</span>
                <span className="sr-only">Actions</span>
              </div>
              {all.map((batch) => (
                <BatchRow key={batch.id} batch={batch} onDelete={() => setDeleting(batch)} />
              ))}
            </section>
            {hasMore && (
              <Button variant="secondary" onClick={loadMore} className="mt-3">
                Load more
              </Button>
            )}
          </>
        )}
      </Container>

      {deleting && (
        <DeleteBatchModal
          run={() => talqing.whatsapp.batches.delete({ batch_id: deleting.id })}
          onDeleted={() => {
            setBatches((current) => (current ?? []).filter((b) => b.id !== deleting.id));
            setDeleting(null);
          }}
          onClose={() => setDeleting(null)}
        >
          <p>
            <span className="font-semibold text-ink">{deleting.name}</span> and its{" "}
            {deleting.counts.total.toLocaleString()} {deleting.counts.total === 1 ? "row" : "rows"}{" "}
            are deleted for good, with who was messaged and every delivery and reply count.
          </p>
          <p>The conversations it started stay.</p>
        </DeleteBatchModal>
      )}
      {creating && senders && (
        <CreateBatch
          senders={senders}
          agents={agents}
          onCancel={() => setCreating(false)}
          onCreated={(batch) => {
            setCreating(false);
            router.push(`/whatsapp/outbound/detail?id=${batch.id}`);
          }}
        />
      )}
    </AppShell>
  );
}

function BatchRow({ batch, onDelete }: { batch: WhatsAppBatchResponse; onDelete: () => void }) {
  const router = useRouter();
  // A batch still scheduled, sending or paused has to be canceled before it can go.
  const live = isLive(batch.status) || batch.status === "paused";
  return (
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2.5 border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      <Link
        href={`/whatsapp/outbound/detail?id=${batch.id}`}
        aria-label={`Open ${batch.name}`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />
      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          <WhatsAppIcon />
        </span>
        <div className="min-w-0">
          <div className="truncate font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
            {batch.name}
          </div>
          {pausedItself(batch) || (batch.status === "failed" && batch.failure_reason) ? (
            <div className={cn("mt-0.5 truncate text-[12px] leading-4", batch.status === "failed" ? "text-danger" : "text-warn")}>
              {batch.failure_reason}
            </div>
          ) : (
            <div className="mt-0.5 flex min-w-0 items-center gap-2 overflow-hidden text-[12px] leading-4 text-muted">
              <span className="truncate font-mono text-[11.5px]">{batch.template.name}</span>
              <span aria-hidden className="text-line-strong">·</span>
              <span className="truncate">
                {batch.sender_e164 ? formatPhone(batch.sender_e164) : "Sender disconnected"}
              </span>
            </div>
          )}
        </div>
      </div>
      <div className="hidden sm:block">
        <Progress batch={batch} compact />
      </div>
      <div className="flex flex-col items-start gap-1.5">
        <Badge variant={BATCH_STATUS_VARIANT[batch.status]} dot={batch.status === "sending"}>
          {BATCH_STATUS_LABEL[batch.status]}
        </Badge>
        <span className="text-[11.5px] leading-4 text-faint">Created {timeAgo(batch.created_at)}</span>
      </div>
      {/* Above the row's link, which covers everything else. */}
      <div className="relative z-10 flex justify-end">
        <Menu
          label={`Actions for ${batch.name}`}
          items={[
            { label: "Open", onSelect: () => router.push(`/whatsapp/outbound/detail?id=${batch.id}`) },
            {
              label: live ? "Delete batch (cancel it first)" : "Delete batch",
              onSelect: onDelete,
              danger: true,
              disabled: live,
            },
          ]}
        />
      </div>
    </div>
  );
}
