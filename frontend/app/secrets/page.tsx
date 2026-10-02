"use client";
import { useEffect, useState } from "react";
import type { SecretResponse } from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { Container, PageHead, Panel, CardHead, Button, Input, Label, Badge, EmptyState, ListSkeleton, Modal } from "@/app/components/ui";
import { api } from "@/lib/api";
import { useActiveRegion } from "@/lib/regions";
import { apiErrorMessage } from "@/lib/apiError";

const KeyIcon = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <rect x="4" y="10" width="16" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v2" />
  </svg>
);

export default function SecretsPage() {
  const region = useActiveRegion();
  const [secrets, setSecrets] = useState<SecretResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<SecretResponse | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState("");

  async function load() {
    try {
      setSecrets((await api.listSecrets()).items);
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      await api.createSecret({ name: name.trim(), value });
      setName("");
      setValue("");
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }
  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteErr("");
    try {
      await api.deleteSecret(deleteTarget.id);
      setDeleteTarget(null);
      await load();
    } catch (error) {
      // The server refuses a secret something still references, and names it.
      setDeleteErr(apiErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }
  function closeDelete() {
    setDeleteTarget(null);
    setDeleteErr("");
  }

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Secrets"
          sub={
            <>
              Write-only tool credentials, referenced from an operation as <code className="rounded border border-line bg-subtle px-1 py-0.5 font-mono text-[12px]">{"{{secrets.NAME}}"}</code>.
            </>
          }
        />

        {err && (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        <form onSubmit={create} className="mb-5">
          <Panel>
            <CardHead title="Add or replace a secret" />
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex w-[240px] flex-none flex-col gap-2">
                <Label>Name</Label>
                <Input placeholder="STRIPE_KEY" value={name} onChange={(e) => setName(e.target.value)} className="font-mono" />
              </div>
              <div className="flex min-w-[240px] flex-1 flex-col gap-2">
                <Label>Value (token, key, …)</Label>
                <Input type="password" placeholder="sk_live_…" value={value} onChange={(e) => setValue(e.target.value)} />
              </div>
              <Button disabled={!name.trim() || !value}>Save secret</Button>
            </div>
            <p className="mt-3 text-[12.5px] leading-5 text-muted">
              Name must match <code className="rounded border border-line bg-subtle px-1 py-0.5 font-mono text-[12px]">[A-Za-z_][A-Za-z0-9_]*</code>.
              Reusing a name rotates the value and updates the masked hint.
            </p>
          </Panel>
        </form>

        {loading ? (
          <ListSkeleton rows={3} />
        ) : secrets.length === 0 ? (
          <EmptyState icon={KeyIcon} title={`No secrets in ${region.name}.`} body="Store API tokens and keys here, then reference them from any tool operation by name." />
        ) : (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white shadow-none">
            {secrets.map((s) => (
              <div key={s.id} className="flex items-center gap-3.5 border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-hover">
                <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-subtle text-ink-soft">{KeyIcon}</span>
                <span className="font-mono text-[13.5px] font-semibold text-ink">{s.name}</span>
                <span className="font-mono text-[12.5px] text-muted">{s.value_hint}</span>
                <div className="flex-1" />
                <span className="text-[12px] text-placeholder" title="Last rotated">
                  {s.updated_at ? new Date(s.updated_at).toLocaleString() : ""}
                </span>
                <Badge>write-only</Badge>
                <Button variant="danger" size="sm" onClick={() => setDeleteTarget(s)}>Delete</Button>
              </div>
            ))}
          </div>
        )}
      </Container>

      {deleteTarget && (
        <Modal
          title="Delete secret?"
          sub="The value is gone for good. Tools, integrations and carrier accounts that still reference it block the delete."
          width="max-w-[460px]"
          onClose={() => !deleting && closeDelete()}
          footer={
            <>
              <Button variant="secondary" onClick={closeDelete} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDelete} disabled={deleting || !!deleteErr}>
                {deleting ? "Deleting…" : "Delete secret"}
              </Button>
            </>
          }
        >
          <div className="pb-2">
            <div className="flex items-baseline gap-2.5 rounded-lg border border-line bg-canvas px-3.5 py-3">
              <span className="font-mono text-[13px] font-semibold text-ink">{deleteTarget.name}</span>
              <span className="font-mono text-[12.5px] text-muted">{deleteTarget.value_hint}</span>
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
