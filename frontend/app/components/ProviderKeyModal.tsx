"use client";

import { useState } from "react";
import type { ProviderKeyResponse } from "@talqing/sdk";
import { Button, Field, Input, Modal, useToast } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";

/* What a provider calls the thing you paste. "API key" is right for eight of
   the ten, and wrong in a way that costs a support ticket for the other two:
   ai-coustics issues an SDK license and rejects anything else by that name, and
   Sarvam's own header and dashboard both say "subscription key". */
const CREDENTIAL_NOUN: Record<string, string> = {
  aicoustics: "SDK license key",
  sarvam: "API subscription key",
};

const credentialNoun = (providerId: string) => CREDENTIAL_NOUN[providerId] ?? "API key";

/** Paste one provider's key. /byok opens it from a row; an editor opens it when
 *  a missing key is the only thing standing between a draft and a publish. */
export function ProviderKeyModal({
  provider,
  storedKey,
  onSaved,
  onRemoved,
  onClose,
}: {
  provider: { id: string; label: string };
  /** The key this workspace already holds, when it holds one. */
  storedKey: ProviderKeyResponse | null;
  /** The key is stored and verified — the caller takes it from here, this
   *  dialog does not close itself. */
  onSaved: () => void;
  /** Omitted where removing a key is not the caller's business: an editor opens
   *  this dialog precisely because there is no key to remove. */
  onRemoved?: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      // The API calls the provider before it stores anything, so this resolving
      // at all means the key works — and for the providers that name the
      // account behind it, says whose it is.
      const saved = await api.setProviderKey(provider.id, apiKey.trim());
      toast({
        kind: "ok",
        msg: saved.account_label
          ? `${provider.label} key verified — ${saved.account_label}.`
          : `${provider.label} key verified.`,
      });
      onSaved();
    } catch (error) {
      toast({ kind: "err", msg: apiErrorMessage(error) });
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!onRemoved) return;
    setSaving(true);
    try {
      await api.deleteProviderKey(provider.id);
      toast({ kind: "ok", msg: `${provider.label} key removed.` });
      onRemoved();
    } catch (error) {
      toast({ kind: "err", msg: apiErrorMessage(error) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={`${provider.label} ${credentialNoun(provider.id)}`}
      sub={
        storedKey
          ? `Currently ${[storedKey.account_label, storedKey.api_key_hint]
              .filter(Boolean)
              .join(" · ")}, verified ${new Date(
              storedKey.updated_at,
            ).toLocaleDateString()}. Saving replaces it — agents pick up the new key on their next run.`
          : `Paste a key from your ${provider.label} account. Talqing never shows it again.`
      }
      width="max-w-[520px]"
      onClose={onClose}
      footer={
        <>
          {storedKey && onRemoved && (
            <Button
              type="button"
              variant="danger"
              onClick={remove}
              disabled={saving}
              className="mr-auto"
            >
              Remove key
            </Button>
          )}
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="byok-form" disabled={saving || !apiKey.trim()}>
            {saving ? "Verifying…" : storedKey ? "Replace key" : "Save key"}
          </Button>
        </>
      }
    >
      {/* No identity well: the title names the provider and the subtitle
          carries the stored key's account, hint and date, so a panel
          repeating them was a box that said nothing new. */}
      <form id="byok-form" onSubmit={save} className="grid gap-4 pb-2">
        <Field
          label={credentialNoun(provider.id)}
          hint={`Checked against ${provider.label} before it is saved. A key they reject is not stored.`}
        >
          <Input
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>
      </form>
    </Modal>
  );
}
