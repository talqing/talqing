"use client";
import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { Container, PageHead, Panel, CardHead, Button, Input, Badge, EmptyState, ListSkeleton } from "../components/ui";
import { api, controlApi } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";

const TokenIcon = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="7.5" cy="15.5" r="3.5" />
    <path d="M10.2 12.8 20 3m-4.5 1.5 3 3M12.5 10.5l2.5 2.5" />
  </svg>
);

type Token = {
  id: string;
  name: string;
  kind: "manual" | "mcp";
  user_id: string;
  user_email: string;
  created_at: string;
};

export default function TokensPage() {
  const [tokens, setTokens] = useState<Token[]>([]);
  const [me, setMe] = useState<{ user?: any } | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<{ name: string; token: string } | null>(null);
  const [copied, setCopied] = useState(false);

  async function load() {
    try {
      const [ctx, tokensPage] = await Promise.all([controlApi.me(), controlApi.listTokens()]);
      const rows = tokensPage.items;
      setMe(ctx);
      setTokens(rows);
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
      const t = await controlApi.createToken(name.trim());
      setFresh({ name: t.name, token: t.token });
      setCopied(false);
      setName("");
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function del(id: string) {
    setErr("");
    try {
      await controlApi.deleteToken(id);
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function copy() {
    if (!fresh) return;
    await navigator.clipboard.writeText(fresh.token);
    setCopied(true);
  }

  const role = me?.user?.role || "…";
  const myUserId = me?.user?.id as string | undefined;
  const isAdmin = role === "ADMIN";

  return (
    <AppShell>
      <Container>
        <PageHead
          title="API tokens"
          sub={
            <>
              Drive the talqing API from your own code — send one as{" "}
              <code className="rounded border border-line bg-subtle px-1 py-0.5 font-mono text-[12px]">Authorization: Bearer &lt;token&gt;</code>.
            </>
          }
        />

        {err && (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {fresh && (
          <Panel className="mb-5">
            <CardHead title={`“${fresh.name}” created`} />
            <p className="mb-3 text-[13px] leading-5 text-muted">Copy it now — it won&apos;t be shown again.</p>
            <div className="flex items-center gap-3">
              <Input
                readOnly
                value={fresh.token}
                onFocus={(e) => e.target.select()}
                className="flex-1 font-mono text-[12.5px]"
              />
              <Button onClick={copy}>{copied ? "Copied ✓" : "Copy"}</Button>
              <Button variant="secondary" size="sm" onClick={() => setFresh(null)}>Done</Button>
            </div>
          </Panel>
        )}

        <form onSubmit={create} className="mb-5">
          <Panel>
            <CardHead title="Create a token" />
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex min-w-[240px] flex-1 flex-col gap-2">
                <label className="text-[14px] font-semibold leading-5 text-ink">Name (what&apos;s it for?)</label>
                <Input placeholder="ci-deploy" value={name} onChange={(e) => setName(e.target.value)} className="font-mono" />
              </div>
              <Button disabled={!name.trim()}>Create token</Button>
            </div>
          </Panel>
        </form>

        {loading ? (
          <ListSkeleton rows={3} />
        ) : tokens.length === 0 ? (
          <EmptyState
            icon={TokenIcon}
            title="No tokens yet."
            body="Create one to call the talqing API from scripts, CI, or your own backend."
          />
        ) : (
          <div className="overflow-hidden rounded-xl border border-line-2 bg-white shadow-none">
            {tokens.map((t) => {
              const mine = myUserId != null && t.user_id === myUserId;
              const canDelete = mine || isAdmin;
              return (
                <div key={t.id} className="flex items-center gap-3.5 border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-hover">
                  <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-subtle text-ink-soft">{TokenIcon}</span>
                  <div className="min-w-0 flex flex-col gap-0.5">
                    <span className="font-mono text-[13.5px] font-semibold text-ink">{t.name}</span>
                    <span className="truncate text-[12px] text-muted">
                      {mine ? "you" : t.user_email}
                    </span>
                  </div>
                  <span className="text-[12.5px] text-muted">created {new Date(t.created_at).toLocaleDateString()}</span>
                  <div className="flex-1" />
                  {t.kind === "mcp" && <Badge variant="info">MCP</Badge>}
                  {mine && <Badge>yours</Badge>}
                  {canDelete ? (
                    <Button variant="danger" size="sm" onClick={() => del(t.id)}>Delete</Button>
                  ) : (
                    <span className="text-[12px] text-placeholder">owner or admin only</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Container>
    </AppShell>
  );
}
