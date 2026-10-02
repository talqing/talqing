"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  AgentResponse,
  IntegrationResponse,
  WhatsAppBatchResponse,
  WhatsAppTemplate,
} from "@talqing/sdk";
import {
  Badge,
  Button,
  Field,
  Input,
  Modal,
  Select,
  UnsavedChangesModal,
  useToast,
} from "@/app/components/ui";
import { api, talqing } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { EMPTY_LIST, type ParsedList, blankCount } from "@/app/email/outbound/csv";
import { rampNeverRises } from "@/app/components/Pacing";
import { RecipientEditor } from "@/app/email/outbound/RecipientEditor";
import { Step } from "@/app/email/outbound/CreateBatch";
import {
  SendTermsFields,
  type SendTermsDraft,
  termsBody,
  termsFrom,
  windowNeverOpens,
} from "@/app/email/outbound/SendTerms";
import { localTimezone } from "@/lib/date";
import { DEFAULT_RESEND, ResendRow, type ResendDraft, resendBody } from "./ResendRow";
import { TemplatePreview } from "./TemplatePreview";
import { listCopy } from "./listCopy";
import { formatPhone } from "./shared";

/* Creating a batch, in the order the questions have to be answered: who sends
 * it and which approved template, then the list, then which column fills each
 * blank. The template comes first because it decides the list's columns: the
 * phone number, plus one per variable. */

/** Column names that almost always hold the phone number. */
const PHONE_COLUMNS = ["phone", "phone_number", "mobile", "whatsapp", "number", "to"];

/** The sender's name, and its number unless the name already says it. */
function senderLabel(s: IntegrationResponse): string {
  const phone = formatPhone(String(s.provider_account_info.sender_e164 ?? ""));
  return s.display_name.includes(phone) ? s.display_name : `${s.display_name} · ${phone}`;
}

/** Where a blank sits when it is not in the body, where the preview shows it less plainly. */
function usedOutsideBody(template: WhatsAppTemplate, variable: string): string | undefined {
  const has = (text: string | null | undefined) =>
    new RegExp(`\\{\\{\\s*${variable}\\s*\\}\\}`).test(text ?? "");
  if (has(template.body)) return undefined;
  if (has(template.header?.text) || has(template.header?.url)) return "Used in the header.";
  if (template.buttons.some((b) => has(b.url))) return "Used in the button link.";
  return "Used in the button's code.";
}

/** A batch to start the form from: another batch's settings, and rows to resend. */
export type InitialBatch = {
  senderId: string;
  templateId: string;
  list: ParsedList;
  toColumn: string;
  variableMap: Record<string, string>;
  terms: SendTermsDraft;
  resend: ResendDraft;
};

const skippedHint = (rows: number) =>
  `Blank on ${rows.toLocaleString()} ${rows === 1 ? "row, which" : "rows, which"} will be skipped.`;

/** What to say when the list has rows but no column for a template blank. */
const missingNote = (variable: string) =>
  `Your list has no ${variable} column. Pick the column that holds it, or add one.`;

export function CreateBatch({
  senders,
  agents,
  initial,
  onCreated,
  onCancel,
}: {
  senders: IntegrationResponse[];
  agents: AgentResponse[];
  initial?: InitialBatch;
  onCreated: (batch: WhatsAppBatchResponse) => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const active = senders.filter((s) => s.status === "active");
  const [senderId, setSenderId] = useState(
    active.some((s) => s.id === initial?.senderId)
      ? initial!.senderId
      : active.length === 1
        ? active[0].id
        : "",
  );
  const [agentName, setAgentName] = useState<string | null | undefined>(undefined);
  const [templates, setTemplates] = useState<WhatsAppTemplate[] | null>(null);
  const [templateId, setTemplateId] = useState("");
  const [list, setList] = useState<ParsedList>(initial?.list ?? EMPTY_LIST);
  const [toColumn, setToColumn] = useState(initial?.toColumn ?? "");
  const [variableMap, setVariableMap] = useState<Record<string, string>>(initial?.variableMap ?? {});
  const [name, setName] = useState("");
  /* 250 a day is Meta's first messaging tier, shared by every number in the
     business portfolio, so it is where a new sender's cap starts. */
  const [terms, setTerms] = useState<SendTermsDraft>(
    () =>
      initial?.terms ??
      termsFrom(
        {
          timezone: localTimezone(),
          window: null,
          send_gap_seconds: 1,
          send_daily_cap: { kind: "fixed", limit: 250 },
        },
        null,
      ),
  );
  const [resend, setResend] = useState<ResendDraft>(() => initial?.resend ?? DEFAULT_RESEND);
  const [discarding, setDiscarding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [errors, setErrors] = useState<string[]>([]);

  const sender = senders.find((s) => s.id === senderId);
  const template = templates?.find((t) => t.id === templateId);
  const copy = useMemo(() => listCopy(template), [template]);
  /* What the rows are keyed by: the list's own columns once it has any,
     otherwise the ones it starts with. */
  const columns = list.columns.length ? list.columns : copy.starterColumns;

  // The sender's templates and who answers its messages, both read live.
  useEffect(() => {
    setTemplates(null);
    setTemplateId("");
    setAgentName(undefined);
    if (!senderId) return;
    let stale = false;
    talqing.whatsapp.templates
      .list({ integration_id: senderId })
      .then((r) => {
        if (stale) return;
        setTemplates(r.templates);
        const sendable = r.templates.filter((t) => !t.unsupported_reason);
        const wanted = senderId === initial?.senderId ? initial.templateId : "";
        if (sendable.some((t) => t.id === wanted)) setTemplateId(wanted);
        else if (sendable.length === 1) setTemplateId(sendable[0].id);
      })
      .catch((e) => !stale && (setTemplates([]), setErr(apiErrorMessage(e))));
    api
      .listIntegrationTriggers(senderId)
      .then((page) => {
        if (stale) return;
        // A reply is a message, so it is the Messages agent that answers it.
        const trigger = page.items.find(
          (t) => t.trigger_type === "whatsapp.message.inbound" && t.enabled && t.status === "active",
        );
        const agent = agents.find((a) => a.id === trigger?.agent_id);
        setAgentName(agent ? agent.config.name : null);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [senderId, agents, initial]);

  // Preselect the obvious columns. Only fills a blank or stale choice — never
  // overwrites one somebody made.
  useEffect(() => {
    setToColumn((current) =>
      columns.includes(current)
        ? current
        : (columns.find((c) => PHONE_COLUMNS.includes(c.toLowerCase())) ?? ""),
    );
    setVariableMap((current) => {
      // Nothing to map onto until the template is chosen.
      if (!template) return current;
      const next: Record<string, string> = {};
      for (const v of template?.variables ?? []) {
        if (current[v] && columns.includes(current[v])) next[v] = current[v];
        else if (columns.includes(v)) next[v] = v;
      }
      return next;
    });
  }, [columns, template]);

  const previewValues = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(variableMap).map(([variable, column]) => [variable, list.rows[0]?.[column] ?? ""]),
      ),
    [variableMap, list.rows],
  );
  const usNumbers = toColumn
    ? list.rows.filter((r) => (r[toColumn] ?? "").replace(/[\s()-]/g, "").startsWith("+1")).length
    : 0;

  /* Blanks the list has rows for but no column of that name: the one case where
     "pick a column" is not enough, because the column may not exist yet. */
  const missing = useMemo(
    () =>
      new Set(
        list.rows.length ? (template?.variables ?? []).filter((v) => !columns.includes(v)) : [],
      ),
    [list.rows.length, template, columns],
  );

  const problems = useMemo(() => {
    const out: string[] = [];
    if (!sender) out.push("Choose the WhatsApp number to send from.");
    else if (!template) out.push("Choose an approved template.");
    if (!list.rows.length) out.push("Add at least one row to the list.");
    if (list.badColumns.length) out.push("Fix the column names flagged above.");
    if (!toColumn) out.push("Say which column has the phone numbers.");
    for (const v of template?.variables ?? []) {
      if (!variableMap[v]) out.push(missing.has(v) ? missingNote(v) : `Say which column fills {{${v}}}.`);
    }
    if (windowNeverOpens(terms)) out.push("Sending hours that start and end at the same time never open.");
    if (rampNeverRises(terms.cap)) out.push("A ramp has to end higher than it starts.");
    return out;
  }, [sender, template, list, toColumn, variableMap, missing, terms]);

  const started = list.rows.length > 0 || !!name.trim();

  async function create() {
    if (!sender || !template) return;
    setSaving(true);
    setErr("");
    setErrors([]);
    try {
      const batch = await talqing.whatsapp.batches.create({
        createWhatsAppBatchRequest: {
          ...(name.trim() ? { name: name.trim() } : {}),
          integration_id: sender.id,
          template_id: template.id,
          to_column: toColumn,
          variable_map: variableMap,
          recipients: list.rows.map((row) => ({ input: row })),
          ...termsBody(terms),
          ...resendBody(resend),
        },
      });
      if (batch.counts.skipped)
        toast({
          msg: `${batch.counts.ready.toLocaleString()} ready, ${batch.counts.skipped.toLocaleString()} skipped`,
          kind: "ok",
        });
      onCreated(batch);
    } catch (e) {
      const reported = apiErrorList(e);
      setErrors(reported);
      setErr(reported.length ? "" : apiErrorMessage(e, "Could not create the batch."));
    } finally {
      setSaving(false);
    }
  }

  const close = () => (saving ? undefined : started ? setDiscarding(true) : onCancel());

  return (
    <Modal
      title="New WhatsApp batch"
      sub="Pick the number and template, build the list, check the message and choose how it sends. Nothing goes out until you start it."
      onClose={close}
      width="max-w-[920px]"
    >
      <section>
        <Step
          n={1}
          title="Name, sender and template"
          sub="The template decides the list's columns: a phone number, plus one per blank."
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" className="sm:col-span-2">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={template?.name ?? "Named after the template unless you say otherwise"}
              maxLength={200}
            />
          </Field>
          <Field
            label="Sender"
            hint={
              !sender
                ? undefined
                : agentName === undefined
                  ? " "
                  : agentName
                    ? `Replies are answered by ${agentName}.`
                    : "No agent answers its messages, so replies won't be answered."
            }
          >
            <Select value={senderId} onChange={(e) => setSenderId(e.target.value)}>
              <option value="">Choose a WhatsApp number</option>
              {active.map((s) => (
                <option key={s.id} value={s.id}>
                  {senderLabel(s)}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="Template"
            hint={
              template
                ? `${template.category.toLowerCase()} · ${template.language}`
                : templates && templates.length === 0 && sender
                  ? "No approved templates on this number yet."
                  : undefined
            }
          >
            <Select
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
              disabled={!sender || templates === null}
            >
              <option value="">{sender && templates === null ? "Loading…" : "Choose an approved template"}</option>
              {(templates ?? []).map((t) => (
                <option
                  key={t.id}
                  value={t.id}
                  disabled={!!t.unsupported_reason}
                  data-detail={t.unsupported_reason ?? undefined}
                >
                  {t.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </section>

      <section className="mt-7 border-t border-line pt-6">
        <Step
          n={2}
          title="The list"
          sub={
            template ? (
              <>
                Type the rows in, upload a CSV, or both. Each row needs{" "}
                <code className="font-mono text-[12px] text-ink-soft">phone</code>
                {template.variables.length > 0 && (
                  <>
                    , plus{" "}
                    {template.variables.map((v, k, all) => (
                      <span key={v}>
                        {k > 0 && (k === all.length - 1 ? " and " : ", ")}
                        <code className="font-mono text-[12px] text-ink-soft">{v}</code>
                      </span>
                    ))}{" "}
                    for the template&apos;s {template.variables.length === 1 ? "blank" : "blanks"}
                  </>
                )}
                . Any other column is extra context for the agent.
              </>
            ) : (
              "Type the rows in, upload a CSV, or both. Choose a template first to see which columns it needs."
            )
          }
        />
        <RecipientEditor list={list} onChange={setList} copy={copy} />
      </section>

      <section className="mt-7 border-t border-line pt-6">
        <Step
          n={3}
          title="The message"
          sub="Which column holds the number and fills each blank, and row 1 exactly as it arrives."
        />
        {template ? (
          <div className="grid gap-4">
            <div className="grid gap-4 sm:grid-cols-3">
              <Field
                label="Phone number"
                hint={
                  toColumn && list.rows.length && blankCount(list.rows, toColumn)
                    ? skippedHint(blankCount(list.rows, toColumn))
                    : undefined
                }
              >
                <Select value={toColumn} onChange={(e) => setToColumn(e.target.value)} aria-label="Column holding the phone number">
                  <option value="">Choose a column</option>
                  {columns.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              </Field>
              {template.variables.map((variable) => {
                const column = variableMap[variable] ?? "";
                const blanks = column && list.rows.length ? blankCount(list.rows, column) : 0;
                const where = usedOutsideBody(template, variable);
                return (
                  <Field
                    key={variable}
                    label={<span className="font-mono">{`{{${variable}}}`}</span>}
                    hint={
                      !column && missing.has(variable)
                        ? missingNote(variable)
                        : [where, blanks ? skippedHint(blanks) : ""].filter(Boolean).join(" ") || undefined
                    }
                  >
                    <Select
                      value={column}
                      onChange={(e) => setVariableMap((m) => ({ ...m, [variable]: e.target.value }))}
                      aria-label={`Column filling {{${variable}}}`}
                    >
                      <option value="">Choose a column</option>
                      {columns.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </Select>
                  </Field>
                );
              })}
            </div>
            <div>
              <div className="mb-1.5 flex items-center gap-2 text-[13px] font-medium text-ink">
                Preview
                {list.rows.length > 0 && <span className="font-normal text-faint">row 1</span>}
              </div>
              <TemplatePreview
                template={template}
                values={list.rows.length ? previewValues : undefined}
                bodyClassName="max-h-[240px] overflow-y-auto scroll-thin"
              />
              {template.category === "MARKETING" && usNumbers > 0 && (
                <p className="mt-2 text-[13px] leading-5 text-warn">
                  {usNumbers.toLocaleString()} {usNumbers === 1 ? "number is" : "numbers are"} in the US,
                  where WhatsApp doesn&apos;t deliver marketing templates.
                </p>
              )}
            </div>
          </div>
        ) : (
          <p className="rounded-xl border border-line-2 bg-white px-4 py-5 text-center text-[13px] text-muted">
            Choose a template above, and its blanks appear here beside your columns.
          </p>
        )}
      </section>

      <section className="mt-7 border-t border-line pt-6">
        <Step
          n={4}
          title="Sending"
          sub="How fast it goes out, and when. You start it from the batch page after reviewing the rows, and can change these until then."
        />
        <SendTermsFields
          value={terms}
          onChange={setTerms}
          showStart={false}
          noun="message"
          trailing={<ResendRow value={resend} onChange={setResend} />}
        />
        <p className="mt-2 text-[12.5px] leading-5 text-muted">
          A new WhatsApp business can message 250 people a day; keep the daily cap at or under your
          tier.
        </p>
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
        <p className="flex min-w-0 flex-1 items-center gap-2 text-[13px] leading-5 text-muted">
          {problems.length ? (
            problems[0]
          ) : (
            <>
              <Badge variant="warn">Opt-in</Badge>
              <span>
                <span className="font-semibold text-ink tabular-nums">
                  {list.rows.length.toLocaleString()} {list.rows.length === 1 ? "person" : "people"}
                </span>
                , only if they opted in to hear from you on WhatsApp. Nothing is sent yet.
              </span>
            </>
          )}
        </p>
        <Button variant="secondary" onClick={close} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={create} disabled={saving || problems.length > 0}>
          {saving ? "Creating…" : "Create batch"}
        </Button>
      </div>

      {discarding && (
        <UnsavedChangesModal
          sub="Nothing has been sent, and this list is not saved anywhere."
          onKeepEditing={() => setDiscarding(false)}
          onDiscard={onCancel}
        />
      )}
    </Modal>
  );
}
