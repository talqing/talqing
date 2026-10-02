"use client";

import Link from "next/link";
import type { TaskResponse, TaskSelection, VarDeclaration } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import {
  Badge,
  Button,
  CardHead,
  Field,
  Input,
  LIFT_ON_HOVER,
  Panel,
  Select,
} from "../../components/ui";

/** Jobs this agent can enter and come back from.
 *
 *  It sits between Tools and Handoffs because that is the order the three read
 *  in: what the agent can do, what it can hand out, where it can send the
 *  caller. The middle one is the new idea — a task borrows the floor, talks to
 *  the caller in the agent's own voice, produces a typed result and hands
 *  control back — so the section says that rather than assuming it.
 *
 *  Two things are shown as facts rather than judged here. The timeout, because
 *  on a voice agent it is literally how long the caller can be held and it is
 *  set on the task, not here. And the variable table, because the rule that
 *  decides it (a task variable the call already supplies is never shown to the
 *  model) is the one thing people get wrong — see `compiler/tools.py`'s
 *  `task_tool_schema`. */
export function TasksSection({
  tasks,
  agentVars,
  library,
  onChange,
}: {
  tasks: TaskSelection[];
  /** What this agent declares. A task variable matching one of these by name is
   *  filled from the call and never reaches the model. */
  agentVars: VarDeclaration[];
  /** Every task in the workspace, so one can be picked by name. */
  library: TaskResponse[];
  onChange: (next: TaskSelection[]) => void;
}) {
  const published = library.filter((t) => t.published_version);
  const declared = new Set(agentVars.map((v) => v.name));

  function edit(index: number, patch: Partial<TaskSelection>) {
    onChange(tasks.map((t, i) => (i === index ? { ...t, ...patch } : t)));
  }

  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      <CardHead
        title="Tasks"
        desc="Tasks are specialized agents that take control of conversation and return a structured output (for example: collect and return caller’s phone number)"
        className="mb-0"
      >
        {tasks.length > 0 && (
          <Badge>
            {tasks.length} {tasks.length === 1 ? "task" : "tasks"}
          </Badge>
        )}
        <Button
          variant="secondary"
          size="sm"
          onClick={() => onChange([...tasks, { name: "", task_id: "", description: "" }])}
        >
          Attach a task
        </Button>
      </CardHead>

      {tasks.length === 0 ? (
        <p className="-mt-1 text-[13px] leading-5 text-muted">
          A task is a second agent with a typed answer. It takes over the conversation for as long
          as it needs — speaking in this agent&rsquo;s own voice, on this call — collects what you
          asked for, and hands control back with its result. Use one for a bounded job inside a
          longer call: taking an address, qualifying a lead, walking someone through a form.
          {published.length === 0 && (
            <>
              {" "}
              <Link
                href="/tasks"
                className="text-ink underline-offset-2 hover:underline"
              >
                Build and publish a task
              </Link>{" "}
              first — only published tasks can be attached.
            </>
          )}
        </p>
      ) : (
        <div className="-mt-1 flex flex-col divide-y divide-line">
          {tasks.map((sel, index) => {
            const picked = published.find((t) => t.id === sel.task_id);
            const vars = picked?.config.vars ?? [];
            return (
              <div key={index} className="grid gap-2.5 py-3 first:pt-0 last:pb-0">
                {/* One 2x2 grid rather than two rows of their own, so the four
                    fields sit on two columns that line up all the way down.
                    Split across a flex row and a grid they did not: the flex row
                    carried the Remove button, so its two columns ended 42px
                    short of the grid's below them. Remove now lives on the meta
                    line, where it costs the fields no width. */}
                <div className="grid gap-x-3 gap-y-2.5 md:grid-cols-2">
                  <Field
                    label="Tool name"
                    className="min-w-0"
                    hint="What the model calls. Renaming the task does not change it."
                  >
                    <Input
                      value={sel.name}
                      placeholder="collect_shipping_address"
                      onChange={(e) => edit(index, { name: e.target.value })}
                    />
                  </Field>
                  <Field label="Task" className="min-w-0">
                    <Select
                      value={sel.task_id ?? ""}
                      onChange={(e) => edit(index, { task_id: e.target.value })}
                      searchable={published.length > 12}
                      aria-label="Task"
                    >
                      <option value="">Pick a task…</option>
                      {published.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.config.name} (v{t.published_version})
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field
                    label="Tool description"
                    className="min-w-0"
                    hint="What the model decides on — say when this job applies."
                  >
                    <Input
                      value={sel.description}
                      placeholder="Handoff to an agent task responsible for taking the caller's shipping address and confirming it back to them, once they've placed an order. Skip this if you already have their address."
                      onChange={(e) => edit(index, { description: e.target.value })}
                    />
                  </Field>
                  <Field
                    label="Initial message"
                    className="min-w-0"
                    hint="Spoken while the task loads. Optional."
                  >
                    <Input
                      value={sel.message ?? ""}
                      placeholder="Sure — let me take that down."
                      onChange={(e) => edit(index, { message: e.target.value || null })}
                    />
                  </Field>
                </div>

                {picked && vars.length > 0 && (
                  <div className="rounded-lg border border-line-2 bg-subtle px-3.5 py-2.5">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="text-[12px] font-medium text-ink">Inputs</span>
                      {/* Both pill values explained once, here, so the column
                          below can stay two words wide. */}
                      <span className="text-[12px] leading-[17px] text-faint">
                        filled from the call, or written by the model
                      </span>
                    </div>
                    {/* Three columns, not three flex rows. Every cell used to be
                        content-width, so the badges started at two different
                        x-positions and the descriptions at three — this is the
                        one place on the card the eye is meant to scan down a
                        column. `contents` keeps a keyed wrapper per variable
                        while letting its cells sit in the shared grid, and
                        `minmax(0,…)` keeps a long description inside its column
                        rather than widening the box. */}
                    <div className="mt-2 grid grid-cols-[auto_auto_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1.5 text-[12.5px] leading-5">
                      {vars.map((v) => {
                        const fromCall = declared.has(v.name);
                        return (
                          <div key={v.name} className="contents">
                            <span className="font-mono text-[12px] text-ink-soft">{v.name}</span>
                            {/* Both states are pills, so the column reads as one
                                control rather than two kinds of thing. The
                                neutral one keeps the default variant's own fill,
                                which is this well's colour — so it reads as a
                                quiet outline and the amber one carries all the
                                weight: that is the row where the model writes
                                the value. */}
                            <Badge variant={fromCall ? "default" : "warn"}>
                              {fromCall ? "from the call" : "from the model"}
                            </Badge>
                            {/* Wraps rather than truncates: the description is
                                the author's own sentence about the value, and a
                                taller row costs less than a hidden half. */}
                            <span className="text-muted">{v.description}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                <div className="flex flex-wrap items-center gap-2 text-[12.5px] text-muted">
                  {picked ? (
                    <>
                      <Badge>holds the caller up to {picked.config.timeout_seconds}s</Badge>
                      <Link
                        href={`/tasks/detail?id=${picked.id}`}
                        className="text-muted underline-offset-2 hover:text-ink hover:underline"
                      >
                        Open {picked.config.name} ↗
                      </Link>
                    </>
                  ) : sel.task_id ? (
                    <Badge variant="warn">this task is gone or unpublished</Badge>
                  ) : (
                    <span>pick a published task</span>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-auto"
                    onClick={() => onChange(tasks.filter((_, i) => i !== index))}
                    aria-label={`Remove ${sel.name || "task"}`}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
