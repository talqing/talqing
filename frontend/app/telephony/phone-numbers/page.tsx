"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "@/app/components/AppShell";
import {
  Button,
  EmptyState,
  Figure,
  Input,
  Modal,
  Segment,
  Skeleton,
  useToast,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { useActiveRegion } from "@/lib/regions";
import { apiErrorMessage } from "@/lib/apiError";
import type {
  AgentResponse,
  CallSummaryResponse,
  PhoneNumberResponse,
  TelephonyAccountResponse,
  TelephonyProviderSpec,
} from "@talqing/sdk";
import { CarrierGroup } from "./CarrierGroup";
import { ImportWizard } from "./ImportWizard";
import { TestCallModal } from "./TestCallModal";
import {
  READINESS,
  byDiallingOrder,
  isPublishedVoiceAgent,
  agentLabel,
  minutes,
  money,
  needsAttention,
  plural,
} from "./shared";
import { bucketTraffic, type NumberTraffic } from "./traffic";

/* Every traffic figure on this page is measured over one window, and the page
   names it in the strip and in the column head. Always pass the window
   explicitly: /v1/calls answers for the last 30 days when asked for none, so a
   total that looks like "this week" silently would not be. */
const WINDOW_DAYS = 7;

/* Per-number figures are bucketed client-side out of /v1/calls, which has no
   phone_number filter — so the window is paged through rather than sampled.
   200 is the API's page ceiling; the loop stops at CALLS_MAX so a very busy
   workspace cannot turn this page into a download. Past that, every figure is
   a floor and the page says so instead of quietly under-reporting. */
const CALLS_PAGE = 200;
const CALLS_MAX = 1000;

/** Past this many numbers, scanning costs enough to earn a search box. */
const SEARCHABLE_AT = 6;

type Filter = "all" | "live" | "attention";

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
  phone: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="7" y="2.5" width="10" height="19" rx="2.5" />
      <path d="M10 5.5h4M11 17.5h2" />
    </svg>
  ),
};

export default function PhoneNumbersPage() {
  const region = useActiveRegion();
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [accounts, setAccounts] = useState<TelephonyAccountResponse[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumberResponse[]>([]);
  const [catalog, setCatalog] = useState<TelephonyProviderSpec[]>([]);
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [calls, setCalls] = useState<{ items: CallSummaryResponse[]; capped: boolean } | null>(null);
  const [loadError, setLoadError] = useState("");

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  const [wizardOpen, setWizardOpen] = useState(false);
  const [callTarget, setCallTarget] = useState<PhoneNumberResponse | null>(null);
  const [disableTarget, setDisableTarget] = useState<PhoneNumberResponse | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<TelephonyAccountResponse | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busyId, setBusyId] = useState("");

  const notify = useCallback((msg: string) => toast({ kind: "ok", msg }), [toast]);
  const fail = useCallback((msg: string) => toast({ kind: "err", msg }), [toast]);

  const voiceAgents = useMemo(() => agents.filter(isPublishedVoiceAgent), [agents]);
  const agentsById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);

  const load = useCallback(async () => {
    try {
      const [accountsPage, numbersPage, providersPage, nextAgents] = await Promise.all([
        api.listTelephonyAccounts(),
        api.listPhoneNumbers(),
        api.listTelephonyProviders(),
        api.listAllAgents(),
      ]);
      setAccounts(accountsPage.items);
      setNumbers(numbersPage.items);
      setCatalog(providersPage.items);
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
    // Traffic decorates the page; a failure leaves the figures blank rather
    // than taking the numbers down with it.
    const start = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString();
    void (async () => {
      const items: CallSummaryResponse[] = [];
      try {
        for (let offset = 0; offset < CALLS_MAX; offset += CALLS_PAGE) {
          const page = await api.listCalls({ start, limit: CALLS_PAGE, offset });
          items.push(...page.items);
          if (!page.has_more) {
            setCalls({ items, capped: false });
            return;
          }
        }
        setCalls({ items, capped: true });
      } catch {
        setCalls({ items: [], capped: false });
      }
    })();
  }, [load]);

  const traffic = useMemo(
    () => bucketTraffic(calls?.items ?? [], numbers, WINDOW_DAYS, calls?.capped ?? false),
    [calls, numbers],
  );

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

  const assign = useCallback(
    (number: PhoneNumberResponse, agentId: string) =>
      void run(number.id, async () => {
        if (!agentId) {
          await api.unassignPhoneNumber(number.id);
          return `${number.e164} no longer answers calls.`;
        }
        await api.assignPhoneNumber(number.id, { agent_id: agentId });
        const agent = agentsById.get(agentId);
        return `${number.e164} now answers with ${agent ? agentLabel(agent) : "the selected agent"}.`;
      }),
    [agentsById, run],
  );

  const provisionNumber = useCallback(
    (number: PhoneNumberResponse) =>
      void run(number.id, async () => {
        const updated = await api.provisionPhoneNumber(number.id);
        return `${updated.e164}: ${READINESS[updated.readiness].label.toLowerCase()}.`;
      }),
    [run],
  );

  const enableNumber = useCallback(
    (number: PhoneNumberResponse) =>
      void run(number.id, async () => {
        await api.patchPhoneNumber(number.id, { status: "pending" });
        const updated = await api.provisionPhoneNumber(number.id);
        return `${updated.e164}: ${READINESS[updated.readiness].label.toLowerCase()}.`;
      }),
    [run],
  );

  async function confirmDisableNumber() {
    if (!disableTarget) return;
    const target = disableTarget;
    setConfirming(true);
    try {
      await api.patchPhoneNumber(target.id, { status: "disabled" });
      notify(`${target.e164} disabled.`);
      setDisableTarget(null);
    } catch (error: unknown) {
      fail(apiErrorMessage(error));
    } finally {
      setConfirming(false);
      await load();
    }
  }

  async function confirmDeleteAccount() {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setConfirming(true);
    try {
      await api.deleteTelephonyAccount(target.id);
      notify(`${target.display_name} disconnected.`);
      setDeleteTarget(null);
    } catch (error: unknown) {
      fail(apiErrorMessage(error));
    } finally {
      setConfirming(false);
      await load();
    }
  }

  /* ── what the filters leave on screen ─────────────────────────────────── */
  const liveCount = numbers.filter((n) => n.readiness === "live").length;
  const attentionCount = numbers.filter(needsAttention).length;
  const searchable = numbers.length > SEARCHABLE_AT;

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return numbers.filter((number) => {
      if (filter === "live" && number.readiness !== "live") return false;
      if (filter === "attention" && !needsAttention(number)) return false;
      if (!needle) return true;
      const agent = number.inbound_agent_id ? agentsById.get(number.inbound_agent_id) : undefined;
      return (
        number.e164.toLowerCase().includes(needle) ||
        (number.label || "").toLowerCase().includes(needle) ||
        (agent ? agentLabel(agent).toLowerCase().includes(needle) : false)
      );
    });
  }, [agentsById, filter, numbers, query]);

  /* Accounts in the order a reader wants them: whatever is broken or unfinished
     first, then the biggest estate. An account with nothing on it still shows,
     because an empty carrier is a prompt to import rather than a thing to hide. */
  const groups = useMemo(() => {
    const rank = (account: TelephonyAccountResponse) =>
      account.status === "error" ? 0 : !account.setup_complete ? 1 : 2;
    const owned = (id: string) => numbers.filter((n) => n.telephony_account_id === id);
    return accounts
      .map((account) => ({
        account,
        all: owned(account.id),
        shown: visible.filter((n) => n.telephony_account_id === account.id).sort(byDiallingOrder),
      }))
      .sort(
        (a, b) => rank(a.account) - rank(b.account) || b.all.length - a.all.length,
      );
  }, [accounts, numbers, visible]);

  const filtered = filter !== "all" || query.trim() !== "";
  const busiest = useMemo(() => {
    let best: { number: PhoneNumberResponse; traffic: NumberTraffic } | null = null;
    for (const number of numbers) {
      const bucket = traffic.byNumber.get(number.id);
      if (bucket && (!best || bucket.calls > best.traffic.calls)) best = { number, traffic: bucket };
    }
    return best;
  }, [numbers, traffic]);

  const nothingConnected = accounts.length === 0 && numbers.length === 0;

  /* The API refuses to delete an account while any number on it is still in
     service, so the modal has to know which ones before offering the button —
     otherwise the only way to discover the rule is to trip over it. Matches the
     server's test exactly: every status except `disabled` blocks. */
  const deleteBlockers = useMemo(
    () =>
      deleteTarget
        ? numbers.filter(
            (n) => n.telephony_account_id === deleteTarget.id && n.status !== "disabled",
          )
        : [],
    [deleteTarget, numbers],
  );

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 text-ink sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-col gap-4 border-b border-line py-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="font-display text-[26px] font-semibold leading-8 tracking-tight text-ink">
              Phone numbers
            </h1>
            <p className="mt-1.5 text-[14px] leading-5 text-muted">
              Bring numbers you already own and put a published voice agent on the line.
            </p>
          </div>
          <Button
            onClick={() => setWizardOpen(true)}
            className="min-h-[38px] self-start sm:self-auto"
          >
            {Icon.plus}
            Import number
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
              <Skeleton className="h-9 w-9 rounded-[10px]" />
              <Skeleton className="h-4 w-[180px]" />
              <div className="flex-1" />
              <Skeleton className="h-5 w-16 rounded-full" />
            </div>
            {[0, 1, 2, 3].map((row) => (
              <div key={row} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-[15px] w-[22%]" />
                  <Skeleton className="mt-2 h-3 w-[34%]" />
                </div>
                <Skeleton className="h-9 w-[160px] rounded-[10px]" />
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : nothingConnected ? (
          <EmptyState
            icon={Icon.phone}
            title={`No phone numbers in ${region.name}`}
            body="Connect the carrier you already buy numbers from — Twilio, Plivo, Exotel or Vobiz — and point one of them at an agent. Talqing answers the calls; your carrier keeps billing the minutes."
            cta={
              <Button onClick={() => setWizardOpen(true)}>
                {Icon.plus}
                Import your first number
              </Button>
            }
          />
        ) : (
          <>
            {/* The week on the phones, on one line. Hidden until there is
                traffic: a row of zeroes above a freshly imported number only
                says "nothing has happened yet" twice. */}
            {traffic.totals.calls > 0 && (
              <section className="mb-5 flex flex-col rounded-xl border border-line-2 bg-white sm:flex-row">
                <div className="flex flex-none items-center border-b border-line px-4 py-3 sm:border-b-0 sm:border-r sm:px-5">
                  <span className="text-[12px] font-semibold uppercase tracking-[0.07em] text-faint">
                    Last {WINDOW_DAYS} days
                  </span>
                </div>
                <div className="grid flex-1 grid-cols-2 sm:grid-cols-4">
                  <Figure
                    label={traffic.capped ? "Calls (at least)" : "Calls"}
                    value={traffic.totals.calls}
                    detail={
                      traffic.capped
                        ? `Counted from the ${CALLS_MAX.toLocaleString()} most recent`
                        : `across ${plural(traffic.byNumber.size, "number")}`
                    }
                  />
                  <Figure label="Talk time" value={minutes(traffic.totals.talkSeconds)} />
                  <Figure label="Spend" value={money(traffic.totals.spend)} />
                  <Figure
                    label="Busiest number"
                    value={
                      busiest ? (
                        // Mono and a size down: an E.164 is longer than any
                        // other figure in the strip and must not truncate.
                        <span className="font-mono text-[14px]">{busiest.number.e164}</span>
                      ) : (
                        "—"
                      )
                    }
                    detail={busiest ? plural(busiest.traffic.calls, "call") : undefined}
                  />
                </div>
              </section>
            )}

            {searchable && (
              <div className="mb-3 flex flex-wrap items-center gap-2.5">
                <div className="relative min-w-[180px] flex-1 sm:max-w-[300px]">
                  <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint">
                    {Icon.search}
                  </span>
                  <Input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search numbers, labels or agents"
                    aria-label="Search phone numbers"
                    className="pl-9"
                  />
                </div>
                <Segment<Filter>
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "All", count: numbers.length },
                    { value: "live", label: "Live", count: liveCount },
                    { value: "attention", label: "Needs attention", count: attentionCount },
                  ]}
                />
              </div>
            )}

            {filtered && visible.length === 0 ? (
              <div className="rounded-xl border border-line-2 bg-white px-4 py-12 text-center">
                <p className="text-[14px] font-medium leading-5 text-ink">No numbers match that</p>
                <p className="mt-1 text-[13px] leading-5 text-muted">
                  Try a different number or label, or switch the filter back to All.
                </p>
              </div>
            ) : (
              <div className="grid gap-4">
                {groups.map(({ account, all, shown }) =>
                  /* A filter that empties a carrier hides the carrier too —
                     otherwise "Needs attention" answers with a page of healthy
                     headers and no rows. */
                  filtered && shown.length === 0 ? null : (
                    <CarrierGroup
                      key={account.id}
                      account={account}
                      owned={all}
                      numbers={shown}
                      catalog={catalog}
                      agents={voiceAgents}
                      agentsById={agentsById}
                      traffic={traffic.byNumber}
                      peakDay={traffic.peakDay}
                      windowDays={WINDOW_DAYS}
                      busyId={busyId}
                      accountBusy={busyId === account.id}
                      onAssign={assign}
                      onTestCall={setCallTarget}
                      onProvisionNumber={provisionNumber}
                      onDisableNumber={setDisableTarget}
                      onEnableNumber={enableNumber}
                      onProvisionAccount={() =>
                        void run(account.id, async () => {
                          const updated = await api.provisionTelephonyAccount(account.id);
                          return `${updated.display_name} is ready.`;
                        })
                      }
                      onDeleteAccount={() => setDeleteTarget(account)}
                      onSetupConfirmed={async (message) => {
                        notify(message);
                        await load();
                      }}
                      onError={fail}
                      onImport={() => setWizardOpen(true)}
                    />
                  ),
                )}
              </div>
            )}

            {voiceAgents.length === 0 && numbers.length > 0 && (
              <p className="mt-3 text-[13px] leading-5 text-muted">
                No published voice agents yet.{" "}
                <Link href="/agents" className="font-medium text-ink underline underline-offset-2">
                  Publish one
                </Link>{" "}
                to let these numbers answer calls.
              </p>
            )}
          </>
        )}
      </div>

      {wizardOpen && (
        <ImportWizard
          catalog={catalog}
          accounts={accounts}
          agents={voiceAgents}
          onClose={() => setWizardOpen(false)}
          onFinished={load}
        />
      )}

      {callTarget && (
        <TestCallModal
          number={callTarget}
          agents={voiceAgents}
          onClose={() => setCallTarget(null)}
        />
      )}

      {disableTarget && (
        <Modal
          title="Disable this number?"
          sub="It stops answering calls right away. You can turn it back on from the same row."
          width="max-w-[460px]"
          onClose={() => !confirming && setDisableTarget(null)}
          footer={
            <>
              <Button
                variant="secondary"
                onClick={() => setDisableTarget(null)}
                disabled={confirming}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => void confirmDisableNumber()}
                disabled={confirming}
              >
                {confirming ? "Disabling…" : "Disable number"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="font-mono text-[14px] font-semibold leading-5 text-ink">
                {disableTarget.e164}
              </div>
              <div className="mt-1 text-[13px] leading-5 text-muted">
                {disableTarget.label ? `${disableTarget.label}. ` : ""}
                The number stays in your carrier account — nothing is released or cancelled.
              </div>
            </div>
          </div>
        </Modal>
      )}

      {deleteTarget && (
        <Modal
          title="Disconnect this carrier?"
          sub="Talqing forgets the credentials and deletes the SIP trunk it created. Your account with the carrier, and the numbers in it, stay exactly as they are."
          width="max-w-[480px]"
          onClose={() => !confirming && setDeleteTarget(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={confirming}>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => void confirmDeleteAccount()}
                disabled={confirming || deleteBlockers.length > 0}
              >
                {confirming ? "Disconnecting…" : "Disconnect carrier"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="font-display text-[14px] font-semibold leading-5 tracking-tight text-ink">
                {deleteTarget.display_name}
              </div>
              <div className="mt-1 text-[13px] leading-5 text-muted">
                {deleteBlockers.length > 0
                  ? "This cannot be undone — reconnecting means re-entering the credentials."
                  : "This cannot be undone. Reconnecting means re-entering the credentials and running setup again."}
              </div>
            </div>
            {deleteBlockers.length > 0 && (
              <div className="mt-3 rounded-lg border border-warn/25 bg-warn/[0.05] px-3.5 py-3">
                <div className="text-[13px] font-medium leading-5 text-ink">
                  Disable {plural(deleteBlockers.length, "number")} first
                </div>
                <p className="mt-1 text-[12.5px] leading-[18px] text-ink-soft">
                  A carrier still carrying numbers cannot be disconnected. Disable{" "}
                  {deleteBlockers.length === 1 ? "it" : "them"} from the row menu, then come back.
                </p>
                <ul className="mt-2 flex flex-wrap gap-1.5">
                  {deleteBlockers.slice(0, 8).map((number) => (
                    <li
                      key={number.id}
                      className="rounded border border-line-2 bg-white px-1.5 py-px font-mono text-[11.5px] leading-5 text-ink-soft"
                    >
                      {number.e164}
                    </li>
                  ))}
                  {deleteBlockers.length > 8 && (
                    <li className="px-1 py-px text-[11.5px] leading-5 text-muted">
                      +{deleteBlockers.length - 8} more
                    </li>
                  )}
                </ul>
              </div>
            )}
          </div>
        </Modal>
      )}
    </AppShell>
  );
}
