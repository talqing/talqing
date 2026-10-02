"use client";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { AuthContextResponse, InviteResponse, MemberResponse, RoleRequest } from "@/lib/control";
import { AppShell } from "@/app/components/AppShell";
import { Billing } from "./Billing";
import {
  Badge,
  Button,
  CardHead,
  Container,
  CopyButton,
  EmptyState,
  Field,
  Input,
  ListSkeleton,
  Modal,
  PageHead,
  Panel,
  Segment,
  Select,
} from "@/app/components/ui";
import { api, controlApi } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { adoptRegionFromUrl } from "@/lib/regions";

const PeopleIcon = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="9" cy="8" r="3.5" /><path d="M3.5 20a5.5 5.5 0 0 1 11 0M15.5 5a3.5 3.5 0 0 1 0 7M16.5 14.6a5.5 5.5 0 0 1 4 5.4" />
  </svg>
);

const ROLES = ["ADMIN", "EDITOR", "VIEWER"] as const;

/* A VIEWER is deliberately not read-only: they can mint a call token, so they
   can try an agent out. Saying "read-only" here would be a lie the product
   tells about its own permissions. */
const ROLE_BLURB: Record<string, string> = {
  ADMIN: "everything, including members, secrets and provider keys",
  EDITOR: "can build and run everything except secrets and provider keys",
  VIEWER: "can view everything and test agents, but can't change anything",
};

type Tab = "general" | "members" | "billing";
const TABS: readonly Tab[] = ["general", "members", "billing"];

function SettingsPageInner() {
  /* Dodo's checkout returns to `?tab=billing&topup=…&region=…`, so the tab has
     to be able to start from the query string rather than always at General. */
  const params = useSearchParams();
  const requested = params.get("tab") as Tab | null;
  const [tab, setTab] = useState<Tab>(
    requested && TABS.includes(requested) ? requested : "general",
  );
  const topupId = params.get("topup");
  const [me, setMe] = useState<AuthContextResponse | null>(null);
  const [orgCount, setOrgCount] = useState(0);
  const [members, setMembers] = useState<MemberResponse[]>([]);
  const [invites, setInvites] = useState<InviteResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<RoleRequest["role"]>("EDITOR");
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [leaving, setLeaving] = useState(false);
  /* Held as the string the input shows, so "" is expressible — that is how the
     page says "keep forever", which is `null` on the wire and the default.
     `savedRetention` is what the server last confirmed, so Save can be disabled
     until something actually changed. Not read off /me: the session context
     carries who you are, not the workspace's data policy. */
  const [retentionDays, setRetentionDays] = useState("");
  const [savedRetention, setSavedRetention] = useState("");
  const [savingRetention, setSavingRetention] = useState(false);

  const isAdmin = me?.user?.role === "ADMIN";

  async function load() {
    try {
      const [ctx, orgs, membersPage] = await Promise.all([
        controlApi.me(),
        controlApi.listOrgs(),
        controlApi.orgMembers(),
      ]);
      setMe(ctx);
      setName(ctx.tenant.name);
      setOrgCount(orgs.items.length);
      // `listOrgs` is the only place the policy is served, and it is readable
      // by every role — someone watching calls disappear needs to be able to
      // see why without asking an admin.
      const days = orgs.items.find((o) => o.id === ctx.tenant.id)?.retention_days;
      setRetentionDays(days == null ? "" : String(days));
      setSavedRetention(days == null ? "" : String(days));
      setMembers(membersPage.items);
      // Pending invites are admin-only on the API, so only an admin asks.
      if (ctx.user.role === "ADMIN") setInvites((await controlApi.listInvites()).items);
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  /* Follow the region the buyer paid into, before anything is polled.
     Credits are held per region, and the dashboard normally picks its region
     from localStorage — which survives the round trip to the payment processor.
     But a buyer who finishes checkout in a different browser, or after clearing
     site data, would land on the DEFAULT region, poll its ledger, find nothing,
     and be told their payment was not seen while the money sits correctly in the
     region they bought it for. That is a support ticket with a frightened
     customer on the end of it, and one query parameter removes the whole class. */
  const paidRegion = params.get("region");
  useEffect(() => {
    if (!paidRegion || !me?.tenant?.id) return;
    adoptRegionFromUrl(me.tenant.id, paidRegion);
  }, [paidRegion, me]);

  async function rename(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setSaving(true);
    try {
      const org = await controlApi.patchOrg({ name: name.trim() });
      // The sidebar reads the name from /me, so re-read rather than patching it
      // in two places and letting them drift.
      setMe((current) => (current ? { ...current, tenant: { ...current.tenant, name: org.name } } : current));
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function saveRetention(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setSavingRetention(true);
    try {
      // Empty means keep forever, which is `null` — the one value that is not a
      // number and the reason this is not a bare number input.
      const trimmed = retentionDays.trim();
      const org = await controlApi.patchOrg({ retention_days: trimmed === "" ? null : Number(trimmed) });
      const next = org.retention_days == null ? "" : String(org.retention_days);
      setRetentionDays(next);
      setSavedRetention(next);
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setSavingRetention(false);
    }
  }

  async function changeRole(member: MemberResponse, role: RoleRequest["role"]) {
    if (role === member.role) return;
    setErr("");
    try {
      const updated = await controlApi.setMemberRole(member.id, role);
      setMembers((ms) => ms.map((m) => (m.id === updated.id ? updated : m)));
      // Demoting yourself changes what the rest of this page may do.
      if (updated.id === me?.user?.id) await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function remove(member: MemberResponse) {
    setErr("");
    try {
      await controlApi.removeMember(member.id);
      setMembers((ms) => ms.filter((m) => m.id !== member.id));
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      const created = await controlApi.createInvite(inviteEmail.trim(), inviteRole);
      setInviteEmail("");
      // An address that already had an account joined on the spot, so it belongs
      // on the roster rather than in the pending list.
      if (created.accepted_at) setMembers((await controlApi.orgMembers()).items);
      else setInvites((is) => [created, ...is]);
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function revoke(id: string) {
    setErr("");
    try {
      await controlApi.revokeInvite(id);
      setInvites((is) => is.filter((i) => i.id !== id));
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function leave() {
    setLeaving(true);
    setErr("");
    try {
      await controlApi.leaveOrg();
      // The cookie still names the organization we just left, so go through a
      // full login rather than a client route into a workspace we cannot read.
      window.location.assign("/login");
    } catch (error) {
      setErr(apiErrorMessage(error));
      setLeaving(false);
      setConfirmLeave(false);
    }
  }

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Organization"
          sub="Its name, who belongs to it, what each of them can do here, and what it has left to spend."
        />

        <Segment<Tab>
          className="mb-5"
          value={tab}
          onChange={setTab}
          options={[
            { value: "general", label: "General" },
            { value: "members", label: "Members", count: members.length || undefined },
            { value: "billing", label: "Billing" },
          ]}
        />

        {err && (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {loading ? (
          <ListSkeleton rows={3} />
        ) : tab === "billing" ? (
          <Billing isAdmin={isAdmin} topupId={topupId} />
        ) : tab === "general" ? (
          <div className="flex flex-col gap-5">
            <Panel>
              <CardHead title="Name" desc="What this organization is called in the switcher and on invites." />
              <form onSubmit={rename} className="flex flex-wrap items-end gap-3">
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={!isAdmin}
                  maxLength={100}
                  className="min-w-[240px] flex-1"
                />
                {isAdmin ? (
                  <Button disabled={saving || !name.trim() || name.trim() === me?.tenant?.name}>
                    {saving ? "Saving…" : "Save"}
                  </Button>
                ) : (
                  <span className="text-[13px] text-muted">Only an admin can rename it.</span>
                )}
              </form>
            </Panel>

            <Panel>
              <CardHead
                title="Data retention"
                desc="How long the content of a call or conversation is kept before it is deleted."
              />
              <form onSubmit={saveRetention} className="flex flex-wrap items-center gap-3">
                <div className="flex items-center gap-2">
                  <Input
                    aria-label="How long call content is kept, in days"
                    type="number"
                    min={1}
                    max={3650}
                    step={1}
                    placeholder="Forever"
                    className="w-[110px] flex-none"
                    value={retentionDays}
                    disabled={!isAdmin || savingRetention}
                    onChange={(e) => setRetentionDays(e.target.value)}
                  />
                  <span className="text-[13.5px] text-ink-soft">days</span>
                </div>
                {isAdmin ? (
                  <>
                    <Button disabled={savingRetention || retentionDays.trim() === savedRetention}>
                      {savingRetention ? "Saving…" : "Save"}
                    </Button>
                    <span className="text-[13px] text-muted">
                      Leave it empty to keep everything forever.
                    </span>
                  </>
                ) : (
                  <span className="text-[13px] text-muted">
                    {savedRetention === "" ? "Kept forever." : ""} Only an admin can change this.
                  </span>
                )}
              </form>
              <p className="mt-3 text-[13px] leading-6 text-muted">
                When the window passes, the recording, transcript, tool calls, summary, extracted
                fields and the phone numbers on the call are deleted. The call itself stays, with
                its duration, outcome and cost, so past invoices still add up.
              </p>
              <p className="mt-2 text-[13px] leading-6 text-muted">
                <strong className="font-semibold text-ink">It applies to calls from now on</strong> —
                anything already here keeps the deadline it was given when it ended, so shortening
                the window never deletes something retroactively. And erasing a person&apos;s last
                remaining call erases what your agents remember about them, so their next call meets
                an agent that has never heard of them.
              </p>
            </Panel>

            <Panel>
              <CardHead
                title="Organization ID"
                desc="Integrations and support requests identify your organization by this."
              />
              <div className="flex items-center gap-2.5">
                <code className="min-w-0 flex-1 truncate rounded-md border border-line bg-subtle px-2.5 py-1.5 font-mono text-[12.5px] text-ink">
                  {me?.tenant?.id}
                </code>
                <CopyButton value={me?.tenant?.id ?? ""} label="Copy" />
              </div>
            </Panel>

            <Panel>
              <CardHead
                title="Leave this organization"
                desc="You lose access to everything in it, and your API tokens for it stop working. Nothing you created is deleted."
              />
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="danger" disabled={orgCount <= 1} onClick={() => setConfirmLeave(true)}>
                  Leave organization
                </Button>
                {orgCount <= 1 && (
                  <span className="text-[13px] text-muted">
                    This is your only organization — create or join another first.
                  </span>
                )}
              </div>
            </Panel>
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            {isAdmin && (
              <Panel>
                <CardHead
                  title="Invite someone"
                  desc="They join the moment they sign in with this address. No email is sent yet, so tell them yourself."
                />
                <form onSubmit={invite} className="flex flex-wrap items-end gap-3">
                  <Field label="Email" htmlFor="invite-email" className="min-w-[240px] flex-1">
                    <Input
                      id="invite-email"
                      type="email"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      placeholder="teammate@acme.com"
                    />
                  </Field>
                  <Field label="Role" className="w-[150px] flex-none">
                    <Select aria-label="Role for the invited member" value={inviteRole} onChange={(e) => setInviteRole(e.target.value as RoleRequest["role"])}>
                      {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </Select>
                  </Field>
                  <Button disabled={!inviteEmail.trim()}>Invite</Button>
                </form>
                <p className="mt-2.5 text-[13px] leading-5 text-muted">{ROLE_BLURB[inviteRole]}</p>
              </Panel>
            )}

            {isAdmin && invites.length > 0 && (
              <Panel>
                <CardHead title="Pending invites" desc="Waiting on a first sign-in with that address." />
                <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
                  {invites.map((i) => (
                    <div key={i.id} className="flex items-center gap-3.5 border-b border-line px-4 py-3 last:border-b-0">
                      <span className="text-[13.5px] font-semibold text-ink">{i.email}</span>
                      <Badge title={ROLE_BLURB[i.role]}>{i.role}</Badge>
                      <span className="text-[12.5px] text-muted">
                        invited {new Date(i.created_at).toLocaleDateString()}
                      </span>
                      <div className="flex-1" />
                      <Button variant="danger" size="sm" onClick={() => revoke(i.id)}>Revoke</Button>
                    </div>
                  ))}
                </div>
              </Panel>
            )}

            {members.length === 0 ? (
              <EmptyState icon={PeopleIcon} title="No members yet." body="Invite someone by email to get started." />
            ) : (
              <div className="overflow-hidden rounded-xl border border-line-2 bg-white shadow-none">
                {members.map((m) => {
                  const isMe = m.id === me?.user?.id;
                  return (
                    <div key={m.id} className="flex items-center gap-3.5 border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-hover">
                      {m.picture_url ? (
                        // eslint-disable-next-line @next/next/no-img-element -- Google avatar, a remote host next/image would have to be allow-listed for
                        <img src={m.picture_url} alt="" className="h-9 w-9 flex-none rounded-[10px] object-cover" />
                      ) : (
                        <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-subtle text-ink-soft">
                          {PeopleIcon}
                        </span>
                      )}
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <span className="truncate text-[13.5px] font-semibold text-ink">{m.name || m.email}</span>
                        {m.name && <span className="truncate text-[12px] text-muted">{m.email}</span>}
                      </div>
                      {isMe && <Badge>you</Badge>}
                      <span className="text-[12.5px] text-muted">
                        joined {new Date(m.joined_at).toLocaleDateString()}
                      </span>
                      <div className="flex-1" />
                      {isAdmin ? (
                        <Select
                          value={m.role}
                          onChange={(e) => changeRole(m, e.target.value as RoleRequest["role"])}
                          title={ROLE_BLURB[m.role]}
                          className="w-[130px] flex-none"
                        >
                          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                        </Select>
                      ) : (
                        <Badge title={ROLE_BLURB[m.role]}>{m.role}</Badge>
                      )}
                      {isAdmin && !isMe && (
                        <Button variant="danger" size="sm" onClick={() => remove(m)}>Remove</Button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {confirmLeave && (
          <Modal
            title={`Leave ${me?.tenant?.name}?`}
            sub="You will need a new invite to get back in. Nothing you created is deleted."
            width="max-w-[440px]"
            onClose={() => setConfirmLeave(false)}
            footer={
              <>
                <Button variant="secondary" onClick={() => setConfirmLeave(false)}>Cancel</Button>
                <Button variant="danger" disabled={leaving} onClick={leave}>
                  {leaving ? "Leaving…" : "Leave organization"}
                </Button>
              </>
            }
          />
        )}
      </Container>
    </AppShell>
  );
}

// useSearchParams() suspends during prerender, so the exported page provides the
// boundary and SettingsPageInner does the work. Without this, `next build` fails
// with "useSearchParams() should be wrapped in a suspense boundary".
export default function SettingsPage() {
  return (
    <Suspense fallback={null}>
      <SettingsPageInner />
    </Suspense>
  );
}
