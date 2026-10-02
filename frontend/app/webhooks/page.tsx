"use client";
import { useEffect, useState } from "react";
import { AppShell } from "@/app/components/AppShell";
import { Container, PageHead, Panel, CardHead, Button, Input, Label, Badge, EmptyState, ListSkeleton } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";

const HookIcon = (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 9a3 3 0 1 1 4 2.8l2.5 4.2M15 14a3 3 0 1 1-2.6 4.5H8M9 12a3 3 0 1 1-2.4 4.8" />
  </svg>
);

export default function WebhooksPage() {
  const region = useActiveRegion();
  const [hooks, setHooks] = useState<any[]>([]);
  const [allEvents, setAllEvents] = useState<string[]>([]);
  const [liveEvents, setLiveEvents] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  // create form
  const [creating, setCreating] = useState(false);
  const [url, setUrl] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [newSecret, setNewSecret] = useState("");

  // per-row UI
  const [open, setOpen] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<Record<string, any[]>>({});
  const [flash, setFlash] = useState("");

  async function load() {
    try {
      const [hs, et] = await Promise.all([
        api.listWebhooks().then((p) => p.items),
        api.eventTypes(),
      ]);
      setHooks(hs);
      setAllEvents(et.all);
      setLiveEvents(et.live);
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  function toggle(ev: string) {
    setPicked((p) => {
      const n = new Set(p);
      n.has(ev) ? n.delete(ev) : n.add(ev);
      return n;
    });
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (!/^https?:\/\//.test(url)) {
      setErr("URL must start with http:// or https://");
      return;
    }
    try {
      const w = await api.createWebhook({ url: url.trim(), subscribed_events: Array.from(picked) });
      setNewSecret(w.secret || "");
      setUrl("");
      setPicked(new Set());
      setCreating(false);
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function toggleStatus(w: any) {
    await api.patchWebhook(w.id, { status: w.status === "active" ? "disabled" : "active" });
    await load();
  }

  async function fire(w: any) {
    setFlash("");
    const r = await api.testWebhook(w.id);
    setFlash(
      r.status === "delivered"
        ? `Test delivered to ${w.url} (HTTP ${r.status_code})`
        : `Test failed: ${r.error || `HTTP ${r.status_code}`}`,
    );
    await load();
    if (open === w.id) await showDeliveries(w.id);
  }

  async function del(w: any) {
    await api.deleteWebhook(w.id);
    await load();
  }

  async function showDeliveries(id: string) {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    setDeliveries((d) => ({ ...d, [id]: d[id] || [] }));
    const page = await api.webhookDeliveries(id);
    setDeliveries((d) => ({ ...d, [id]: page.items }));
  }

  const showForm = creating || (!loading && hooks.length === 0);

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Webhooks"
          /* NAMING THE REGION IS LOAD-BEARING HERE, not decoration. A webhook
             subscription belongs to one region, so one created here never fires
             for a call in another — correct by construction, and completely
             invisible. A tenant silently missing half their events is the worst
             failure this shape can produce, and it costs one line of copy to
             prevent. */
          sub={
            <>
              Signed lifecycle events for every agent in {region.name}, HMAC-stamped with{" "}
              <code className="rounded border border-line bg-subtle px-1 py-0.5 font-mono text-[12px]">X-Talqing-Signature</code>.
              {" "}A subscription covers this region only — add one in each region you run agents in.
            </>
          }
          actions={
            !loading && hooks.length > 0 ? (
              <Button onClick={() => setCreating((v) => !v)}>+ Add webhook</Button>
            ) : undefined
          }
        />

        {err && (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}
        {flash && (
          <div className="mb-3 rounded-md border border-live/30 bg-live/10 px-3.5 py-2.5 text-[13.5px] text-live">
            {flash}
          </div>
        )}

        {newSecret && (
          <Panel className="mb-4 border-line-2">
            <div className="mb-2 text-[13.5px] font-semibold text-ink">Signing secret — shown once, copy it now:</div>
            <div className="mt-2 overflow-x-auto rounded-md border border-line bg-subtle px-3 py-2.5 font-mono text-[12.5px] text-ink">
              {newSecret}
            </div>
          </Panel>
        )}

        {showForm && (
          <form onSubmit={create} className="mb-5 animate-fade-in">
            <Panel>
              <CardHead title="Add a webhook" />
              <div className="mb-4 flex gap-3">
                <Input
                  placeholder="https://your-server.example.com/talqing-hook"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  className="flex-1 font-mono"
                />
                <Button disabled={!url.trim()}>Create</Button>
                {hooks.length > 0 && (
                  <Button type="button" variant="ghost" onClick={() => setCreating(false)}>Cancel</Button>
                )}
              </div>
              <Label className="mb-2 block">
                Events (none selected = all). Greyed types are emitted in a later phase.
              </Label>
              <div className="flex flex-wrap gap-2">
                {allEvents.map((ev) => {
                  const live = liveEvents.includes(ev);
                  const on = picked.has(ev);
                  return (
                    <button
                      type="button"
                      key={ev}
                      onClick={() => toggle(ev)}
                      title={live ? "" : "emitted in a later phase"}
                      className={cn(
                        on
                          ? "border-ink bg-ink text-white"
                          : "border-line-2 bg-subtle text-ink-soft hover:border-line-strong hover:text-ink-hover",
                        !live && "opacity-45",
                        "cursor-pointer rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
                      )}
                    >
                      {ev}
                    </button>
                  );
                })}
              </div>
            </Panel>
          </form>
        )}

        {loading ? (
          <ListSkeleton rows={3} />
        ) : hooks.length === 0 ? null : (
          <div className="flex flex-col gap-3">
            {hooks.map((w) => (
              <Panel key={w.id} className="p-4">
                <div className="flex flex-wrap items-center gap-2.5">
                  <Badge variant={w.status === "active" ? "live" : "default"} dot={w.status === "active"}>
                    {w.status}
                  </Badge>
                  <strong className="truncate font-mono text-[13.5px] text-ink">{w.url}</strong>
                  <div className="flex-1" />
                  {w.last_status && (
                    <span className="text-[12.5px] text-muted">last: {w.last_status}</span>
                  )}
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <span className="text-[12.5px] text-muted">all agents</span>
                  <span className="text-placeholder">·</span>
                  <span className="text-[12.5px] text-muted">
                    {w.subscribed_events.length ? w.subscribed_events.join(", ") : "all events"}
                  </span>
                  <span className="text-placeholder">·</span>
                  <span className="font-mono text-[12.5px] text-muted">{w.secret_hint}</span>
                  <div className="flex-1" />
                  <Button variant="secondary" size="sm" onClick={() => fire(w)}>Test</Button>
                  <Button variant="secondary" size="sm" onClick={() => toggleStatus(w)}>
                    {w.status === "active" ? "Disable" : "Enable"}
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => showDeliveries(w.id)}>
                    {open === w.id ? "Hide log" : "Log"}
                  </Button>
                  <Button variant="danger" size="sm" onClick={() => del(w)}>Delete</Button>
                </div>
                {open === w.id && (
                  <div className="mt-3 border-t border-line pt-3">
                    {(deliveries[w.id] || []).length === 0 ? (
                      <p className="m-0 text-[13px] text-muted">No deliveries yet.</p>
                    ) : (
                      (deliveries[w.id] || []).map((d, i) => (
                        <div key={i} className="py-[3px] font-mono text-[12px] text-muted">
                          {new Date(d.created_at).toLocaleTimeString()} · {d.event_type} ·{" "}
                          <span className={cn("font-semibold", d.status === "delivered" ? "text-live" : "text-ink")}>
                            {d.status}
                          </span>{" "}
                          {d.status_code ? `HTTP ${d.status_code}` : ""} · {d.duration_ms}ms
                          {d.error ? ` · ${d.error}` : ""}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </Panel>
            ))}
          </div>
        )}

        {!loading && hooks.length === 0 && !showForm && (
          <EmptyState icon={HookIcon} title={`No webhooks in ${region.name}.`} body="Get notified when sessions start or end, transcripts finalize, or tools fire — POSTed to your server, signed with an HMAC." />
        )}
      </Container>
    </AppShell>
  );
}
