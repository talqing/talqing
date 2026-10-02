"use client";

import Link from "next/link";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { AgentResponse, IntegrationResponse, IntegrationTriggerResponse } from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { Button, Container, EmptyState, Modal, PageHead, Skeleton, useToast } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { saveTrigger, type TriggerType } from "@/lib/triggers";
import { formatPhone, whatsAppSenders } from "../outbound/shared";
import { WhatsAppIcon } from "../outbound/icons";
import { NumberGroup, type Account, type CellState } from "./NumberGroup";
import { WhatsAppConnect } from "./WhatsAppConnect";

const isPublished = (channel: "text" | "voice") => (agent: AgentResponse) =>
  agent.config?.channel === channel && Boolean(agent.published_version);

function WhatsAppNumbers() {
  const router = useRouter();
  const params = useSearchParams();
  const toast = useToast();
  const [numbers, setNumbers] = useState<IntegrationResponse[] | null>(null);
  const [triggers, setTriggers] = useState<Record<string, IntegrationTriggerResponse[]>>({});
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [loadError, setLoadError] = useState("");
  const [cells, setCells] = useState<Record<string, CellState>>({});
  const [resuming, setResuming] = useState("");
  // Connecting new numbers (null), or replacing the credential of these.
  const [connect, setConnect] = useState<{ editing: IntegrationResponse[] | null } | null>(null);
  const [disconnectTarget, setDisconnectTarget] = useState<IntegrationResponse | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  const load = useCallback(async () => {
    try {
      const [integrations, allAgents] = await Promise.all([api.listIntegrations(), api.listAllAgents()]);
      const senders = whatsAppSenders(integrations.items);
      const pages = await Promise.all(senders.map((s) => api.listIntegrationTriggers(s.id)));
      setTriggers(Object.fromEntries(senders.map((s, i) => [s.id, pages[i].items])));
      setNumbers(senders);
      setAgents(allAgents);
      setLoadError("");
    } catch (error) {
      setNumbers((current) => current ?? []);
      setLoadError(apiErrorMessage(error));
    }
  }, []);

  useEffect(() => void load(), [load]);

  // `?connect=1` is how the Integrations catalog and the Outbound page send
  // someone here to connect their first number.
  useEffect(() => {
    if (params.get("connect") !== "1") return;
    setConnect({ editing: null });
    router.replace("/whatsapp/numbers");
  }, [params, router]);

  const textAgents = useMemo(() => agents.filter(isPublished("text")), [agents]);
  const voiceAgents = useMemo(() => agents.filter(isPublished("voice")), [agents]);

  /* Twilio numbers group under their account, which is also what one auth
     token rotates. A Gupshup app has exactly one number, so it is its own. */
  const accounts = useMemo(() => {
    const groups = new Map<string, Account>();
    for (const number of numbers ?? []) {
      const info = number.provider_account_info ?? {};
      const bsp = info.bsp === "gupshup" ? "gupshup" : "twilio";
      const key = bsp === "twilio" ? `twilio:${info.account_sid}` : `gupshup:${number.id}`;
      const group = groups.get(key) ?? { key, bsp, numbers: [] };
      group.numbers.push(number);
      groups.set(key, group);
    }
    for (const group of groups.values()) {
      group.numbers.sort((a, b) =>
        String(a.provider_account_info?.sender_e164).localeCompare(String(b.provider_account_info?.sender_e164)),
      );
    }
    return [...groups.values()];
  }, [numbers]);

  async function save(number: IntegrationResponse, triggerType: TriggerType, agentId: string, enabled: boolean) {
    const key = `${number.id}:${triggerType}`;
    setCells((c) => ({ ...c, [key]: { busy: true, error: "" } }));
    let error = "";
    try {
      const existing = triggers[number.id]?.find((t) => t.trigger_type === triggerType);
      await saveTrigger(number.id, existing, { triggerType, agentId, enabled });
    } catch (e) {
      error = apiErrorMessage(e);
    }
    // Re-read either way: a refused enable leaves the trigger in `error`.
    try {
      const page = await api.listIntegrationTriggers(number.id);
      setTriggers((t) => ({ ...t, [number.id]: page.items }));
    } catch (e) {
      error ||= apiErrorMessage(e);
    }
    setCells((c) => ({ ...c, [key]: { busy: false, error } }));
  }

  async function resume(number: IntegrationResponse) {
    setResuming(number.id);
    try {
      await api.patchIntegration(number.id, { status: "active" });
      toast({ kind: "ok", msg: `${formatPhone(String(number.provider_account_info?.sender_e164))} resumed.` });
    } catch (e) {
      toast({ kind: "err", msg: apiErrorMessage(e) });
    } finally {
      setResuming("");
      await load();
    }
  }

  async function confirmDisconnect() {
    if (!disconnectTarget) return;
    setDisconnecting(true);
    try {
      await api.deleteIntegration(disconnectTarget.id);
      toast({
        kind: "ok",
        msg: `${formatPhone(String(disconnectTarget.provider_account_info?.sender_e164))} disconnected.`,
      });
      setDisconnectTarget(null);
    } catch (e) {
      toast({ kind: "err", msg: apiErrorMessage(e) });
    } finally {
      setDisconnecting(false);
      await load();
    }
  }

  const connectButton = <Button onClick={() => setConnect({ editing: null })}>Connect number</Button>;
  const hasTwilio = (numbers ?? []).some((n) => n.provider_account_info?.bsp === "twilio");

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Numbers"
          sub="The WhatsApp numbers your agents answer."
          actions={connectButton}
        />

        {loadError && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger [overflow-wrap:anywhere]">
            {loadError}
          </div>
        )}

        {numbers === null ? (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            <div className="flex items-center gap-3.5 border-b border-line bg-canvas px-4 py-3.5">
              <Skeleton className="h-9 w-9 rounded-[10px]" />
              <Skeleton className="h-4 w-[160px]" />
            </div>
            {[0, 1].map((row) => (
              <div key={row} className="flex items-center gap-4 border-b border-line px-4 py-3.5 last:border-b-0">
                <Skeleton className="h-[15px] w-[18%]" />
                <Skeleton className="h-9 flex-1 rounded-[10px]" />
                <Skeleton className="h-9 flex-1 rounded-[10px]" />
                <Skeleton className="hidden h-3 w-16 sm:block" />
              </div>
            ))}
          </div>
        ) : numbers.length === 0 ? (
          <EmptyState
            icon={<WhatsAppIcon className="h-[22px] w-[22px]" />}
            title="Connect your first WhatsApp number"
            body="Bring a number you have at Twilio or Gupshup, and choose the agents that answer its messages and calls."
            cta={connectButton}
          />
        ) : (
          <>
            <div className="grid gap-4">
              {accounts.map((account) => (
                <NumberGroup
                  key={account.key}
                  account={account}
                  triggers={triggers}
                  textAgents={textAgents}
                  voiceAgents={voiceAgents}
                  cells={cells}
                  resuming={resuming}
                  onSave={(number, triggerType, agentId, enabled) =>
                    void save(number, triggerType, agentId, enabled)
                  }
                  onResume={(number) => void resume(number)}
                  onUpdateCredential={(editing) => setConnect({ editing })}
                  onDisconnect={setDisconnectTarget}
                />
              ))}
            </div>
            {(textAgents.length === 0 || (hasTwilio && voiceAgents.length === 0)) && (
              <p className="mt-3 text-[13px] leading-5 text-muted">
                {textAgents.length === 0 ? "No published text agents yet. " : "No published voice agents yet. "}
                <Link href="/agents" className="font-medium text-ink underline underline-offset-2">
                  Publish one
                </Link>{" "}
                to answer {textAgents.length === 0 ? "messages" : "calls"}.
              </p>
            )}
          </>
        )}
      </Container>

      {connect && (
        <WhatsAppConnect
          editing={connect.editing}
          connected={numbers ?? []}
          onClose={() => setConnect(null)}
          onChanged={(message, done) => {
            toast({ kind: "ok", msg: message });
            if (done) setConnect(null);
            void load();
          }}
        />
      )}

      {disconnectTarget && (
        <Modal
          title={`Disconnect ${formatPhone(String(disconnectTarget.provider_account_info?.sender_e164))}?`}
          width="max-w-[480px]"
          onClose={() => !disconnecting && setDisconnectTarget(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDisconnectTarget(null)} disabled={disconnecting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={() => void confirmDisconnect()} disabled={disconnecting}>
                {disconnecting ? "Disconnecting…" : "Disconnect number"}
              </Button>
            </>
          }
        >
          <p className="pb-2 text-[13.5px] leading-5 text-ink-soft">
            {disconnectTarget.provider_account_info?.bsp === "twilio"
              ? "Talqing stops answering it and clears its webhook and calling in Twilio. The number stays in your Twilio account."
              : "Talqing stops answering it and removes its webhook from your Gupshup app. The number stays in your Gupshup app."}
          </p>
        </Modal>
      )}
    </AppShell>
  );
}

// useSearchParams() suspends during the static export's prerender.
export default function WhatsAppNumbersPage() {
  return (
    <Suspense fallback={null}>
      <WhatsAppNumbers />
    </Suspense>
  );
}
