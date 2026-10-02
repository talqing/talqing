"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import type { AgentResponse, JsonObject, ToolResponse } from "@talqing/sdk";
import type { MemberResponse } from "@/lib/control";
import { AppShell } from "../components/AppShell";
import { Creator, useOrgMembers } from "../components/Creator";
import { Badge, Button, EmptyState, Input, Menu, Modal, Segment, Skeleton } from "../components/ui";
import { timeAgo } from "../components/diff";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { NewToolModal } from "./NewToolModal";

/* Rows and their header share one template so the columns line up. The
   signature is what tells two tools apart, so it keeps its width and everything
   else gives way: below a wide window "used by" drops entirely rather than
   squeezing a name down to `check_availabi…`. */
const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(124px,0.26fr)_36px] lg:grid-cols-[minmax(0,1fr)_minmax(160px,0.32fr)_minmax(124px,0.24fr)_36px]";

const MAX_SIGNATURE_ARGS = 4;

const Icon = {
  plus: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  search: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  ),
  tool: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8 7.2 20l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.2-.4-.4-2.2 2.6-2.6Z" />
    </svg>
  ),
};

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/* ── what a tool is, at a glance ────────────────────────────────────────────
   A tool is a function the model decides to call, so the row leads with what the
   model is shown: the name, its arguments, and the sentence describing when to
   reach for it. Everything below derives that from what the list endpoint
   already returns — no extra request per row. */

/** The arguments the model fills in, as `json_schema` stores them. */
function signatureArgs(schema: Record<string, unknown>): { name: string; required: boolean }[] {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return [];
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.keys(properties).map((name) => ({ name, required: required.includes(name) }));
}

/** `(date, time, party_size?)` — optional arguments carry the `?` they carry in
    every other typed language, and sort last so the truncation below always
    keeps the arguments the model must supply. Declaration order is not stable
    to begin with: `json_schema` is stored as JSONB, which reorders keys.

    A long list is cut rather than wrapped — the row is a summary, and the full
    schema is one click away. */
function signature(schema: Record<string, unknown>): string {
  const args = signatureArgs(schema);
  if (args.length === 0) return "()";
  const ordered = [...args.filter((a) => a.required), ...args.filter((a) => !a.required)];
  const shown = ordered
    .slice(0, MAX_SIGNATURE_ARGS)
    .map((arg) => `${arg.name}${arg.required ? "" : "?"}`);
  if (ordered.length > MAX_SIGNATURE_ARGS) shown.push("…");
  return `(${shown.join(", ")})`;
}

/** Agent names by tool id. An agent references a tool either by attaching it or
    by wiring it to a lifecycle hook, and both mean the same thing here: delete
    this tool and that agent stops publishing. */
function usageByTool(agents: AgentResponse[]): Record<string, string[]> {
  const byTool: Record<string, string[]> = {};
  for (const agent of agents) {
    /* Inline tools have no id and no page to link to, so they drop out. */
    const ids = [
      ...(agent.config.tools ?? []),
      agent.config.on_enter,
      agent.config.on_exit,
      agent.config.on_user_turn_completed,
    ]
      .map((sel) => sel?.tool_id)
      .filter((id): id is string => Boolean(id));
    for (const id of Array.from(new Set(ids))) {
      (byTool[id] ??= []).push(agent.config.name || "Untitled agent");
    }
  }
  return byTool;
}

/** True when the draft has moved on from the version agents are calling. */
function hasDraftChanges(tool: ToolResponse): boolean {
  if (!tool.published_at) return false;
  return new Date(tool.updated_at).getTime() > new Date(tool.published_at).getTime();
}

/** The next free `<name>_copy` — duplicating twice should not need a rename. */
function copyName(base: string, taken: Set<string>): string {
  let candidate = `${base}_copy`;
  for (let n = 2; taken.has(candidate); n += 1) candidate = `${base}_copy_${n}`;
  return candidate.slice(0, 60);
}

/* ── page furniture ─────────────────────────────────────────────────────── */

type Filter = "all" | "live" | "draft" | "edited";

function ToolRow({
  tool,
  usedBy,
  members,
  onCopyId,
  onDuplicate,
  onDelete,
}: {
  tool: ToolResponse;
  usedBy: string[];
  members: Map<string, MemberResponse>;
  onCopyId: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const drafted = hasDraftChanges(tool);
  const flags = [
    tool.silent && "silent",
    tool.long_running_task && "long-running",
    tool.disable_interruptions && "uninterruptible",
  ].filter(Boolean);

  return (
    /* Same construction as the agents list: a div with a stretched link, so the
       overflow menu is not a button nested inside an anchor. */
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2.5 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      <Link
        href={`/tools/detail?id=${tool.id}`}
        aria-label={`${tool.name} tool`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />

      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          {Icon.tool}
        </span>
        <div className="min-w-0">
          {/* The signature, because that is literally what the model is shown
              when it decides whether to call this. */}
          <div className="truncate font-mono text-[13.5px] leading-5">
            <span className="font-semibold text-ink">{tool.name}</span>
            <span className="text-muted">{signature(tool.json_schema)}</span>
          </div>
          {/* The description is the only prompt the model gets for when to reach
              for this tool, so it holds the line and the behaviour flags beside
              it are the first thing dropped on a narrow window. */}
          <div className="mt-0.5 flex min-w-0 items-center gap-2 overflow-hidden text-[12px] leading-4">
            {tool.description ? (
              <span className="truncate text-muted">{tool.description}</span>
            ) : (
              /* An empty description is a quality bug, not a blank. */
              <span className="truncate text-warn">
                No description — the model has only the name to go on
              </span>
            )}
            {flags.length > 0 && (
              <span className="hidden flex-none items-center gap-2 lg:flex">
                <span aria-hidden className="text-line-strong">|</span>
                <span className="text-faint">{flags.join(" · ")}</span>
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="hidden min-w-0 lg:block">
        {/* One agent is named outright; the count only earns its line once
            there is more than one thing for it to count. */}
        {usedBy.length === 0 ? (
          <span className="text-[13px] leading-5 text-faint">—</span>
        ) : usedBy.length === 1 ? (
          <div className="truncate text-[13px] leading-5 text-ink" title={usedBy[0]}>
            {usedBy[0]}
          </div>
        ) : (
          <>
            <div className="text-[13px] font-medium leading-5 text-ink tabular-nums">
              {plural(usedBy.length, "agent")}
            </div>
            <div className="truncate text-[11.5px] leading-4 text-faint" title={usedBy.join(", ")}>
              {usedBy.join(", ")}
            </div>
          </>
        )}
      </div>

      {/* Who and when are one fact, so they share a line — on its own the avatar
          reads as a stray dot and pushes the row half a line taller. */}
      <div className="flex min-w-0 flex-col items-start gap-1.5">
        {tool.published_version ? (
          <Badge variant="live" dot>Live v{tool.published_version}</Badge>
        ) : (
          <Badge>Draft</Badge>
        )}
        <span className="flex min-w-0 items-center gap-1.5">
          <Creator userId={tool.created_by} members={members} className="relative z-10 flex-none" />
        {/* Three facts compete for this line and only the most urgent is worth
            reading. A tool with no operations cannot be published at all; a
            draft that has moved past its live version is what agents are not
            calling yet; otherwise, when it was last touched. */}
        {tool.operations.length === 0 ? (
          <span className="truncate text-[11.5px] leading-4 text-warn">No operations yet</span>
        ) : drafted ? (
          <span className="truncate text-[11.5px] leading-4 text-warn">
            Edited since v{tool.published_version}
          </span>
        ) : (
          <span className="truncate text-[11.5px] leading-4 text-faint">
            Edited {timeAgo(tool.updated_at)}
          </span>
        )}
        </span>
      </div>

      <div className="relative z-10 flex justify-end">
        <Menu
          label={`More actions for ${tool.name}`}
          items={[
            { label: "Copy tool ID", onSelect: onCopyId },
            { label: "Duplicate", onSelect: onDuplicate },
            { label: "Delete tool", onSelect: onDelete, danger: true },
          ]}
        />
      </div>
    </div>
  );
}

export default function ToolsPage() {
  const region = useActiveRegion();
  const router = useRouter();
  const [tools, setTools] = useState<ToolResponse[] | null>(null);
  const [usage, setUsage] = useState<Record<string, string[]>>({});
  const members = useOrgMembers();
  const [err, setErr] = useState("");

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ToolResponse | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState("");

  async function loadTools() {
    try {
      setTools((await api.listTools()).items);
    } catch (error) {
      setErr(apiErrorMessage(error));
      setTools([]);
    }
  }

  useEffect(() => {
    loadTools();
    // Who uses each tool decorates the list; a failure leaves that column
    // showing "—" rather than taking the page down with it.
    api
      .listAllAgents()
      .then((agents) => setUsage(usageByTool(agents)))
      .catch(() => {});
  }, []);

  async function duplicate(tool: ToolResponse) {
    setErr("");
    try {
      const copy = await api.createTool({
        name: copyName(tool.name, new Set((tools ?? []).map((t) => t.name))),
        description: tool.description,
        json_schema: tool.json_schema,
        operations: tool.operations,
        long_running_task: tool.long_running_task,
        silent: tool.silent,
        disable_interruptions: tool.disable_interruptions,
      });
      router.push(`/tools/detail?id=${copy.id}`);
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteErr("");
    try {
      await api.deleteTool(deleteTarget.id);
      setDeleteTarget(null);
      await loadTools();
    } catch (error) {
      // Inside the dialog, not on the page behind it: the server refuses a tool
      // an agent still references, and that answer belongs where the button is.
      setDeleteErr(apiErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }

  const liveCount = tools?.filter((tool) => tool.published_version).length ?? 0;
  const draftCount = (tools?.length ?? 0) - liveCount;
  const edited = useMemo(() => (tools ?? []).filter(hasDraftChanges), [tools]);

  /* Searching and filtering a list you can take in at a glance is chrome, not
     help. The controls appear at the size where scanning starts to cost. */
  const searchable = (tools?.length ?? 0) > 4;

  const visible = useMemo(() => {
    if (!searchable) return tools ?? [];
    const needle = query.trim().toLowerCase();
    return (tools ?? []).filter((tool) => {
      if (filter === "live" && !tool.published_version) return false;
      if (filter === "draft" && tool.published_version) return false;
      if (filter === "edited" && !hasDraftChanges(tool)) return false;
      if (!needle) return true;
      return `${tool.name} ${tool.description}`.toLowerCase().includes(needle);
    });
  }, [tools, filter, query, searchable]);

  const loading = tools === null;
  const empty = tools !== null && tools.length === 0;
  const deleteUsage = deleteTarget ? (usage[deleteTarget.id] ?? []) : [];

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line py-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="font-display text-[26px] font-semibold leading-8 tracking-tight text-ink">Tools</h1>
            <p className="mt-1.5 max-w-[76ch] text-[14px] leading-5 text-muted">
              A function your agents can call mid-conversation. Publish it before attaching it to an
              agent — and again after every change.
            </p>
          </div>
          <Button onClick={() => setCreating(true)} className="min-h-[38px] self-start sm:self-auto">
            {Icon.plus}
            New tool
          </Button>
        </header>

        {err && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {loading ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            {[0, 1, 2, 3].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <Skeleton className="h-9 w-9 rounded-[10px]" />
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[30%]" />
                  <Skeleton className="mt-2 h-3 w-[46%]" />
                </div>
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : empty ? (
          <EmptyState
            icon={Icon.tool}
            title={`No tools in ${region.name}`}
            body="A tool is something your agent can do mid-conversation — call an API, run a few lines of TypeScript, hand the caller to another agent, hang up. Name one and lay out its steps."
            cta={
              <Button onClick={() => setCreating(true)}>
                {Icon.plus}
                Create your first tool
              </Button>
            }
          />
        ) : (
          <>
            {searchable && (
              <div className="mb-3 flex flex-wrap items-center gap-2.5">
                <div className="relative min-w-[180px] flex-1 sm:max-w-[280px]">
                  <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint">
                    {Icon.search}
                  </span>
                  <Input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search tools"
                    aria-label="Search tools by name or description"
                    className="pl-9"
                  />
                </div>
                <Segment
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "All", count: tools.length },
                    { value: "live", label: "Live", count: liveCount },
                    { value: "draft", label: "Drafts", count: draftCount },
                    // Offered only when it would leave you with something, so
                    // the control never advertises a state you are not in.
                    ...(edited.length > 0
                      ? [{ value: "edited" as const, label: "Edited", count: edited.length }]
                      : []),
                  ]}
                />
              </div>
            )}

            <section className="min-w-0 overflow-hidden rounded-xl border border-line-2 bg-white">
              {visible.length === 0 ? (
                <div className="px-4 py-12 text-center">
                  <p className="text-[14px] font-medium leading-5 text-ink">No tools match that</p>
                  <p className="mt-1 text-[13px] leading-5 text-muted">
                    Try a different name, or switch the filter back to All.
                  </p>
                </div>
              ) : (
                <>
                  <div
                    className={cn(
                      "hidden gap-x-4 border-b border-line px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
                      ROW_GRID,
                    )}
                  >
                    <span>Tool</span>
                    <span className="hidden lg:block">Used by</span>
                    <span>Status</span>
                    <span className="sr-only">Actions</span>
                  </div>
                  {visible.map((tool) => (
                    <ToolRow
                      key={tool.id}
                      tool={tool}
                      usedBy={usage[tool.id] ?? []}
                      members={members}
                      onCopyId={() => void navigator.clipboard.writeText(tool.id)}
                      onDuplicate={() => void duplicate(tool)}
                      onDelete={() => setDeleteTarget(tool)}
                    />
                  ))}
                </>
              )}
            </section>
          </>
        )}
      </div>

      {creating && (
        <NewToolModal
          defaultName=""
          title="New tool"
          sub="Name it here — the arguments and the operations it runs come next, on the tool page."
          onClose={() => setCreating(false)}
        />
      )}

      {deleteTarget && (
        <Modal
          title="Delete tool?"
          sub="This removes the draft, its operation tree, and every published version."
          width="max-w-[460px]"
          onClose={() => !deleting && (setDeleteTarget(null), setDeleteErr(""))}
          footer={
            <>
              <Button
                variant="secondary"
                onClick={() => { setDeleteTarget(null); setDeleteErr(""); }}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={confirmDelete}
                disabled={deleting || deleteUsage.length > 0}
              >
                {deleting ? "Deleting…" : "Delete tool"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="font-mono text-[13px] font-semibold leading-5 text-ink">
                {deleteTarget.name}
                <span className="font-normal text-muted">{signature(deleteTarget.json_schema)}</span>
              </div>
              <div className="mt-1 text-[13px] leading-5 text-muted">
                {deleteUsage.length === 0 ? (
                  "Not attached to any agent."
                ) : (
                  /* An agent version pins a tool version rather than carrying a
                     copy of it, so deleting the tool would take the pinned
                     definition out from under a live agent. The server refuses;
                     say so here rather than letting the button find out. */
                  <>
                    Used by {deleteUsage.join(", ")}. Detach it there first — a published agent runs
                    the tool version it was published with, and deleting the tool takes that version
                    with it.
                  </>
                )}
              </div>
            </div>
            {deleteErr && (
              <div className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                {deleteErr}
              </div>
            )}
          </div>
        </Modal>
      )}
    </AppShell>
  );
}
