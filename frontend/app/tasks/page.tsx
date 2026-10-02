"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import type { TaskResponse } from "@talqing/sdk";
import type { MemberResponse } from "@/lib/control";
import { AppShell } from "../components/AppShell";
import { Creator, useOrgMembers } from "../components/Creator";
import { Badge, Button, EmptyState, Input, Menu, Modal, Segment, Skeleton } from "../components/ui";
import { timeAgo } from "../components/diff";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { NewTaskModal } from "./NewTaskModal";
import { runOutcome } from "./detail/outcome";

/* Rows and their header share one template so the columns line up. What the
   task produces is the fact that tells two apart, so it keeps its width; the
   model gives way first, then the last run. */
const ROW_GRID =
  "sm:grid-cols-[minmax(0,1fr)_minmax(150px,0.34fr)_36px] lg:grid-cols-[minmax(0,1fr)_minmax(150px,0.26fr)_minmax(170px,0.30fr)_36px]";

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
  task: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M8 4H6.5A2.5 2.5 0 0 0 4 6.5v11A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5V16" />
      <path d="M8.5 3.5h7v2.2a1 1 0 0 1-1 1h-5a1 1 0 0 1-1-1V3.5Z" />
      <path d="m13.5 12.5 2 2 5-5" />
    </svg>
  ),
};

type Filter = "all" | "live" | "draft";

/** `email → subject, body` — what goes in and what comes out, which is the
 *  whole of what a task is. Truncated rather than wrapped: the row is a
 *  summary and the full lists are one click away. */
function shape(task: TaskResponse): { inputs: string; outputs: string } {
  const vars = task.config.vars ?? [];
  const output = task.config.output ?? [];
  return {
    inputs: vars.length === 0 ? "no inputs" : vars.map((v) => v.name).join(", "),
    outputs: output.map((f) => f.name).join(", "),
  };
}

function TaskRow({
  task,
  members,
  onCopyId,
  onDelete,
}: {
  task: TaskResponse;
  members: Map<string, MemberResponse>;
  onCopyId: () => void;
  onDelete: () => void;
}) {
  const { inputs, outputs } = shape(task);
  const run = task.last_run;
  const outcome = runOutcome(run);

  return (
    /* A div with a stretched link, so the overflow menu is not a button nested
       inside an anchor. Same construction as the tools and agents lists. */
    <div
      className={cn(
        "group relative grid items-center gap-x-4 gap-y-2.5 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
      )}
    >
      <Link
        href={`/tasks/detail?id=${task.id}`}
        aria-label={`${task.config.name} task`}
        className="absolute inset-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ink/15"
      />

      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft transition-colors group-hover:border-line-2 group-hover:bg-white"
        >
          {Icon.task}
        </span>
        <div className="min-w-0">
          <div className="truncate text-[13.5px] font-semibold leading-5 text-ink">
            {task.config.name}
          </div>
          {/* The signature of the task: what it takes and what it hands back. */}
          <div className="mt-0.5 truncate font-mono text-[12px] leading-4 text-muted">
            {inputs} <span className="text-line-strong">→</span>{" "}
            <span className="text-ink-soft">{outputs}</span>
          </div>
        </div>
      </div>

      <div className="hidden min-w-0 lg:block">
        <div className="truncate text-[13px] leading-5 text-ink">
          {task.config.llm?.model ?? "—"}
        </div>
        <div className="truncate text-[11.5px] leading-4 text-faint">
          {task.config.llm?.provider ?? ""}
        </div>
      </div>

      {/* How it last went, then who owns it and when it was last touched — the
          two questions a list of tasks is actually asked. */}
      <div className="flex min-w-0 flex-col items-start gap-1.5">
        <span className="flex flex-wrap items-center gap-1.5">
          {task.published_version ? (
            <Badge variant="live" dot>Live v{task.published_version}</Badge>
          ) : (
            <Badge>Draft</Badge>
          )}
          {run ? (
            <Badge variant={outcome.badge} dot={run.status === "completed"} title={outcome.help}>
              {outcome.label}
            </Badge>
          ) : (
            <Badge>Never run</Badge>
          )}
        </span>
        <span className="flex min-w-0 items-center gap-1.5">
          <Creator userId={task.created_by} members={members} className="relative z-10 flex-none" />
          <span className="truncate text-[11.5px] leading-4 text-faint">
            {run ? `Ran ${timeAgo(run.started_at)}` : `Edited ${timeAgo(task.updated_at)}`}
          </span>
        </span>
      </div>

      <div className="relative z-10 flex justify-end">
        <Menu
          label={`More actions for ${task.config.name}`}
          items={[
            { label: "Copy task ID", onSelect: onCopyId },
            { label: "Delete task", onSelect: onDelete, danger: true },
          ]}
        />
      </div>
    </div>
  );
}

export default function TasksPage() {
  const region = useActiveRegion();
  const [tasks, setTasks] = useState<TaskResponse[] | null>(null);
  const members = useOrgMembers();
  const [err, setErr] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<TaskResponse | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState("");

  async function loadTasks() {
    try {
      setTasks((await api.listTasks()).items);
    } catch (error) {
      setErr(apiErrorMessage(error));
      setTasks([]);
    }
  }

  useEffect(() => {
    loadTasks();
  }, []);

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteErr("");
    try {
      await api.deleteTask(deleteTarget.id);
      setDeleteTarget(null);
      await loadTasks();
    } catch (error) {
      setDeleteErr(apiErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }

  const liveCount = tasks?.filter((task) => task.published_version).length ?? 0;
  const draftCount = (tasks?.length ?? 0) - liveCount;

  /* Searching and filtering a list you can take in at a glance is chrome, not
     help. The controls appear at the size where scanning starts to cost. */
  const searchable = (tasks?.length ?? 0) > 4;
  const visible = useMemo(() => {
    if (!searchable) return tasks ?? [];
    const needle = query.trim().toLowerCase();
    return (tasks ?? []).filter((task) => {
      if (filter === "live" && !task.published_version) return false;
      if (filter === "draft" && task.published_version) return false;
      if (!needle) return true;
      const { inputs, outputs } = shape(task);
      return `${task.config.name} ${inputs} ${outputs}`.toLowerCase().includes(needle);
    });
  }, [tasks, filter, query, searchable]);

  const loading = tasks === null;
  const empty = tasks !== null && tasks.length === 0;

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line py-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="font-display text-[26px] font-semibold leading-8 tracking-tight text-ink">Tasks</h1>
            <p className="mt-1.5 max-w-[76ch] text-[14px] leading-5 text-muted">
              One job with a typed answer. Run it on its own — give it values, let it use its
              tools, get the result back — or attach it to an agent and it takes over the
              conversation until the job is done. Publishing pins the version everything uses.
            </p>
          </div>
          <Button onClick={() => setCreating(true)} className="min-h-[38px] self-start sm:self-auto">
            {Icon.plus}
            New task
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
            icon={Icon.task}
            title={`No tasks in ${region.name}`}
            body="A task is an agent with nobody on the other end: hand it an email address and it researches the company, hand it a message and it tells you what the person wants. Name one and say what it should produce."
            cta={
              <Button onClick={() => setCreating(true)}>
                {Icon.plus}
                Create your first task
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
                    placeholder="Search tasks"
                    aria-label="Search tasks by name, input or output"
                    className="pl-9"
                  />
                </div>
                <Segment
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "All", count: tasks.length },
                    { value: "live", label: "Live", count: liveCount },
                    { value: "draft", label: "Drafts", count: draftCount },
                  ]}
                />
              </div>
            )}

            <section className="min-w-0 overflow-hidden rounded-xl border border-line-2 bg-white">
              {visible.length === 0 ? (
                <div className="px-4 py-12 text-center">
                  <p className="text-[14px] font-medium leading-5 text-ink">No tasks match that</p>
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
                    <span>Task</span>
                    <span className="hidden lg:block">Model</span>
                    <span>Status</span>
                    <span className="sr-only">Actions</span>
                  </div>
                  {visible.map((task) => (
                    <TaskRow
                      key={task.id}
                      task={task}
                      members={members}
                      onCopyId={() => void navigator.clipboard.writeText(task.id)}
                      onDelete={() => setDeleteTarget(task)}
                    />
                  ))}
                </>
              )}
            </section>
          </>
        )}
      </div>

      {creating && <NewTaskModal onClose={() => setCreating(false)} />}

      {deleteTarget && (
        <Modal
          title="Delete task?"
          sub="This removes the task itself. Its runs stay, still named after it."
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
              <Button variant="danger" onClick={confirmDelete} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete task"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="text-[13px] font-semibold leading-5 text-ink">
                {deleteTarget.config.name}
              </div>
              <div className="mt-1 font-mono text-[12.5px] leading-5 text-muted">
                {shape(deleteTarget).inputs} → {shape(deleteTarget).outputs}
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
