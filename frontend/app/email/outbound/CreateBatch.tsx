"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  EmailBatchResponse,
  IntegrationResponse,
  TaskConfig,
  TaskResponse,
} from "@talqing/sdk";
import { Button, Field, Modal, Select, UnsavedChangesModal } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import {
  PolicyFields,
  type PolicyDraft,
  emptyDraft,
  policyProblems,
  toCreateRequest,
} from "./PolicyFields";
import { RecipientEditor, type ListCopy } from "./RecipientEditor";
import { EMPTY_LIST, type ParsedList, blankCount } from "./csv";

/* Creating a batch, over the list rather than instead of it.
 *
 * The screen is three questions in the order they have to be answered: what the
 * list is, what runs over it, and which column carries which part of the email.
 * The third cannot be asked before the first two, because its choices are the
 * union of the CSV's headers and the task's output fields — which is the whole
 * design in one control. */

const MAPPED: { key: "to" | "subject" | "body"; label: string; hint: string }[] = [
  { key: "to", label: "To", hint: "The address. From your file, or something the task finds." },
  { key: "subject", label: "Subject", hint: "The subject line." },
  { key: "body", label: "Body", hint: "The message itself." },
];

/** Column names an operator almost always means, so the three selects start
 *  filled in rather than empty on the common case. */
const OBVIOUS: Record<string, string[]> = {
  to: ["email", "to", "email_address", "address", "work_email", "email_to"],
  subject: ["subject", "subject_line", "email_subject"],
  body: ["body", "message", "email_body", "text"],
};

/* The list starts on `email` because it is the usual case, but it is not
   reserved: an address may just as well be something the task goes and finds,
   so the column can be renamed, removed, or mapped to an output field instead. */
export const LIST_COPY: ListCopy = {
  starterColumns: ["email"],
  sample: {
    csv: "email,first_name,company\nasha@acme.com,Asha,Acme\nravi@globex.com,Ravi,Globex\n",
    fileName: "recipients-sample.csv",
  },
  composerNote: (
    <>
      Every column reaches the task as{" "}
      <code className="font-mono text-[12px] text-ink-soft">{"{{vars.name}}"}</code>, and can be
      the address, the subject or the body further down.
    </>
  ),
  uploadNote: (
    <>
      every column becomes a{" "}
      <code className="font-mono text-[12px] text-ink-soft">{"{{vars.name}}"}</code> the task can
      read.
    </>
  ),
  emptyNote:
    "Nothing on the list yet. Every row you add shows up below, exactly as the task will receive it, before a single draft is written.",
};

export function CreateBatch({
  tasks: everyTask,
  accounts,
  onCreated,
  onCancel,
}: {
  tasks: TaskResponse[];
  accounts: IntegrationResponse[];
  onCreated: (batch: EmailBatchResponse) => void;
  onCancel: () => void;
}) {
  /* Only tasks that have been PUBLISHED, because only those can draft a batch:
     the API refuses the rest with "publish 'X' before drafting a batch with it",
     and discovering that after filling in a whole three-step form is not a way
     to learn it. */
  const tasks = useMemo(
    () => everyTask.filter((t) => t.published_version != null),
    [everyTask],
  );
  const [draft, setDraft] = useState<PolicyDraft>(emptyDraft);
  const [list, setList] = useState<ParsedList>(EMPTY_LIST);
  const [fieldMap, setFieldMap] = useState({ to: "", subject: "", body: "" });
  const [confirming, setConfirming] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [errors, setErrors] = useState<string[]>([]);

  const task = tasks.find((t) => t.id === draft.taskId);

  /* The PUBLISHED definition, fetched, not `task.config` — which is the DRAFT.
     Every check on this screen has a twin on the server that reads the published
     version, so validating against the draft offered output fields nobody has
     approved and refused mappings that are perfectly valid. `TraceModal` reads
     a version's config the same way. */
  const [published, setPublished] = useState<TaskConfig | null>(null);
  useEffect(() => {
    setPublished(null);
    if (!task?.published_version) return;
    let stale = false;
    api
      .getTaskVersion(task.id, task.published_version)
      .then((v) => !stale && setPublished(v.config))
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [task?.id, task?.published_version]);

  const outputColumns = useMemo(
    () => (published?.output ?? []).map((f) => f.name),
    [published],
  );
  const stringOutputs = useMemo(
    () => new Set((published?.output ?? []).filter((f) => f.type === "string").map((f) => f.name)),
    [published],
  );
  /* The merged column space, and it is worth seeing rendered: a value may come
     from the file or from the task, and after that nothing downstream can tell
     which. */
  const allColumns = useMemo(
    () => [...list.columns, ...outputColumns],
    [list.columns, outputColumns],
  );
  const collisions = useMemo(
    () => list.columns.filter((c) => outputColumns.includes(c)),
    [list.columns, outputColumns],
  );
  /* A required variable with no column and no default fails every row before
     the model is reached, so it is refused here rather than at create. */
  const missingVars = useMemo(
    () =>
      (published?.vars ?? [])
        .filter((v) => v.required && v.default == null && !list.columns.includes(v.name))
        .map((v) => v.name),
    [published, list.columns],
  );
  const requiredBlanks = useMemo(
    () =>
      (published?.vars ?? [])
        .filter((v) => v.required && list.columns.includes(v.name))
        .map((v) => ({ name: v.name, blanks: blankCount(list.rows, v.name) }))
        .filter((v) => v.blanks > 0),
    [published, list],
  );

  // Preselect the obvious names once both halves of the space are known. Only
  // fills a blank select — never overwrites a choice somebody made.
  useEffect(() => {
    if (!allColumns.length) return;
    setFieldMap((current) => {
      const next = { ...current };
      for (const { key } of MAPPED) {
        if (next[key] && allColumns.includes(next[key])) continue;
        const guess = allColumns.find((c) => OBVIOUS[key].includes(c.toLowerCase()));
        next[key] = guess ?? "";
      }
      return next;
    });
  }, [allColumns]);

  const problems = useMemo(() => {
    const out = policyProblems(draft);
    if (!list.rows.length) out.push("Add at least one row to the list.");
    if (list.badColumns.length) out.push("Fix the column names flagged above.");
    if (collisions.length) {
      out.push(
        `“${collisions[0]}” is both a column in your file and something the task produces — rename one of them.`,
      );
    }
    if (missingVars.length) {
      out.push(`The task needs a “${missingVars[0]}” column and your file has none.`);
    }
    for (const { key, label } of MAPPED) {
      if (!fieldMap[key]) out.push(`Say which column carries the ${label.toLowerCase()}.`);
      else if (!allColumns.includes(fieldMap[key])) {
        out.push(`The ${label.toLowerCase()} came from “${fieldMap[key]}”, which is no longer on the list — pick another column.`);
      } else if (
        !list.columns.includes(fieldMap[key]) &&
        outputColumns.includes(fieldMap[key]) &&
        !stringOutputs.has(fieldMap[key])
      ) {
        out.push(`The task's “${fieldMap[key]}” is not text, so it cannot be the ${label.toLowerCase()}.`);
      }
    }
    return out;
  }, [draft, list, collisions, missingVars, fieldMap, allColumns, outputColumns, stringOutputs]);

  const started = list.rows.length > 0 || !!draft.name.trim() || !!draft.taskId;

  async function submit() {
    setSaving(true);
    setErr("");
    setErrors([]);
    try {
      const batch = await api.createEmailBatch(
        toCreateRequest(draft, fieldMap, list.rows.map((input) => ({ input }))),
      );
      onCreated(batch);
    } catch (error: unknown) {
      const reported = apiErrorList(error);
      setErrors(reported);
      setErr(reported.length ? "" : apiErrorMessage(error));
      setConfirming(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="New email batch"
      sub="Build the list, pick the task that writes each email, and say which column carries what. Nothing is sent until you review the drafts."
      onClose={() => (started ? setDiscarding(true) : onCancel())}
      width="max-w-[920px]"
    >
      <section>
        <Step
          n={1}
          title="The list"
          sub="Type the rows in, upload a CSV, or both — every column becomes a variable the task can read."
        />
        <RecipientEditor list={list} onChange={setList} copy={LIST_COPY} />
      </section>

      <section className="mt-7 border-t border-line pt-6">
        <Step
          n={2}
          title="How it runs"
          sub="Which task writes each email, which account sends it, and when the drafting happens."
        />
        <PolicyFields draft={draft} onChange={setDraft} tasks={tasks} accounts={accounts} />
      </section>

      <section className="mt-7 border-t border-line pt-6">
        <Step
          n={3}
          title="The email"
          sub="Each part can come from your file or from the task — a row holds both side by side."
        />
        {task ? (
          <>
            <div className="grid gap-4 sm:grid-cols-3">
              {MAPPED.map(({ key, label, hint }) => (
                <Field key={key} label={label} hint={hint}>
                  <Select
                    value={fieldMap[key]}
                    onChange={(e) => setFieldMap({ ...fieldMap, [key]: e.target.value })}
                    aria-label={`Column carrying the ${label.toLowerCase()}`}
                  >
                    <option value="">Choose a column</option>
                    {list.columns.length > 0 && (
                      <optgroup label="From your file">
                        {list.columns.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {outputColumns.length > 0 && (
                      <optgroup label={`From ${task.config.name}`}>
                        {outputColumns.map((c) => (
                          <option key={c} value={c} disabled={!stringOutputs.has(c)}>
                            {c}
                            {stringOutputs.has(c) ? "" : " (not text)"}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </Select>
                </Field>
              ))}
            </div>
            {collisions.length > 0 && (
              <p className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[13px] leading-5 text-danger">
                <span className="font-mono">{collisions.join(", ")}</span>{" "}
                {collisions.length === 1 ? "is" : "are"} both a column in your file and something{" "}
                {task.config.name} produces. A row holds both side by side, so one of the two would
                be unreachable — rename the column, or rename the output field.
              </p>
            )}
            {missingVars.length > 0 && (
              <p className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[13px] leading-5 text-danger">
                {task.config.name} needs a value for{" "}
                <span className="font-mono">{missingVars.join(", ")}</span> and your file has no
                such column. Add it, or give the variable a default in the task editor.
              </p>
            )}
            {requiredBlanks.length > 0 && (
              <p className="mt-3 rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-2 text-[13px] leading-5 text-ink-soft">
                {requiredBlanks
                  .map((v) => `${v.blanks} ${v.blanks === 1 ? "row has" : "rows have"} no ${v.name}`)
                  .join(", ")}
                . Those rows will fail on their own without spending anything — the rest draft
                normally, and you can fill the cells in and redraft them, which runs the task on
                what you typed.
              </p>
            )}
          </>
        ) : (
          <p className="rounded-xl border border-line-2 bg-white px-4 py-5 text-center text-[13px] text-muted">
            Choose a task above, and the columns it produces appear here beside your own.
          </p>
        )}
      </section>

      {(err || errors.length > 0) && (
        <div className="mt-4 rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
          {err || (
            <ul className="grid gap-1">
              {errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="sticky bottom-0 -mx-6 mt-6 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line bg-surface px-6 py-3.5">
        <p className="min-w-0 flex-1 text-[13px] leading-5 text-muted">
          {problems.length ? (
            problems[0]
          ) : (
            <>
              <span className="font-semibold text-ink tabular-nums">
                {list.rows.length.toLocaleString()} drafts
              </span>{" "}
              written by {task?.config.name}, ready for you to review. Nothing is sent yet.
            </>
          )}
        </p>
        <Button variant="secondary" onClick={() => (started ? setDiscarding(true) : onCancel())}>
          Cancel
        </Button>
        <Button onClick={() => setConfirming(true)} disabled={problems.length > 0}>
          Review and start drafting
        </Button>
      </div>

      {discarding && (
        <UnsavedChangesModal
          sub="Nothing has been drafted, and this list and its settings are not saved anywhere."
          onKeepEditing={() => setDiscarding(false)}
          onDiscard={onCancel}
        />
      )}

      {confirming && (
        <Modal
          title="Start drafting?"
          onClose={() => !saving && setConfirming(false)}
          width="max-w-[480px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirming(false)} disabled={saving}>
                Back
              </Button>
              <Button onClick={submit} disabled={saving}>
                {saving ? "Starting…" : "Start drafting"}
              </Button>
            </>
          }
        >
          {/* Two lines, and they are the only two a person reads before pressing:
              what this costs, and that it mails nobody. Everything else the long
              version used to say is already on the form it is covering — the
              billing note under Drafting pace, the published-version note on the
              Task field — and repeating it here bought nothing except the length
              that made all of it skippable. */}
          <div className="grid gap-2.5 text-[14px] leading-6 text-ink-soft">
            <p>
              <strong className="font-semibold text-ink">
                {list.rows.length.toLocaleString()} runs of {task?.config.name}
              </strong>
              , one per row, billed to your own provider keys. You can pause at any point.
            </p>
            <p>
              <strong className="font-semibold text-ink">Nothing is emailed.</strong> You review
              the drafts and pick which ones go.
            </p>
          </div>
        </Modal>
      )}
    </Modal>
  );
}

export function Step({ n, title, sub }: { n: number; title: string; sub: React.ReactNode }) {
  return (
    <div className="mb-3.5 flex items-start gap-3">
      <span
        aria-hidden
        className="grid h-6 w-6 flex-none place-items-center rounded-full border border-line-2 bg-white font-mono text-[11.5px] font-medium text-ink-soft"
      >
        {n}
      </span>
      <div className="min-w-0">
        <h2 className="text-[14.5px] font-semibold leading-6 text-ink">{title}</h2>
        <p className="text-[13px] leading-5 text-muted">{sub}</p>
      </div>
    </div>
  );
}
