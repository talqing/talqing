"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Badge,
  Button,
  Field,
  Input,
  Modal,
  Select,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import type {
  AgentResponse,
  ImportedNumberResult,
  RemoteNumber,
  TelephonyAccountResponse,
  TelephonyProviderSpec,
  TelephonySetupField,
} from "@talqing/sdk";
import { CarrierPicker } from "./CarrierPicker";
import { agentLabel } from "./shared";

type Step = "carrier" | "account" | "connect" | "select" | "results";
type TaskState = "pending" | "running" | "done" | "failed";
type Task = { key: string; label: string; state: TaskState };

const STEP_TITLE: Record<Step, string> = {
  carrier: "Import a number",
  account: "Choose an account",
  connect: "Connect your carrier",
  select: "Select numbers",
  results: "Import complete",
};

function TaskList({ tasks }: { tasks: Task[] }) {
  return (
    <ul className="grid gap-2.5">
      {tasks.map((task) => (
        <li key={task.key} className="flex items-center gap-2.5 text-[13.5px] leading-5">
          <span
            className={cn(
              "grid h-[18px] w-[18px] flex-none place-items-center rounded-full text-[10px] font-bold",
              task.state === "done" && "bg-live/[0.12] text-live",
              task.state === "failed" && "bg-danger/[0.1] text-danger",
              task.state === "running" && "bg-info/[0.1] text-info",
              task.state === "pending" && "bg-subtle text-placeholder",
            )}
          >
            {task.state === "done" ? "✓" : task.state === "failed" ? "✕" : "·"}
          </span>
          <span className={cn(task.state === "pending" ? "text-muted" : "text-ink")}>
            {task.label}
            {task.state === "running" && "…"}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ImportWizard({
  catalog,
  accounts,
  agents,
  onClose,
  onFinished,
}: {
  catalog: TelephonyProviderSpec[];
  accounts: TelephonyAccountResponse[];
  agents: AgentResponse[];
  onClose: () => void;
  /** Reload the page's data. Called on every mutation, not just at the end. */
  onFinished: () => Promise<void>;
}) {
  const [step, setStep] = useState<Step>("carrier");
  const [err, setErr] = useState("");
  const [spec, setSpec] = useState<TelephonyProviderSpec | null>(null);
  const [accountId, setAccountId] = useState("");

  // connect step
  const [displayName, setDisplayName] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [tasks, setTasks] = useState<Task[]>([]);
  const [connecting, setConnecting] = useState(false);

  // select step
  const [remote, setRemote] = useState<RemoteNumber[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);

  // results step
  const [results, setResults] = useState<ImportedNumberResult[]>([]);
  const [assignAgentId, setAssignAgentId] = useState("");
  const [assigning, setAssigning] = useState(false);

  const busy = connecting || importing || assigning;

  const accountsForProvider = useMemo(
    () =>
      spec
        ? accounts.filter((a) => a.provider === spec.provider && a.status !== "disabled")
        : [],
    [accounts, spec],
  );

  const visibleRemote = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return remote;
    return remote.filter(
      (item) =>
        item.e164.toLowerCase().includes(query) ||
        (item.label || "").toLowerCase().includes(query),
    );
  }, [remote, search]);

  const selectableRemote = useMemo(
    () => visibleRemote.filter((item) => !item.already_imported),
    [visibleRemote],
  );

  const loadRemote = useCallback(async (id: string) => {
    setRemoteLoading(true);
    setErr("");
    try {
      const page = await api.listRemoteNumbers(id);
      setRemote(page.items);
    } catch (error: unknown) {
      setErr(apiErrorMessage(error));
    } finally {
      setRemoteLoading(false);
    }
  }, []);

  function selectAccount(account: TelephonyAccountResponse) {
    if (account.status !== "ready" && account.status !== "connected") {
      setErr(
        "This carrier account needs attention. Fix it from its card on the Phone numbers page, then import again.",
      );
      return;
    }
    setAccountId(account.id);
    setSelected(new Set());
    setSearch("");
    setStep("select");
    void loadRemote(account.id);
  }

  function pickCarrier(next: TelephonyProviderSpec) {
    if (!next.implemented) return;
    setErr("");
    setSpec(next);
    const existing = accounts.filter(
      (a) => a.provider === next.provider && a.status !== "disabled",
    );
    if (existing.length === 0) {
      startConnect(next);
      return;
    }
    if (existing.length === 1) {
      selectAccount(existing[0]);
      return;
    }
    setStep("account");
  }

  function startConnect(next: TelephonyProviderSpec) {
    setDisplayName(next.label);
    // Which fields come prefilled is carrier knowledge, so the catalog owns it.
    const defaults: Record<string, string> = {};
    for (const field of next.setup_fields) {
      defaults[field.key] = field.default ?? "";
    }
    setFieldValues(defaults);
    setTasks([]);
    setStep("connect");
  }

  async function submitConnect(e: FormEvent) {
    e.preventDefault();
    if (!spec) return;
    setConnecting(true);
    setErr("");
    const next: Task[] = [
      { key: "verify", label: `Verifying credentials with ${spec.label}`, state: "running" },
      { key: "trunks", label: "Setting up call routing", state: "pending" },
    ];
    setTasks(next);
    const mark = (key: string, state: TaskState) =>
      setTasks((current) => current.map((t) => (t.key === key ? { ...t, state } : t)));
    try {
      const account_info: Record<string, string> = {};
      const credentials: Record<string, string> = {};
      for (const field of spec.setup_fields) {
        const value = (fieldValues[field.key] || "").trim();
        if (!value) {
          if (field.required) throw new Error(`${field.label} is required`);
          continue;
        }
        if (field.target === "credentials") credentials[field.key] = value;
        else account_info[field.key] = value;
      }
      const created = await api.createTelephonyAccount({
        provider: spec.provider,
        display_name: displayName.trim(),
        account_info,
        credentials,
      });
      await onFinished();
      if (created.status === "error") {
        mark("verify", "failed");
        throw new Error(created.status_message || "Could not verify these credentials.");
      }
      mark("verify", "done");
      mark("trunks", "running");
      const provisioned = await api.provisionTelephonyAccount(created.id);
      await onFinished();
      if (provisioned.status !== "ready") {
        mark("trunks", "failed");
        throw new Error(provisioned.status_message || "Setup did not finish.");
      }
      mark("trunks", "done");
      setAccountId(provisioned.id);
      setSelected(new Set());
      setSearch("");
      setStep("select");
      void loadRemote(provisioned.id);
    } catch (error: unknown) {
      setTasks((current) =>
        current.map((t) => (t.state === "running" ? { ...t, state: "failed" } : t)),
      );
      setErr(apiErrorMessage(error));
    } finally {
      setConnecting(false);
    }
  }

  async function submitImport() {
    if (selected.size === 0) {
      setErr("Select at least one number.");
      return;
    }
    setImporting(true);
    setErr("");
    try {
      const imported = await api.importPhoneNumbers({
        telephony_account_id: accountId,
        e164s: Array.from(selected),
      });
      setResults(imported.items);
      setStep("results");
      await onFinished();
    } catch (error: unknown) {
      setErr(apiErrorMessage(error));
    } finally {
      setImporting(false);
    }
  }

  // Only numbers that came back provisioned and inbound-capable; a DID still
  // blocked by carrier-console setup would just 400 on assign.
  const assignable = useMemo(
    () =>
      results.flatMap((r) =>
        r.phone_number && r.phone_number.readiness === "needs_agent" ? [r.phone_number] : [],
      ),
    [results],
  );

  async function submitAssign() {
    if (!assignAgentId) return;
    setAssigning(true);
    setErr("");
    try {
      for (const number of assignable) {
        await api.assignPhoneNumber(number.id, { agent_id: assignAgentId });
      }
      await onFinished();
      onClose();
    } catch (error: unknown) {
      setErr(apiErrorMessage(error));
      await onFinished();
    } finally {
      setAssigning(false);
    }
  }

  useEffect(() => {
    if (step !== "results") return;
    if (assignAgentId) return;
    if (agents.length > 0) setAssignAgentId(agents[0].id);
  }, [step, agents, assignAgentId]);

  const okCount = results.filter((r) => r.ok).length;
  const failCount = results.length - okCount;

  const footer = (() => {
    if (step === "carrier") {
      return (
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
      );
    }
    if (step === "account") {
      return (
        <>
          <Button variant="secondary" onClick={() => setStep("carrier")}>
            Back
          </Button>
          {spec && <Button onClick={() => startConnect(spec)}>Connect another account</Button>}
        </>
      );
    }
    if (step === "connect") {
      const complete =
        spec &&
        displayName.trim() &&
        spec.setup_fields.every(
          (f) => !f.required || Boolean((fieldValues[f.key] || "").trim()),
        );
      return (
        <>
          <Button
            variant="secondary"
            onClick={() => setStep("carrier")}
            disabled={connecting}
          >
            Back
          </Button>
          <Button type="submit" form="connect-carrier-form" disabled={!complete || connecting}>
            {connecting ? "Connecting…" : "Connect"}
          </Button>
        </>
      );
    }
    if (step === "select") {
      return (
        <>
          <Button variant="secondary" onClick={() => setStep("carrier")} disabled={importing}>
            Back
          </Button>
          <Button onClick={() => void submitImport()} disabled={importing || selected.size === 0}>
            {importing
              ? "Importing…"
              : selected.size > 0
                ? `Import ${selected.size} number${selected.size === 1 ? "" : "s"}`
                : "Import"}
          </Button>
        </>
      );
    }
    return (
      <>
        <Button variant="secondary" onClick={onClose} disabled={assigning}>
          Done
        </Button>
        {assignable.length > 0 && agents.length > 0 && (
          <Button onClick={() => void submitAssign()} disabled={assigning || !assignAgentId}>
            {assigning
              ? "Assigning…"
              : `Assign to ${assignable.length} number${assignable.length === 1 ? "" : "s"}`}
          </Button>
        )}
      </>
    );
  })();

  return (
    <Modal
      title={STEP_TITLE[step]}
      sub={
        step === "carrier"
          ? "Bring a number you already own. Talqing answers the calls; your carrier keeps billing the minutes."
          : step === "connect" && spec
            ? `Paste your ${spec.label} API credentials. Talqing encrypts them and stores them as secrets.`
            : step === "select"
              ? "Numbers on this carrier account. Importing gets them ready to take calls."
              : undefined
      }
      width="max-w-[720px]"
      onClose={() => {
        if (busy) return;
        onClose();
      }}
      footer={footer}
    >
      <div className="pb-6">
        {err && (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/[0.06] px-3.5 py-2.5 text-[13.5px] text-danger [overflow-wrap:anywhere]">
            {err}
          </div>
        )}

        {step === "carrier" && (
          <CarrierPicker catalog={catalog} accounts={accounts} onPick={pickCarrier} />
        )}

        {step === "account" && (
          <div className="overflow-hidden rounded-xl border border-line-2">
            {accountsForProvider.map((account) => (
              <button
                key={account.id}
                type="button"
                onClick={() => selectAccount(account)}
                className="flex w-full items-center gap-3 border-b border-line px-4 py-3 text-left last:border-b-0 hover:bg-canvas"
              >
                <span className="min-w-0 flex-1">
                  <span className="block text-[14px] font-medium text-ink">
                    {account.display_name}
                  </span>
                  <span className="block text-[12.5px] text-muted">
                    {account.status === "ready" ? "Ready" : "Setup incomplete"}
                  </span>
                </span>
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" className="flex-none text-placeholder" aria-hidden>
                  <path d="m6 3.5 4.5 4.5L6 12.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            ))}
          </div>
        )}

        {step === "connect" && spec && (
          <>
            <form id="connect-carrier-form" className="grid gap-4" onSubmit={(e) => void submitConnect(e)}>
              <Field label="Name" htmlFor="ta-display-name">
                <Input
                  id="ta-display-name"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder={spec.label}
                  disabled={connecting}
                  required
                />
              </Field>
              {spec.setup_fields.map((field: TelephonySetupField) => (
                <Field
                  key={field.key}
                  label={field.label}
                  htmlFor={`ta-${field.key}`}
                  hint={field.hint || undefined}
                >
                  <Input
                    id={`ta-${field.key}`}
                    type={field.type === "secret_ref" ? "password" : "text"}
                    value={fieldValues[field.key] || ""}
                    onChange={(e) =>
                      setFieldValues((current) => ({ ...current, [field.key]: e.target.value }))
                    }
                    placeholder={field.placeholder || undefined}
                    autoComplete={field.type === "secret_ref" ? "off" : undefined}
                    className={field.type === "secret_ref" ? "font-mono" : undefined}
                    disabled={connecting}
                    required={field.required}
                  />
                </Field>
              ))}
            </form>
            {tasks.length > 0 && (
              <div className="mt-5 rounded-xl border border-line-2 bg-canvas p-4">
                <TaskList tasks={tasks} />
              </div>
            )}
          </>
        )}

        {step === "select" && (
          <div className="grid gap-3">
            <div className="flex items-center gap-2">
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search numbers…"
                disabled={importing}
              />
              <Button
                variant="secondary"
                disabled={importing || selectableRemote.length === 0}
                onClick={() =>
                  setSelected((current) =>
                    current.size > 0
                      ? new Set()
                      : new Set(selectableRemote.map((item) => item.e164)),
                  )
                }
              >
                {selected.size > 0 ? "Clear" : "Select all"}
              </Button>
            </div>
            {remoteLoading ? (
              <div className="rounded-xl border border-line-2 px-4 py-10 text-center text-[13px] text-muted">
                Loading numbers from your carrier…
              </div>
            ) : visibleRemote.length === 0 ? (
              <div className="rounded-xl border border-dashed border-line-strong px-4 py-10 text-center text-[13px] leading-5 text-muted">
                {remote.length === 0
                  ? "No numbers on this carrier account. Buy or transfer one with your carrier, then come back."
                  : "No numbers match that search."}
              </div>
            ) : (
              <div className="max-h-[280px] overflow-auto rounded-xl border border-line-2">
                {visibleRemote.map((item) => (
                  <label
                    key={item.e164}
                    className={cn(
                      "flex items-center gap-3 border-b border-line px-3.5 py-2.5 last:border-b-0",
                      item.already_imported
                        ? "cursor-not-allowed opacity-55"
                        : "cursor-pointer hover:bg-canvas",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(item.e164) || item.already_imported}
                      disabled={item.already_imported || importing}
                      onChange={() =>
                        setSelected((current) => {
                          const next = new Set(current);
                          if (next.has(item.e164)) next.delete(item.e164);
                          else next.add(item.e164);
                          return next;
                        })
                      }
                    />
                    <span className="font-mono text-[13.5px] font-medium text-ink">
                      {item.e164}
                    </span>
                    {item.label && (
                      <span className="truncate text-[12.5px] text-muted">{item.label}</span>
                    )}
                    {item.already_imported && (
                      <Badge className="ml-auto">Already imported</Badge>
                    )}
                  </label>
                ))}
              </div>
            )}
          </div>
        )}

        {step === "results" && (
          <div className="grid gap-4">
            <div className="text-[13.5px] leading-5 text-ink-soft">
              {okCount} imported and ready
              {failCount > 0 && `, ${failCount} could not be imported`}.
            </div>
            <div className="overflow-hidden rounded-xl border border-line-2">
              {results.map((result) => (
                <div
                  key={result.e164}
                  className="flex items-start gap-3 border-b border-line px-3.5 py-2.5 last:border-b-0"
                >
                  <span
                    className={cn(
                      "mt-0.5 grid h-[18px] w-[18px] flex-none place-items-center rounded-full text-[10px] font-bold",
                      result.ok ? "bg-live/[0.12] text-live" : "bg-danger/[0.1] text-danger",
                    )}
                  >
                    {result.ok ? "✓" : "✕"}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-[13.5px] font-medium text-ink">
                      {result.e164}
                    </span>
                    {result.error && (
                      <span className="mt-0.5 block text-[12.5px] leading-[18px] text-danger [overflow-wrap:anywhere]">
                        {result.error}
                      </span>
                    )}
                  </span>
                </div>
              ))}
            </div>
            {assignable.length > 0 &&
              (agents.length > 0 ? (
                <Field
                  label="Answer calls with"
                  hint={`Applied to the ${assignable.length} number${assignable.length === 1 ? "" : "s"} ready for inbound. You can change this per number later.`}
                >
                  <Select
                    value={assignAgentId}
                    onChange={(e) => setAssignAgentId(e.target.value)}
                    disabled={assigning}
                  >
                    {agents.map((agent) => (
                      <option key={agent.id} value={agent.id}>
                        {agentLabel(agent)}
                      </option>
                    ))}
                  </Select>
                </Field>
              ) : (
                <div className="rounded-xl border border-warn/30 bg-warn/[0.04] px-3.5 py-3 text-[13px] leading-5 text-warn">
                  No published voice agents yet. Publish one, then assign it to these numbers.
                </div>
              ))}
          </div>
        )}
      </div>
    </Modal>
  );
}
