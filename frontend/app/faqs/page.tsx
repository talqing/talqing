"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { FaqSummary } from "@talqing/sdk";
import type { MemberResponse } from "@/lib/control";
import { AppShell } from "../components/AppShell";
import { Creator, useOrgMembers } from "../components/Creator";
import { timeAgo } from "../components/diff";
import { FaqIcon, questionCount } from "../components/FaqsSection";
import { Button, EmptyState, Input, Label, Menu, Modal, Skeleton } from "../components/ui";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";

/* Rows and their header share one template so the columns line up. */
const ROW_GRID = "sm:grid-cols-[minmax(0,1fr)_minmax(150px,0.3fr)_36px]";

const PlusIcon = (
  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

function FaqRow({
  faq,
  members,
  onDelete,
}: {
  faq: FaqSummary;
  members: Map<string, MemberResponse>;
  onDelete: () => void;
}) {
  return (
    /* A div with a stretched link, so the overflow menu is not a button nested
       inside an anchor. Same construction as the tasks and tools lists. */
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      <Link
        href={`/faqs/detail?id=${faq.id}`}
        aria-label={`${faq.name} FAQ`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />
      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          <FaqIcon className="h-[18px] w-[18px]" />
        </span>
        <div className="min-w-0">
          <div className="truncate text-[13.5px] font-semibold leading-5 text-ink">{faq.name}</div>
          <div className="mt-0.5 truncate text-[12px] leading-4 text-muted">
            {faq.entry_count === 0 ? "No questions yet" : questionCount(faq.entry_count)}
          </div>
        </div>
      </div>
      <span className="flex min-w-0 items-center gap-1.5">
        <Creator userId={faq.created_by} members={members} className="relative z-10 flex-none" />
        <span className="truncate text-[11.5px] leading-4 text-faint">Edited {timeAgo(faq.updated_at)}</span>
      </span>
      <div className="relative z-10 flex justify-end">
        <Menu
          label={`More actions for ${faq.name}`}
          items={[
            { label: "Copy FAQ ID", onSelect: () => void navigator.clipboard.writeText(faq.id) },
            { label: "Delete FAQ", onSelect: onDelete, danger: true },
          ]}
        />
      </div>
    </div>
  );
}

/* Name it here, write it there — the shape agent, tool and task creation take. */
function NewFaqModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const trimmed = name.trim();

  async function create() {
    if (!trimmed || busy) return;
    setBusy(true);
    setError("");
    try {
      const faq = await api.createFaq(trimmed);
      router.push(`/faqs/detail?id=${faq.id}`);
    } catch (e) {
      setError(apiErrorMessage(e, "Could not create the FAQ."));
      setBusy(false);
    }
  }

  return (
    <Modal
      title="New FAQ"
      onClose={onClose}
      width="max-w-[460px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={create} disabled={busy || !trimmed}>
            {busy ? "Creating…" : "Create and open"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor="new-faq-name">Name</Label>
        <Input
          id="new-faq-name"
          value={name}
          autoFocus
          maxLength={80}
          placeholder="Store FAQ"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void create();
            }
          }}
        />
        <p className="text-[12.5px] leading-5 text-muted">
          The agent sees this as the heading over its questions.
        </p>
        {error && (
          <div role="alert" className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger [overflow-wrap:anywhere]">
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}

export default function FaqsPage() {
  const region = useActiveRegion();
  const members = useOrgMembers();
  const [faqs, setFaqs] = useState<FaqSummary[] | null>(null);
  const [err, setErr] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<FaqSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState("");

  async function load() {
    try {
      setFaqs((await api.listFaqs()).items);
    } catch (error) {
      setErr(apiErrorMessage(error));
      setFaqs([]);
    }
  }
  useEffect(() => {
    void load();
  }, []);

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteErr("");
    try {
      await api.deleteFaq(deleteTarget.id);
      setDeleteTarget(null);
      await load();
    } catch (error) {
      // The server refuses an FAQ an agent or task still attaches, and names them.
      setDeleteErr(apiErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }
  function closeDelete() {
    setDeleteTarget(null);
    setDeleteErr("");
  }

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line py-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="font-display text-[26px] font-semibold leading-8 tracking-tight text-ink">FAQs</h1>
            <p className="mt-1.5 max-w-[76ch] text-[14px] leading-5 text-muted">
              Questions people ask, with the answers you want given. Attach one to an agent or a
              task and it answers from your text. Edits go live without publishing.
            </p>
          </div>
          <Button onClick={() => setCreating(true)} className="min-h-[38px] self-start sm:self-auto">
            {PlusIcon}
            New FAQ
          </Button>
        </header>

        {err && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {faqs === null ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            {[0, 1, 2].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <Skeleton className="h-9 w-9 rounded-[10px]" />
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[26%]" />
                  <Skeleton className="mt-2 h-3 w-[14%]" />
                </div>
              </div>
            ))}
          </div>
        ) : faqs.length === 0 ? (
          !err && (
            <EmptyState
              icon={<FaqIcon className="h-[22px] w-[22px]" />}
              title={`No FAQs in ${region.name}`}
              body="Opening hours, refund policy, delivery areas: write each answer once and every agent you attach it to gives it the same way."
              cta={
                <Button onClick={() => setCreating(true)}>
                  {PlusIcon}
                  Create your first FAQ
                </Button>
              }
            />
          )
        ) : (
          <section className="min-w-0 overflow-hidden rounded-xl border border-line-2 bg-white">
            <div className={cn("hidden gap-x-4 border-b border-line px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid", ROW_GRID)}>
              <span>FAQ</span>
              <span>Last edited</span>
              <span className="sr-only">Actions</span>
            </div>
            {faqs.map((faq) => (
              <FaqRow key={faq.id} faq={faq} members={members} onDelete={() => setDeleteTarget(faq)} />
            ))}
          </section>
        )}
      </div>

      {creating && <NewFaqModal onClose={() => setCreating(false)} />}

      {deleteTarget && (
        <Modal
          title="Delete FAQ?"
          sub="Its questions and answers are gone for good."
          width="max-w-[460px]"
          onClose={() => !deleting && closeDelete()}
          footer={
            <>
              <Button variant="secondary" onClick={closeDelete} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDelete} disabled={deleting || !!deleteErr}>
                {deleting ? "Deleting…" : "Delete FAQ"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="text-[13px] font-semibold leading-5 text-ink">{deleteTarget.name}</div>
              <div className="mt-0.5 text-[12.5px] leading-5 text-muted">{questionCount(deleteTarget.entry_count)}</div>
            </div>
            {deleteErr && (
              <div role="alert" className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                {deleteErr}
              </div>
            )}
          </div>
        </Modal>
      )}
    </AppShell>
  );
}
