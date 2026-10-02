"use client";

import { useState } from "react";
import type { IntegrationResponse, TwilioSender } from "@talqing/sdk";
import { Badge, BoxCheckbox, Button, Field, Input, Modal, Segment } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import { talqing } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { formatPhone } from "../outbound/shared";

type Bsp = "twilio" | "gupshup";

/* Connecting WhatsApp numbers the user already has at a BSP, or replacing the
 * credential of ones already connected.
 *
 * Twilio's senders are PICKED from the account rather than typed, several at a
 * time: each becomes its own integration, and the first one's stored secret is
 * shared by the rest. A Gupshup app has exactly one number. */
export function WhatsAppConnect({
  editing,
  connected,
  onClose,
  onChanged,
}: {
  /** The numbers whose credential is being replaced — one Gupshup number, or
      every number on one Twilio account. Null connects new ones. */
  editing: IntegrationResponse[] | null;
  /** Every WhatsApp number already connected, so a sender is not offered twice. */
  connected: IntegrationResponse[];
  onClose: () => void;
  /** Something was saved. The modal closes itself only when all of it was. */
  onChanged: (message: string, done: boolean) => void;
}) {
  const editBsp = editing?.[0]?.provider_account_info?.bsp;
  const [bsp, setBsp] = useState<Bsp>("twilio");
  const [credential, setCredential] = useState("");
  const [accountSid, setAccountSid] = useState("");
  const [senders, setSenders] = useState<TwilioSender[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [appId, setAppId] = useState("");
  const [appName, setAppName] = useState("");
  const [number, setNumber] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const connectedNumbers = new Set(connected.map((i) => String(i.provider_account_info?.sender_e164)));

  async function findSenders() {
    setBusy(true);
    setErr("");
    try {
      const found = await talqing.whatsapp.twilioSenders.list({
        twilioSendersRequest: { account_sid: accountSid.trim(), auth_token: credential.trim() },
      });
      setSenders(found.senders);
      const open = found.senders.filter((s) => s.status === "ONLINE" && !connectedNumbers.has(s.e164));
      setPicked(open.length === 1 ? [open[0].e164] : []);
      if (!found.senders.length) setErr("This Twilio account has no WhatsApp senders yet.");
    } catch (error) {
      setSenders(null);
      setErr(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  /* Each PATCH re-checks the credential with the BSP, so a wrong one fails on
     the first number and nothing else changes. The old secret stays in Secrets. */
  async function rotate() {
    if (!editing) return;
    let ref = credential.trim();
    for (const integration of editing) {
      const saved = await talqing.integrations.update({
        integration_id: integration.id,
        patchIntegrationRequest: { credentials_ref: ref },
      });
      ref = saved.credentials_ref ?? ref;
    }
    onChanged(
      editBsp === "gupshup" ? "API key updated." : "Auth token updated on every number on this account.",
      true,
    );
  }

  async function connectTwilio() {
    let ref = credential.trim();
    const done: string[] = [];
    for (const e164 of picked) {
      try {
        const created = await talqing.integrations.create({
          createIntegrationRequest: {
            display_name: `WhatsApp ${e164}`,
            provider: "whatsapp",
            credentials_ref: ref,
            provider_account_info: { bsp: "twilio", account_sid: accountSid.trim(), sender_e164: e164 },
          },
        });
        // The pasted token is stored once; the rest of the account shares it.
        ref = created.credentials_ref ?? ref;
        done.push(e164);
      } catch (error) {
        if (done.length) {
          setPicked(picked.filter((p) => !done.includes(p)));
          setCredential(ref);
          onChanged(`Connected ${done.map(formatPhone).join(", ")}.`, false);
        }
        throw error;
      }
    }
    onChanged(`Connected ${done.map(formatPhone).join(", ")}.`, true);
  }

  async function connectGupshup() {
    const e164 = number.trim();
    await talqing.integrations.create({
      createIntegrationRequest: {
        display_name: `WhatsApp ${e164}`,
        provider: "whatsapp",
        credentials_ref: credential.trim(),
        provider_account_info: { bsp: "gupshup", app_id: appId.trim(), app_name: appName.trim(), sender_e164: e164 },
      },
    });
    onChanged(`Connected ${formatPhone(e164)}.`, true);
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      if (editing) await rotate();
      else if (bsp === "twilio") await connectTwilio();
      else await connectGupshup();
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  const ready = editing
    ? Boolean(credential.trim())
    : bsp === "twilio"
      ? Boolean(accountSid.trim() && credential.trim() && picked.length)
      : Boolean(appId.trim() && appName.trim() && credential.trim() && number.trim());

  const title = !editing
    ? "Connect WhatsApp number"
    : editBsp === "gupshup"
      ? "Update API key"
      : "Update auth token";
  const submitLabel = editing
    ? "Save"
    : bsp === "twilio" && picked.length > 1
      ? `Connect ${picked.length} numbers`
      : "Connect";

  return (
    <Modal
      title={title}
      sub={
        editing
          ? editBsp === "gupshup"
            ? `For ${formatPhone(String(editing[0].provider_account_info?.sender_e164))}. From the app's Settings → API Keys.`
            : `Every number on this Twilio account switches to it.`
          : "A number already registered for WhatsApp at Twilio or Gupshup."
      }
      onClose={() => !busy && onClose()}
      width="max-w-[560px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="whatsapp-form" disabled={busy || !ready}>
            {busy ? "Saving…" : submitLabel}
          </Button>
        </>
      }
    >
      <form id="whatsapp-form" onSubmit={save} className="grid gap-4 pb-2">
        {err && (
          <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger [overflow-wrap:anywhere]">
            {err}
          </div>
        )}

        {editing ? (
          <Field
            label={editBsp === "gupshup" ? "New API key" : "New auth token"}
            hint={editBsp === "gupshup" ? undefined : "Talqing checks it with Twilio before saving."}
          >
            <Input
              type="password"
              autoComplete="off"
              autoFocus
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              placeholder="Paste it, or {{secrets.NAME}}"
              className="font-mono"
            />
          </Field>
        ) : (
          <>
            <Field label="Provider">
              <Segment
                equal
                value={bsp}
                onChange={(next) => {
                  setBsp(next);
                  setErr("");
                  setCredential("");
                }}
                options={[
                  { value: "twilio", label: "Twilio" },
                  { value: "gupshup", label: "Gupshup" },
                ]}
              />
            </Field>

            {bsp === "twilio" ? (
              <>
                <Field label="Account SID">
                  <Input
                    value={accountSid}
                    onChange={(e) => {
                      setAccountSid(e.target.value);
                      setSenders(null);
                    }}
                    placeholder="AC…"
                    className="font-mono"
                  />
                </Field>
                <Field
                  label="Auth token"
                  hint="Not an API key: Twilio signs its webhooks with the auth token."
                >
                  <Input
                    type="password"
                    autoComplete="off"
                    value={credential}
                    onChange={(e) => {
                      setCredential(e.target.value);
                      setSenders(null);
                    }}
                    placeholder="Paste it, or {{secrets.NAME}}"
                    className="font-mono"
                  />
                </Field>
                <Field label="Numbers">
                  {senders ? (
                    <SenderList
                      senders={senders}
                      connected={connectedNumbers}
                      picked={picked}
                      onToggle={(e164, on) =>
                        setPicked((current) => (on ? [...current, e164] : current.filter((p) => p !== e164)))
                      }
                    />
                  ) : (
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={findSenders}
                      disabled={busy || !accountSid.trim() || !credential.trim()}
                      className="self-start"
                    >
                      Find numbers
                    </Button>
                  )}
                </Field>
              </>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="App ID">
                    <Input value={appId} onChange={(e) => setAppId(e.target.value)} className="font-mono" />
                  </Field>
                  <Field label="App name" hint="Exactly as Gupshup shows it.">
                    <Input value={appName} onChange={(e) => setAppName(e.target.value)} />
                  </Field>
                </div>
                <Field label="API key" hint="From the app's Settings → API Keys.">
                  <Input
                    type="password"
                    autoComplete="off"
                    value={credential}
                    onChange={(e) => setCredential(e.target.value)}
                    placeholder="Paste it, or {{secrets.NAME}}"
                    className="font-mono"
                  />
                </Field>
                <Field label="WhatsApp number">
                  <Input
                    value={number}
                    onChange={(e) => setNumber(e.target.value)}
                    placeholder="+919876543210"
                    className="font-mono"
                  />
                </Field>
              </>
            )}
          </>
        )}
      </form>
    </Modal>
  );
}

/** The account's senders, ticked to connect. One that cannot be connected says why. */
function SenderList({
  senders,
  connected,
  picked,
  onToggle,
}: {
  senders: TwilioSender[];
  connected: Set<string>;
  picked: string[];
  onToggle: (e164: string, on: boolean) => void;
}) {
  return (
    <div className="overflow-hidden rounded-[10px] border border-line-2">
      {senders.map((sender) => {
        const isConnected = connected.has(sender.e164);
        const pickable = !isConnected && sender.status === "ONLINE";
        const checked = picked.includes(sender.e164);
        return (
          <label
            key={sender.sid}
            className={cn(
              "flex items-center gap-3 border-b border-line px-3.5 py-2.5 last:border-b-0",
              pickable ? "cursor-pointer hover:bg-canvas" : "cursor-default",
            )}
          >
            <BoxCheckbox
              checked={checked || isConnected}
              disabled={!pickable}
              onChange={(on) => onToggle(sender.e164, on)}
              ariaLabel={`Connect ${sender.e164}`}
            />
            <span
              className={cn(
                "min-w-0 flex-1 truncate font-mono text-[13px] leading-5 tabular-nums",
                pickable ? "text-ink" : "text-muted",
              )}
            >
              {formatPhone(sender.e164)}
            </span>
            {isConnected ? (
              <Badge>Connected</Badge>
            ) : sender.status !== "ONLINE" ? (
              <Badge variant="warn">{sender.status.toLowerCase()}</Badge>
            ) : null}
          </label>
        );
      })}
    </div>
  );
}
