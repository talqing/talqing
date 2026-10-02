"use client";

import { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import type { FaqDetail, FaqEntryInput } from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { EditorSkeleton } from "@/app/components/EditorSkeleton";
import {
  Button,
  Container,
  Input,
  Menu,
  Modal,
  RowRemove,
  UnsavedChangesModal,
  useToast,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { useUnsavedChanges } from "@/lib/useUnsavedChanges";
import { parseFaqCsv } from "./csv";

/* The API's limits (`services/faqs/models.py`). Mirrored so a field stops at
   its limit rather than failing on save; the API stays the authority. */
const MAX_ENTRIES = 500;
const MAX_NAME = 80;
const MAX_QUESTION = 300;
const MAX_ANSWER = 2000;

/** One question/answer card. `id` is null until its first save; `saved` is what
 *  the server holds, which is what "dirty" is measured against. */
type Card = {
  key: string;
  id: string | null;
  question: string;
  answer: string;
  saved: { question: string; answer: string } | null;
  busy: boolean;
  error: string;
};

const isDirty = (card: Card): boolean =>
  card.saved === null ||
  card.question.trim() !== card.saved.question ||
  card.answer.trim() !== card.saved.answer;

const blankCard = (key: string): Card => ({
  key,
  id: null,
  question: "",
  answer: "",
  saved: null,
  busy: false,
  error: "",
});

/** A count that only appears once the limit is close enough to matter. */
function NearLimit({ length, max }: { length: number; max: number }) {
  if (length < max * 0.9) return null;
  return (
    <span className={cn("text-[12px] tabular-nums", length >= max ? "text-warn" : "text-faint")}>
      {length} / {max}
    </span>
  );
}

/* A cell of the sheet: text that reads as text and edits in place. Bare rather
   than the `Textarea` primitive, whose border and minimum height are what made
   every entry a form inside a box. Grows with its content, so a long answer is
   read whole instead of through a viewport. */
function Cell({
  value,
  onChange,
  onKeyDown,
  autoFocus,
  maxLength,
  placeholder,
  label,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  onKeyDown?: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  autoFocus?: boolean;
  maxLength: number;
  placeholder: string;
  label: string;
  className?: string;
}) {
  const el = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    if (!el.current) return;
    el.current.style.height = "auto";
    el.current.style.height = `${el.current.scrollHeight}px`;
  }, [value]);
  return (
    <textarea
      ref={el}
      rows={1}
      value={value}
      autoFocus={autoFocus}
      maxLength={maxLength}
      placeholder={placeholder}
      aria-label={label}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      className={cn(
        "block w-full resize-none overflow-hidden bg-transparent px-4 py-3.5 text-[14px] leading-[22px] text-ink placeholder:text-placeholder focus:outline-none",
        className,
      )}
    />
  );
}

/** Question on the left, answer on the right, one hairline between entries.
 *  The save strip shows only while the row differs from what is stored. */
const ROW_GRID = "grid grid-cols-1 sm:grid-cols-[minmax(0,0.4fr)_minmax(0,0.6fr)]";

function EntryRow({
  card,
  autoFocus,
  onEdit,
  onSave,
  onDelete,
}: {
  card: Card;
  autoFocus: boolean;
  onEdit: (patch: Partial<Pick<Card, "question" | "answer">>) => void;
  onSave: () => void;
  onDelete: () => void;
}) {
  const dirty = isDirty(card);
  const complete = card.question.trim() !== "" && card.answer.trim() !== "";
  const answer = useRef<HTMLDivElement>(null);

  function saveOnShortcut(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && dirty && complete && !card.busy) {
      event.preventDefault();
      onSave();
    }
  }

  return (
    <div className="group relative border-b border-line last:border-b-0">
      <div className={ROW_GRID}>
        <div className="transition-colors hover:bg-canvas/60 focus-within:bg-canvas">
          <Cell
            value={card.question}
            autoFocus={autoFocus}
            maxLength={MAX_QUESTION}
            placeholder="Question, the way a caller would ask it"
            label="Question"
            // On a phone the pair stacks: the question keeps clear of the delete
            // control and sits tight over its answer, so the pair reads as one.
            className="pb-1.5 pr-12 font-medium sm:pb-3.5 sm:pr-4"
            onChange={(question) => onEdit({ question })}
            onKeyDown={(event) => {
              // A question is one line of thought: Enter moves on to its answer.
              if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && !event.shiftKey) {
                event.preventDefault();
                answer.current?.querySelector("textarea")?.focus();
                return;
              }
              saveOnShortcut(event);
            }}
          />
        </div>
        <div
          ref={answer}
          className="transition-colors hover:bg-canvas/60 focus-within:bg-canvas sm:border-l sm:border-line"
        >
          <Cell
            value={card.answer}
            maxLength={MAX_ANSWER}
            placeholder="The answer to give"
            label="Answer"
            className="pt-0 text-ink-soft sm:pr-12 sm:pt-3.5"
            onChange={(answer) => onEdit({ answer })}
            onKeyDown={saveOnShortcut}
          />
        </div>
      </div>
      <RowRemove
        onClick={onDelete}
        disabled={card.busy}
        ariaLabel={card.id ? "Delete question" : "Discard question"}
        className="absolute right-2.5 top-3"
      />
      {(dirty || card.error) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line bg-canvas px-4 py-2">
          {card.error ? (
            <span role="alert" className="min-w-0 text-[12.5px] leading-5 text-danger [overflow-wrap:anywhere]">
              {card.error}
            </span>
          ) : (
            <span className="text-[12.5px] leading-5 text-muted">
              {card.saved ? "Unsaved changes" : "Not saved yet"}
            </span>
          )}
          <NearLimit length={card.question.length} max={MAX_QUESTION} />
          <NearLimit length={card.answer.length} max={MAX_ANSWER} />
          {dirty && (
            <div className="ml-auto flex flex-none items-center gap-2">
              {card.saved && (
                <Button variant="ghost" size="sm" onClick={() => onEdit(card.saved!)} disabled={card.busy}>
                  Revert
                </Button>
              )}
              <Button size="sm" onClick={onSave} disabled={!complete || card.busy}>
                {card.busy ? "Saving…" : "Save"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FaqEditorInner() {
  // Query param, not a path segment: the dashboard ships as a static export,
  // which cannot prerender an unbounded set of ids.
  const id = useSearchParams().get("id") ?? "";
  const router = useRouter();
  const toast = useToast();
  const [faq, setFaq] = useState<FaqDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [cards, setCards] = useState<Card[]>([]);
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState("");
  const [query, setQuery] = useState("");
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [deleteEntry, setDeleteEntry] = useState<Card | null>(null);
  const [deleteFaq, setDeleteFaq] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState("");
  const [csv, setCsv] = useState<
    { file: string; entries: FaqEntryInput[]; problem: string; errors: string[]; busy: boolean } | null
  >(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const nextKey = useRef(0);

  async function load() {
    try {
      const loaded = await api.getFaq(id);
      setFaq(loaded);
      setName(loaded.name);
      setCards(
        loaded.entries.map((e) => ({
          key: e.id,
          id: e.id,
          question: e.question,
          answer: e.answer,
          saved: { question: e.question, answer: e.answer },
          busy: false,
          error: "",
        })),
      );
    } catch (error) {
      setLoadError(apiErrorMessage(error, "Could not load this FAQ."));
    }
  }
  useEffect(() => {
    void load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps -- one load per FAQ id

  const unsaved = useUnsavedChanges(cards.some(isDirty));
  const savedCount = cards.filter((c) => c.id).length;
  const full = cards.length >= MAX_ENTRIES;

  /* Filtering a list you can take in at a glance is chrome, not help. */
  const searchable = savedCount > 6;
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!searchable || !needle) return cards;
    // An unsaved card always shows: hiding what someone is typing loses it.
    return cards.filter(
      (c) => c.saved === null || `${c.question} ${c.answer}`.toLowerCase().includes(needle),
    );
  }, [cards, query, searchable]);

  function patchCard(key: string, patch: Partial<Card>) {
    setCards((all) => all.map((c) => (c.key === key ? { ...c, ...patch } : c)));
  }

  function addCard() {
    const key = `new-${nextKey.current++}`;
    setCards((all) => [...all, blankCard(key)]);
    setFocusKey(key);
    setQuery("");
  }

  async function saveCard(card: Card) {
    const question = card.question.trim();
    const answer = card.answer.trim();
    patchCard(card.key, { busy: true, error: "" });
    try {
      const entry = card.id
        ? await api.updateFaqEntry(id, card.id, { question, answer })
        : (await api.createFaqEntries(id, [{ question, answer }])).entries[0];
      patchCard(card.key, {
        id: entry.id,
        question: entry.question,
        answer: entry.answer,
        saved: { question: entry.question, answer: entry.answer },
        busy: false,
      });
    } catch (error) {
      patchCard(card.key, { busy: false, error: apiErrorMessage(error, "Could not save this question.") });
    }
  }

  async function confirmDeleteEntry() {
    if (!deleteEntry?.id) return;
    setDeleting(true);
    setDeleteErr("");
    try {
      await api.deleteFaqEntry(id, deleteEntry.id);
      setCards((all) => all.filter((c) => c.key !== deleteEntry.key));
      setDeleteEntry(null);
    } catch (error) {
      setDeleteErr(apiErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }

  async function confirmDeleteFaq() {
    setDeleting(true);
    setDeleteErr("");
    try {
      await api.deleteFaq(id);
      router.push("/faqs");
    } catch (error) {
      // Refused while an agent or task attaches it; the message names them.
      setDeleteErr(apiErrorMessage(error));
      setDeleting(false);
    }
  }

  async function commitName() {
    const trimmed = name.trim();
    if (!faq || trimmed === faq.name) return;
    if (!trimmed) {
      setName(faq.name);
      return;
    }
    setNameError("");
    try {
      const renamed = await api.renameFaq(id, trimmed);
      setFaq({ ...faq, name: renamed.name });
      setName(renamed.name);
    } catch (error) {
      setNameError(apiErrorMessage(error, "Could not rename this FAQ."));
      setName(faq.name);
    }
  }

  async function pickCsv(file: File) {
    const parsed = parseFaqCsv(await file.text());
    setCsv({
      file: file.name,
      entries: "entries" in parsed ? parsed.entries : [],
      problem: "problem" in parsed ? parsed.problem : "",
      errors: [],
      busy: false,
    });
  }

  async function importCsv() {
    if (!csv) return;
    setCsv({ ...csv, busy: true });
    try {
      await api.createFaqEntries(id, csv.entries);
      setCsv(null);
      toast({ kind: "ok", msg: `Added ${csv.entries.length} question${csv.entries.length === 1 ? "" : "s"}.` });
      // Reload rather than append: the import landed after any unsaved cards.
      const unsavedCards = cards.filter((c) => c.saved === null);
      await load();
      setCards((all) => [...all, ...unsavedCards]);
    } catch (error) {
      // All or nothing on the server, so nothing was added. Shown as returned.
      setCsv({
        ...csv,
        busy: false,
        problem: apiErrorMessage(error, "Could not import this file."),
        errors: apiErrorList(error),
      });
    }
  }

  if (loadError) {
    return (
      <AppShell>
        <Container className="pt-6">
          <div role="alert" className="rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {loadError}
          </div>
          <Link href="/faqs" className="mt-3 inline-block text-[13.5px] text-muted underline underline-offset-2">
            Back to FAQs
          </Link>
        </Container>
      </AppShell>
    );
  }
  if (!faq) return <EditorSkeleton />;

  return (
    <AppShell>
      <div className="min-h-screen bg-surface text-ink">
        <Container className="min-h-screen">
          <div className="sticky top-0 z-30 -mx-6 border-b border-line bg-surface/90 px-6 backdrop-blur-md">
            <div className="flex min-h-[68px] flex-wrap items-center gap-x-3 gap-y-2 pb-3 pt-3">
              <Link
                href="/faqs"
                className="-ml-1.5 flex h-8 flex-none items-center gap-1 rounded-lg px-1.5 text-[13px] font-medium text-muted transition-colors hover:bg-hover hover:text-ink"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M14 6l-6 6 6 6" />
                </svg>
                FAQs
              </Link>
              <span aria-hidden className="h-4 w-px flex-none bg-line-2" />
              <input
                className="min-w-[200px] flex-1 basis-[260px] rounded-lg border border-transparent bg-transparent px-1.5 py-1 font-display text-[21px] font-semibold leading-7 tracking-[-0.015em] text-ink transition-colors hover:border-line-2 focus:border-ink focus:outline-none focus:ring-2 focus:ring-ink/10"
                value={name}
                maxLength={MAX_NAME}
                aria-label="FAQ name"
                onChange={(e) => setName(e.target.value)}
                onBlur={() => void commitName()}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                }}
              />
              <div className="ml-auto flex flex-none items-center gap-2">
                {cards.length > 0 && (
                  <span className="mr-1 text-[12.5px] tabular-nums text-muted">
                    {savedCount} of {MAX_ENTRIES}
                  </span>
                )}
                <input
                  ref={fileInput}
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    // Cleared so picking the same file again fires a change.
                    e.target.value = "";
                    if (file) void pickCsv(file);
                  }}
                />
                {/* The empty state offers these two itself. */}
                {cards.length > 0 && (
                  <>
                  <Button
                    variant="secondary"
                    onClick={() => fileInput.current?.click()}
                    disabled={full}
                    title="A CSV with the header: question,answer"
                  >
                    Import CSV
                  </Button>
                  <Button onClick={addCard} disabled={full} title={full ? `An FAQ holds up to ${MAX_ENTRIES} questions.` : undefined}>
                    Add question
                  </Button>
                  </>
                )}
                <Menu
                  label="More actions for this FAQ"
                  items={[
                    { label: "Copy FAQ ID", onSelect: () => void navigator.clipboard.writeText(id) },
                    { label: "Delete FAQ", onSelect: () => setDeleteFaq(true), danger: true },
                  ]}
                />
              </div>
            </div>
          </div>

          {nameError && (
            <div role="alert" className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
              {nameError}
            </div>
          )}

          <div className="pt-6">
            {savedCount === 0 && cards.length === 0 ? (
              <div className="relative overflow-hidden rounded-xl border border-line-2 bg-white">
                <div aria-hidden className={cn(ROW_GRID, "border-b border-line bg-canvas text-[12.5px] font-medium text-faint")}>
                  <span className="px-4 py-2.5">Question</span>
                  <span className="hidden px-4 py-2.5 sm:block sm:border-l sm:border-line">Answer</span>
                </div>
                <div className="flex flex-col items-center px-6 py-14 text-center">
                  <h2 className="text-[17px] font-semibold leading-6 text-ink">No questions yet</h2>
                  <p className="mt-1.5 max-w-[46ch] text-[14px] leading-[22px] text-muted">
                    Add a question people ask and the answer you want given.
                  </p>
                  <div className="mt-5 flex flex-wrap justify-center gap-2">
                    <Button onClick={addCard}>Add question</Button>
                    <Button variant="secondary" onClick={() => fileInput.current?.click()}>
                      Import CSV
                    </Button>
                  </div>
                  <p className="mt-4 text-[12.5px] leading-5 text-faint">
                    A CSV needs the header{" "}
                    <code className="rounded border border-line bg-subtle px-1 py-0.5 font-mono text-[11.5px] text-muted">question,answer</code>
                  </p>
                </div>
              </div>
            ) : (
              <>
                <div className="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
                  <p className="flex items-start gap-2 text-[13px] leading-5 text-muted">
                    <span aria-hidden className="mt-[7px] h-1.5 w-1.5 flex-none rounded-full bg-live" />
                    Live. Saved changes reach agents on their next conversation, with no publish.
                  </p>
                  {searchable && (
                    <Input
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Search questions and answers"
                      aria-label="Search questions and answers"
                      className="max-w-[300px]"
                    />
                  )}
                </div>
                <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
                  <div className={cn(ROW_GRID, "hidden border-b border-line bg-canvas text-[12.5px] font-medium text-faint sm:grid")}>
                    <span className="px-4 py-2.5">Question</span>
                    <span className="hidden px-4 py-2.5 sm:block sm:border-l sm:border-line">Answer</span>
                  </div>
                  {visible.length === 0 ? (
                    <p className="border-b border-line px-4 py-10 text-center text-[13.5px] text-muted">
                      No questions match that.
                    </p>
                  ) : (
                    visible.map((card) => (
                      <EntryRow
                        key={card.key}
                        card={card}
                        autoFocus={card.key === focusKey}
                        onEdit={(patch) => patchCard(card.key, { ...patch, error: "" })}
                        onSave={() => void saveCard(card)}
                        onDelete={() =>
                          card.id
                            ? setDeleteEntry(card)
                            : setCards((all) => all.filter((c) => c.key !== card.key))
                        }
                      />
                    ))
                  )}
                  {!full && (
                    <button
                      type="button"
                      onClick={addCard}
                      className="flex w-full items-center gap-2 border-t border-line px-4 py-3 text-left text-[13.5px] font-medium text-muted transition-colors first:border-t-0 hover:bg-canvas hover:text-ink focus:outline-none focus-visible:bg-canvas focus-visible:text-ink"
                    >
                      <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
                        <path d="M12 5v14M5 12h14" />
                      </svg>
                      Add question
                    </button>
                  )}
                </div>
              </>
            )}
          </div>
        </Container>
      </div>

      {deleteEntry && (
        <Modal
          title="Delete question?"
          sub="Agents using this FAQ stop answering it from their next conversation."
          width="max-w-[460px]"
          onClose={() => !deleting && (setDeleteEntry(null), setDeleteErr(""))}
          footer={
            <>
              <Button variant="secondary" disabled={deleting} onClick={() => { setDeleteEntry(null); setDeleteErr(""); }}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDeleteEntry} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete question"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3 text-[13px] font-medium leading-5 text-ink [overflow-wrap:anywhere]">
              {deleteEntry.saved?.question}
            </div>
            {deleteErr && (
              <div role="alert" className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                {deleteErr}
              </div>
            )}
          </div>
        </Modal>
      )}

      {deleteFaq && (
        <Modal
          title="Delete FAQ?"
          sub="Its questions and answers are gone for good."
          width="max-w-[460px]"
          onClose={() => !deleting && (setDeleteFaq(false), setDeleteErr(""))}
          footer={
            <>
              <Button variant="secondary" disabled={deleting} onClick={() => { setDeleteFaq(false); setDeleteErr(""); }}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDeleteFaq} disabled={deleting || !!deleteErr}>
                {deleting ? "Deleting…" : "Delete FAQ"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="text-[13px] font-semibold leading-5 text-ink">{faq.name}</div>
              <div className="mt-0.5 text-[12.5px] leading-5 text-muted">
                {savedCount} question{savedCount === 1 ? "" : "s"}
              </div>
            </div>
            {deleteErr && (
              <div role="alert" className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                {deleteErr}
              </div>
            )}
          </div>
        </Modal>
      )}

      {csv && (
        <Modal
          title="Import questions"
          sub={csv.file}
          width="max-w-[480px]"
          onClose={() => !csv.busy && setCsv(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setCsv(null)} disabled={csv.busy}>
                Cancel
              </Button>
              {csv.entries.length > 0 && !csv.problem && (
                <Button onClick={importCsv} disabled={csv.busy}>
                  {csv.busy ? "Adding…" : `Add ${csv.entries.length} question${csv.entries.length === 1 ? "" : "s"}`}
                </Button>
              )}
            </>
          }
        >
          <div className="pb-2">
            {csv.problem ? (
              <div role="alert" className="grid gap-1 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                <span>{csv.problem}</span>
                {csv.errors
                  .filter((line) => line !== csv.problem)
                  .map((line, i) => (
                    <span key={i} className="[overflow-wrap:anywhere]">{line}</span>
                  ))}
                {csv.entries.length > 0 && <span className="text-ink-soft">Nothing was added.</span>}
              </div>
            ) : (
              <p className="text-[13.5px] leading-5 text-ink-soft">
                {csv.entries.length} question{csv.entries.length === 1 ? "" : "s"} will be added to{" "}
                <span className="font-semibold text-ink">{faq.name}</span>
                {savedCount > 0 ? ", after the ones already there." : "."}
              </p>
            )}
          </div>
        </Modal>
      )}

      {unsaved.pendingPath && (
        <UnsavedChangesModal
          sub="This FAQ has questions you have not saved."
          onKeepEditing={unsaved.cancelLeave}
          onDiscard={unsaved.confirmLeave}
        />
      )}
    </AppShell>
  );
}

export default function FaqEditorPage() {
  return (
    <Suspense fallback={<EditorSkeleton />}>
      <FaqEditorInner />
    </Suspense>
  );
}
