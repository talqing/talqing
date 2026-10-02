"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "../components/AppShell";
import {
  Badge,
  Button,
  EmptyState,
  Field,
  Input,
  Menu,
  Modal,
  Select,
  Skeleton,
} from "../components/ui";
import type { MenuItem } from "../components/ui";
import { cn } from "@/lib/cn";
import { useActiveRegion } from "@/lib/regions";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { saveTrigger } from "@/lib/triggers";
import type {
  CreateIntegrationRequest,
  PatchIntegrationRequest,
  AgentResponse,
  IntegrationCatalogItem,
  IntegrationResponse,
  IntegrationTriggerCapability,
  IntegrationTriggerResponse,
  JsonObject,
  JsonValue,
} from "@talqing/sdk";
import { IntegrationLogo } from "../components/IntegrationLogo";
import { ToolApprovalModal } from "./ToolApprovalModal";

const CARD = "rounded-xl border border-line bg-white shadow-[0_1px_2px_rgba(15,15,16,0.025)]";
const INTEGRATION_TABLE_GRID =
  "sm:grid-cols-[minmax(0,1.3fr)_140px_110px_minmax(0,1fr)_140px_80px_52px]";

type HeaderDraft = {
  id: string;
  key: string;
  value: string;
};

type ModalStep = "catalog" | "manual";

const newHeaderId = (): string =>
  (typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `hdr_${Math.random().toString(36).slice(2)}`);

function commaList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * Who this connection is signed in as — and "" when the provider has nobody to
 * report.
 *
 * Only the identity, with no "Hosted MCP ·" or "Telegram bot ·" in front of it:
 * the row already has a Provider column, and a cell that spends its width
 * restating its neighbour is a cell that says nothing. An API-key provider has
 * no account at all (`provider_account_info` stays `{}` — nothing fetches one),
 * so it lands here as "" and the table draws a dash, the same way it marks a
 * provider that has no tools to namespace.
 *
 * A custom server is the exception that proves the rule: it has no account, but
 * its URL is the only thing that tells two of them apart, so the URL is its
 * identity.
 */
function integrationAccount(integration: IntegrationResponse): string {
  if (integration.provider === "custom_mcp") {
    return typeof integration.mcp_config?.url === "string" ? integration.mcp_config.url : "";
  }
  if (integration.provider === "telegram") {
    const username =
      typeof integration.provider_account_info?.bot_username === "string"
        ? integration.provider_account_info.bot_username
        : "";
    const botId =
      typeof integration.provider_account_info?.bot_id === "string"
        ? integration.provider_account_info.bot_id
        : "";
    return username ? `@${username.replace(/^@/, "")}` : botId;
  }
  return (
    (typeof integration.provider_account_info?.account_email === "string" &&
      integration.provider_account_info.account_email) ||
    (typeof integration.provider_account_info?.account_username === "string" &&
      integration.provider_account_info.account_username) ||
    (typeof integration.provider_account_info?.account_name === "string" &&
      integration.provider_account_info.account_name) ||
    ""
  );
}

/** Trigger catalog from the backend registry (not hardcoded per provider). */
function providerTriggers(integration: IntegrationResponse): IntegrationTriggerCapability[] {
  return integration.capabilities.triggers;
}

function triggerStateKey(integrationId: string, triggerType: string): string {
  return `${integrationId}:${triggerType}`;
}

function assignSetupValue(
  body: CreateIntegrationRequest | PatchIntegrationRequest,
  target: IntegrationCatalogItem["setup_fields"][number]["target"],
  key: string,
  value: JsonValue,
) {
  if (target === "credentials_ref") {
    if (typeof value !== "string") throw new Error("credentials reference must be text");
    body.credentials_ref = value;
    return;
  }
  const current = (body[target] ?? {}) as Record<string, unknown>;
  current[key] = value;
  body[target] = current;
}

export default function IntegrationsPage() {
  const region = useActiveRegion();
  const router = useRouter();
  const [integrations, setIntegrations] = useState<IntegrationResponse[]>([]);
  const [catalog, setCatalog] = useState<IntegrationCatalogItem[]>([]);
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [triggers, setTriggers] = useState<Record<string, IntegrationTriggerResponse[]>>({});
  const [triggerAgentDrafts, setTriggerAgentDrafts] = useState<Record<string, string>>({});
  const [triggerSaving, setTriggerSaving] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [step, setStep] = useState<ModalStep>("catalog");
  const [manualItem, setManualItem] = useState<IntegrationCatalogItem | null>(null);
  const [category, setCategory] = useState("All integrations");
  const [search, setSearch] = useState("");
  const [name, setName] = useState("");
  const [manualValues, setManualValues] = useState<Record<string, string>>({});
  const [manualHeaders, setManualHeaders] = useState<Record<string, HeaderDraft[]>>({});
  const [creating, setCreating] = useState(false);
  const [editingIntegration, setEditingIntegration] = useState<IntegrationResponse | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<IntegrationResponse | null>(null);
  const [deleting, setDeleting] = useState(false);
  // The integration whose MCP tools are being approved (its own modal).
  const [toolsTarget, setToolsTarget] = useState<IntegrationResponse | null>(null);

  async function load() {
    try {
      const [integrationsPage, catalogPage, nextAgents] = await Promise.all([
        api.listIntegrations(),
        api.integrationCatalog(),
        api.listAllAgents(),
      ]);
      // A WhatsApp number is a channel with its own page, not something lent to agents.
      const nextIntegrations = integrationsPage.items.filter((item) => item.provider !== "whatsapp");
      const nextCatalog = catalogPage.items;
      setIntegrations(nextIntegrations);
      setCatalog(nextCatalog);
      setAgents(nextAgents);
      const triggerEntries = await Promise.all(
        nextIntegrations
          .filter((integration) => providerTriggers(integration).length > 0)
          .map(async (integration) => {
            const page = await api.listIntegrationTriggers(integration.id);
            return [integration.id, page.items] as const;
          }),
      );
      const nextTriggers = Object.fromEntries(triggerEntries);
      setTriggers(nextTriggers);
      setTriggerAgentDrafts((current) => {
        const next = { ...current };
        for (const [integrationId, integrationTriggers] of triggerEntries) {
          const integration = nextIntegrations.find((item) => item.id === integrationId);
          for (const meta of integration ? providerTriggers(integration) : []) {
            const trigger = integrationTriggers.find((item) => item.trigger_type === meta.trigger_type);
            if (trigger?.agent_id) next[triggerStateKey(integrationId, meta.trigger_type)] = trigger.agent_id;
          }
        }
        return next;
      });
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  useEffect(() => {
    if (loading) return;
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const integrationId = params.get("integration_id");
    const oauthError = params.get("oauth_error");
    if (connected) {
      const connectedIntegration = integrations.find((integration) => integration.id === integrationId);
      const catalogLabel = catalog.find((item) => item.provider === connected)?.name;
      setNotice(
        `${connectedIntegration?.provider_label || catalogLabel || "Integration"} connected. Attach it to an agent so it can use the tools.`,
      );
      // A fresh connection exposes every tool until someone looks — so look now.
      if (connectedIntegration?.capabilities.attachable_as_mcp) setToolsTarget(connectedIntegration);
    }
    if (oauthError) setErr(oauthError);
    if (connected || oauthError) {
      const url = new URL(window.location.href);
      url.search = "";
      window.history.replaceState(null, "", url.toString());
    }
  }, [catalog, integrations, loading]);

  function openCatalog() {
    setErr("");
    setEditingIntegration(null);
    setModalOpen(true);
  }

  function closeModal(force = false) {
    if (creating && !force) return;
    setErr("");
    setModalOpen(false);
    setStep("catalog");
    setManualItem(null);
    setEditingIntegration(null);
    setCategory("All integrations");
    setSearch("");
    setName("");
    setManualValues({});
    setManualHeaders({});
  }

  function headersObject(headers: HeaderDraft[]): Record<string, string> {
    return Object.fromEntries(
      headers
        .map((header) => [header.key.trim(), header.value.trim()])
        .filter(([key]) => key),
    );
  }

  function selectCatalogItem(item: IntegrationCatalogItem) {
    setErr("");
    if (!item.enabled) {
      setErr(`${item.name} is not available on this Talqing backend yet.`);
      return;
    }
    if (item.provider === "whatsapp") {
      router.push("/whatsapp/numbers?connect=1");
      return;
    }
    // Catalog auth_type is the source of truth — no per-provider URL map.
    if (item.auth_type === "oauth") {
      window.location.href = api.oauthStartUrl(item.provider);
      return;
    }
    setManualItem(item);
    setEditingIntegration(null);
    setStep("manual");
    setName(item.name);
    setManualValues({});
    setManualHeaders({});
  }

  function openConfigure(integration: IntegrationResponse) {
    const item = catalog.find((candidate) => candidate.provider === integration.provider);
    if (!item) {
      setErr(`Setup definition for ${integration.provider_label} is unavailable.`);
      return;
    }
    const values: Record<string, string> = {};
    const headers: Record<string, HeaderDraft[]> = {};
    for (const field of item.setup_fields) {
      if (field.target === "credentials_ref") {
        if (integration.credentials_ref) values[field.key] = integration.credentials_ref;
        continue;
      }
      const raw = integration[field.target]?.[field.key];
      if (field.type === "headers" && raw && typeof raw === "object" && !Array.isArray(raw)) {
        headers[field.key] = Object.entries(raw as Record<string, string>).map(([key, value]) => ({
          id: newHeaderId(),
          key,
          value: String(value ?? ""),
        }));
        continue;
      }
      if (Array.isArray(raw)) values[field.key] = raw.join(", ");
      else if (typeof raw === "string" || typeof raw === "number") values[field.key] = String(raw);
    }
    setErr("");
    setEditingIntegration(integration);
    setManualItem(item);
    setStep("manual");
    setName(integration.display_name);
    setManualValues(values);
    setManualHeaders(headers);
    setModalOpen(true);
  }

  async function createManualIntegration(e: React.FormEvent) {
    e.preventDefault();
    if (!manualItem) return;
    setCreating(true);
    setErr("");
    try {
      const body: CreateIntegrationRequest | PatchIntegrationRequest = editingIntegration
        ? { display_name: name.trim() }
        : {
            display_name: name.trim(),
            provider: manualItem.provider,
            provider_account_info: {},
            mcp_config: {},
            webhook_config: {},
            metadata: {},
          };
      for (const field of manualItem.setup_fields) {
        if (field.type === "headers") {
          const value = headersObject(manualHeaders[field.key] || []);
          if (Object.keys(value).length) {
            assignSetupValue(body, field.target, field.key, value);
          }
        } else if (field.type === "string_list") {
          const value = commaList(manualValues[field.key] || "");
          if (value.length) {
            assignSetupValue(body, field.target, field.key, value);
          }
        } else {
          const value = (manualValues[field.key] || "").trim();
          if (value) {
            assignSetupValue(body, field.target, field.key, value);
          }
        }
      }
      if (editingIntegration) {
        await api.patchIntegration(editingIntegration.id, body as PatchIntegrationRequest);
        setNotice(`${name.trim()} configuration updated.`);
      } else {
        const created = await api.createIntegration(body as CreateIntegrationRequest);
        setNotice(
          created.provider === "telegram"
            ? "Telegram bot connected. Choose a published text agent and enable inbound messages — Talqing registers the webhook for you."
            : `${created.provider_label} added.`,
        );
        // Approve tools straight away rather than leaving the whole server open.
        if (created.capabilities.attachable_as_mcp) setToolsTarget(created);
      }
      closeModal(true);
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setCreating(false);
    }
  }

  async function setStatus(integration: IntegrationResponse, status: "active" | "disabled") {
    setErr("");
    try {
      await api.patchIntegration(integration.id, { status });
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    }
  }

  async function setProviderTrigger(
    integration: IntegrationResponse,
    meta: IntegrationTriggerCapability,
    enabled: boolean,
  ) {
    const stateKey = triggerStateKey(integration.id, meta.trigger_type);
    const selectedAgentId = triggerAgentDrafts[stateKey] || "";
    if (enabled && !selectedAgentId) {
      setErr(`Select a published ${meta.agent_channel} agent before enabling this trigger.`);
      return;
    }
    setErr("");
    setTriggerSaving((current) => ({ ...current, [stateKey]: true }));
    try {
      const existing = (triggers[integration.id] || []).find((item) => item.trigger_type === meta.trigger_type);
      const updated = await saveTrigger(integration.id, existing, {
        triggerType: meta.trigger_type,
        agentId: enabled ? selectedAgentId : existing?.agent_id || selectedAgentId || null,
        enabled,
      });
      setTriggers((current) => ({
        ...current,
        [integration.id]: [
          updated,
          ...(current[integration.id] || []).filter((item) => item.id !== updated.id),
        ],
      }));
      if (updated.agent_id) {
        setTriggerAgentDrafts((current) => ({ ...current, [stateKey]: updated.agent_id || "" }));
      }
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setTriggerSaving((current) => ({ ...current, [stateKey]: false }));
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setErr("");
    try {
      await api.deleteIntegration(deleteTarget.id);
      setDeleteTarget(null);
      await load();
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }

  const categories = useMemo(() => {
    const all = ["All integrations", ...Array.from(new Set(catalog.map((item) => item.category)))];
    return all.length ? all : ["All integrations"];
  }, [catalog]);
  const visibleCatalog = catalog.filter((item) => {
    const q = search.trim().toLowerCase();
    const matchesCategory = category === "All integrations" || item.category === category;
    const matchesSearch = !q || [item.name, item.description, item.category].some((value) => value.toLowerCase().includes(q));
    return matchesCategory && matchesSearch;
  });
  const manualFields = manualItem?.setup_fields || [];
  const canSubmitManual = Boolean(
    manualItem &&
    name.trim() &&
    manualFields.every((field) => {
      if (!field.required) return true;
      if (field.type === "headers") return Object.keys(headersObject(manualHeaders[field.key] || [])).length > 0;
      return Boolean((manualValues[field.key] || "").trim());
    }),
  );
  const activeCount = integrations.filter((integration) => integration.status === "active").length;
  const publishedTextAgents = agents.filter((agent) => agent.published_version && agent.config?.channel === "text");
  const publishedVoiceAgents = agents.filter((agent) => agent.published_version && agent.config?.channel === "voice");

  return (
    <AppShell>
      <div className="min-h-screen bg-white px-5 pb-20 pt-0 text-ink sm:px-6 lg:px-8">
        <div className="w-full">
          <header className="mb-6 flex flex-col gap-4 border-b border-line bg-white py-6 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <h1 className="text-[26px] font-semibold leading-8 text-ink">Integrations</h1>
              <p className="mt-2 max-w-[72ch] text-[14px] leading-5 text-muted">
                Connect external services and MCP servers for agents to use.
              </p>
            </div>
            <Button onClick={openCatalog} className="min-h-[38px] self-start">+ Add integration</Button>
          </header>

          {/* Counts of nothing would only repeat the empty state below. */}
          {(loading || integrations.length > 0) && (
            <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-line bg-white p-4 transition-colors hover:border-line-strong">
                <div className="text-[13px] font-medium leading-5 text-muted">Integrations</div>
                <div className="mt-2 text-[22px] font-semibold leading-7 text-ink tabular-nums">{loading ? <Skeleton className="h-7 w-12" /> : integrations.length}</div>
                <div className="mt-1 text-[13px] leading-5 text-faint">Configured connections</div>
              </div>
              <div className="rounded-xl border border-line bg-white p-4 transition-colors hover:border-line-strong">
                <div className="text-[13px] font-medium leading-5 text-muted">Ready</div>
                <div className="mt-2 text-[22px] font-semibold leading-7 text-ink tabular-nums">{loading ? <Skeleton className="h-7 w-12" /> : `${activeCount}/${integrations.length}`}</div>
                <div className="mt-1 text-[13px] leading-5 text-faint">Operational connections</div>
              </div>
            </div>
          )}

          {notice && (
            <div className="mb-5 rounded-lg border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[13.5px] text-emerald-700">
              <span>{notice} </span>
              <a href="/agents" className="font-semibold underline decoration-emerald-400 underline-offset-2 hover:text-emerald-900">
                Open agents
              </a>
            </div>
          )}
          {/* Only when nothing is covering it. A create that fails keeps the
              modal open, and this banner then sits behind the overlay where it
              cannot be read — so while the modal is up, the modal renders it. */}
          {err && !modalOpen && (
            <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
              {err}
            </div>
          )}

          {loading ? (
            <div className={cn(CARD, "overflow-hidden")}>
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="flex items-center gap-3.5 border-b border-line px-4 py-4 last:border-b-0">
                  <Skeleton className="h-9 w-9 rounded-[10px]" />
                  <div className="min-w-0 flex-1">
                    <Skeleton className="h-[15px] w-[28%]" />
                    <Skeleton className="mt-2 h-3 w-[44%]" />
                  </div>
                </div>
              ))}
            </div>
          ) : integrations.length === 0 ? (
            <EmptyState
              title={`No integrations in ${region.name}`}
              body="Connect an app or an MCP server, and your agents can use its tools."
              cta={<Button onClick={openCatalog}>+ Add integration</Button>}
            />
          ) : (
            <section className={cn(CARD, "min-w-0 overflow-hidden")}>
              <div className="flex flex-col gap-1 border-b border-line px-4 py-4">
                <h2 className="text-[14px] font-semibold leading-5 text-ink">Integration library</h2>
                <p className="text-[13px] leading-5 text-faint">Manage service connections and account state.</p>
              </div>
              <div className={cn(
                "hidden gap-3 border-b border-line bg-white px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
                INTEGRATION_TABLE_GRID,
                )}>
                <span>Integration</span>
                <span>Provider</span>
                <span>Status</span>
                <span>Account</span>
                <span>Tool namespace</span>
                <span>Tools</span>
                <span className="sr-only">Actions</span>
              </div>
              {integrations.map((integration) => {
                const active = integration.status === "active";
                const needsReconnect = integration.status === "needs_reconnect";
                const reconnectUrl =
                  integration.auth_type === "oauth"
                    ? api.oauthStartUrl(integration.provider, integration.id)
                    : null;
                const triggerMetas = providerTriggers(integration);
                const hasSetupFields = Boolean(
                  catalog.find((item) => item.provider === integration.provider)?.setup_fields?.length,
                );
                const goReconnect = reconnectUrl
                  ? () => {
                      window.location.href = reconnectUrl;
                    }
                  : null;
                const canManageTools = integration.capabilities.attachable_as_mcp;
                const account = integrationAccount(integration);
                // Null allowed_tools means nothing has been narrowed yet, which
                // exposes the whole server — "All", not zero.
                const toolsLabel = integration.allowed_tools
                  ? String(integration.allowed_tools.length)
                  : "All";
                /* Set exactly when the tool list is readable — the modal reads
                   it off the live server, so a disabled or unreachable
                   connection has nothing to open. This used to be the row's
                   primary button; the whole row carries it now. */
                const openTools = active && canManageTools ? () => setToolsTarget(integration) : null;
                /* What is left for a button is whatever the row is still asking
                   for, at most one of it, with the rest under "⋯": a broken
                   connection wants reconnecting, a disabled one wants enabling,
                   and a provider that lends no tools may want configuring. A
                   working MCP row asks for nothing and gets no button at all. */
                const primary =
                  needsReconnect && goReconnect
                    ? { kind: "reconnect", label: "Reconnect", onSelect: goReconnect }
                    : !active
                      ? {
                          kind: "enable",
                          label: "Enable",
                          onSelect: () => setStatus(integration, "active"),
                        }
                      : !canManageTools && hasSetupFields
                        ? {
                            kind: "configure",
                            label: "Configure",
                            onSelect: () => openConfigure(integration),
                          }
                        : null;
                const menuItems: MenuItem[] = [
                  ...(canManageTools
                    ? [
                        {
                          label: "Manage tools",
                          onSelect: () => setToolsTarget(integration),
                          // The tool list is read from the live MCP server.
                          disabled: !active,
                        },
                      ]
                    : []),
                  ...(hasSetupFields && primary?.kind !== "configure"
                    ? [{ label: "Configure", onSelect: () => openConfigure(integration) }]
                    : []),
                  ...(goReconnect && primary?.kind !== "reconnect"
                    ? [{ label: "Reconnect", onSelect: goReconnect }]
                    : []),
                  // Enable is only ever the primary, so only Disable belongs here.
                  ...(active
                    ? [{ label: "Disable", onSelect: () => setStatus(integration, "disabled") }]
                    : []),
                  { label: "Delete integration", onSelect: () => setDeleteTarget(integration), danger: true },
                ];
                return (
                  <div
                    key={integration.id}
                    /* Clicking the row opens its tools. Deliberately NOT
                       role="button": that collapses the row into one a11y leaf
                       whose label replaces everything in it, and the provider,
                       status, account and namespace stop being readable. So this
                       is a mouse convenience, and the keyboard gets the real
                       control on the name below (plus "Manage tools" in "⋯"). */
                    onClick={openTools ?? undefined}
                    className={cn(
                      "grid min-h-[68px] gap-3 border-b border-line px-4 py-3.5 transition-colors last:border-b-0 hover:bg-canvas sm:items-center",
                      openTools && "cursor-pointer",
                      INTEGRATION_TABLE_GRID,
                    )}
                  >
                    <div className="flex min-w-0 items-center gap-3">
                      <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft">
                        <IntegrationLogo kind={integration.provider} logoUrl={integration.logo_url} size={20} />
                      </span>
                      <div className="min-w-0">
                        {openTools ? (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              openTools();
                            }}
                            title={integration.display_name}
                            className="block max-w-full cursor-pointer truncate rounded text-left text-[14px] font-semibold leading-5 text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
                          >
                            {integration.display_name}
                          </button>
                        ) : (
                          <div
                            className="truncate text-[14px] font-semibold leading-5 text-ink"
                            title={integration.display_name}
                          >
                            {integration.display_name}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center text-[13px] font-medium text-ink-soft">{integration.provider_label}</div>
                    <div className="flex items-center">
                      <Badge variant={active ? "live" : "warn"} dot={active}>
                        {needsReconnect ? "reconnect" : integration.status}
                      </Badge>
                    </div>
                    <div className="flex min-w-0 items-center text-[13px] leading-5 text-ink-soft">
                      {account ? (
                        <span className="truncate" title={account}>
                          {account}
                        </span>
                      ) : (
                        <span className="text-placeholder">—</span>
                      )}
                    </div>
                    {/* Three states, and they are not two: a provider that lends
                        no tools has no namespace to have (—), while an MCP server
                        with a blank one has made a choice ("No prefix", the same
                        words as the field's placeholder). */}
                    <div className="flex min-w-0 items-center text-[13px] leading-5">
                      {!canManageTools ? (
                        <span className="text-placeholder">—</span>
                      ) : integration.tools_namespace ? (
                        <span className="truncate font-mono text-[12.5px] leading-5 text-ink-soft">
                          {integration.tools_namespace}
                        </span>
                      ) : (
                        <span className="text-faint">No prefix</span>
                      )}
                    </div>
                    {/* How much of the server is switched on. A count, not an
                        action — the row opens the picker — so it reads as data
                        under its own header rather than as text loose beside
                        the menu. */}
                    <div className="flex min-w-0 items-center text-[13px] leading-5 text-ink-soft tabular-nums">
                      {canManageTools ? toolsLabel : <span className="text-placeholder">—</span>}
                    </div>
                    {/* Everything here acts on its own, so none of it may also
                        trigger the row underneath. */}
                    <div
                      className="flex items-center gap-1.5 sm:justify-end"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {primary && (
                        <Button
                          variant={primary.kind === "reconnect" ? "primary" : "secondary"}
                          size="sm"
                          onClick={primary.onSelect}
                        >
                          {primary.label}
                        </Button>
                      )}
                      <Menu
                        label={`More actions for ${integration.display_name}`}
                        items={menuItems}
                      />
                    </div>
                    {triggerMetas.map((triggerMeta) => {
                      const stateKey = triggerStateKey(integration.id, triggerMeta.trigger_type);
                      const trigger = (triggers[integration.id] || []).find((item) => item.trigger_type === triggerMeta.trigger_type);
                      const selectedAgentId = triggerAgentDrafts[stateKey] || trigger?.agent_id || "";
                      const triggerActive = Boolean(trigger?.enabled && trigger.status === "active");
                      const triggerOperational = active && triggerActive;
                      const triggerBusy = Boolean(triggerSaving[stateKey]);
                      const availableAgents = triggerMeta.agent_channel === "voice" ? publishedVoiceAgents : publishedTextAgents;
                      return (
                        <div
                          key={triggerMeta.trigger_type}
                          className="sm:col-span-7"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <div className="mt-1 grid gap-3 rounded-lg border border-line bg-canvas px-3.5 py-3 sm:grid-cols-[minmax(0,1fr)_180px] sm:items-end">
                            <div className="min-w-0">
                              <div className="mb-1.5 flex flex-wrap items-center gap-2">
                                <span className="text-[13px] font-semibold leading-5 text-ink">{triggerMeta.title}</span>
                                {trigger && (
                                  <Badge variant={triggerOperational ? "live" : "warn"} dot={triggerOperational}>
                                    {triggerActive && !active ? "integration disabled" : trigger.status}
                                  </Badge>
                                )}
                              </div>
                              <div className="grid gap-2 sm:grid-cols-[minmax(0,280px)_minmax(0,1fr)] sm:items-center">
                                <Select
                                  value={selectedAgentId}
                                  onChange={(e) => setTriggerAgentDrafts((items) => ({ ...items, [stateKey]: e.target.value }))}
                                  disabled={triggerBusy || availableAgents.length === 0}
                                >
                                  <option value="">Select a published {triggerMeta.agent_channel} agent</option>
                                  {availableAgents.map((agent) => (
                                    <option key={agent.id} value={agent.id}>{agent.config.name || agent.id}</option>
                                  ))}
                                </Select>
                                <p className="text-[12.5px] leading-5 text-muted">
                                  {triggerMeta.description}
                                </p>
                              </div>
                            </div>
                            <div className="flex flex-wrap justify-start gap-2 sm:justify-end">
                              {triggerActive ? (
                                <Button
                                  variant="secondary"
                                  size="sm"
                                  onClick={() => setProviderTrigger(integration, triggerMeta, false)}
                                  disabled={triggerBusy}
                                >
                                  Disable trigger
                                </Button>
                              ) : (
                                <Button
                                  variant="primary"
                                  size="sm"
                                  onClick={() => setProviderTrigger(integration, triggerMeta, true)}
                                  disabled={triggerBusy || !active || !selectedAgentId}
                                >
                                  Enable trigger
                                </Button>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </section>
          )}
        </div>
      </div>

      {modalOpen && (
        <Modal
          title={editingIntegration ? `Configure ${editingIntegration.provider_label}` : "Add integration"}
          onClose={() => closeModal()}
          width="max-w-[980px]"
          footer={step === "manual" ? (
            <>
              <Button
                variant="secondary"
                onClick={() => {
                  if (editingIntegration) closeModal();
                  else {
                    setManualItem(null);
                    setStep("catalog");
                  }
                }}
                disabled={creating}
              >
                {editingIntegration ? "Cancel" : "Back"}
              </Button>
              <Button
                type="submit"
                form="integration-form"
                disabled={creating || !canSubmitManual}
              >
                {editingIntegration ? "Save changes" : "Add integration"}
              </Button>
            </>
          ) : (
            <Button variant="secondary" onClick={() => closeModal()}>
              Cancel
            </Button>
          )}
        >
          {step === "catalog" ? (
            <div className="-mx-6 -mt-4 grid min-h-[520px] grid-cols-1 border-t border-line md:grid-cols-[210px_minmax(0,1fr)]">
              <aside className="border-b border-line bg-white p-3 md:border-b-0 md:border-r">
                {categories.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setCategory(item)}
                    className={cn(
                      "mb-1 block w-full rounded-lg px-3 py-2 text-left text-[13.5px] font-medium leading-5",
                      item === category ? "bg-subtle text-ink" : "text-muted hover:bg-subtle hover:text-ink",
                    )}
                  >
                    {item}
                  </button>
                ))}
              </aside>
              <div className="min-w-0 p-5">
                <div className="mb-5 flex flex-col gap-3 md:flex-row md:items-center">
                  <h2 className="min-w-0 flex-1 text-[15px] font-semibold leading-5 text-ink">{category}</h2>
                  <div className="relative w-full md:w-[280px]">
                    <svg className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-placeholder" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                      <circle cx="9" cy="9" r="6" />
                      <path d="m14 14 3 3" />
                    </svg>
                    <Input
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder="Search integrations..."
                      className="pl-9"
                    />
                  </div>
                </div>
                {visibleCatalog.length ? (
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {visibleCatalog.map((item) => (
                      <button
                        key={item.provider}
                        type="button"
                        onClick={() => selectCatalogItem(item)}
                        className="flex items-center gap-3.5 rounded-xl border border-line-2 bg-white p-4 text-left transition-colors hover:border-line-strong hover:bg-canvas focus:outline-none focus:ring-2 focus:ring-ink/10"
                      >
                        <span className="grid h-12 w-12 flex-none place-items-center rounded-[10px] border border-line-2 bg-white text-ink">
                          <IntegrationLogo kind={item.provider} logoUrl={item.logo_url} size={30} />
                        </span>
                        <span className="flex min-w-0 items-center gap-2 text-[14px] font-semibold leading-5 text-ink">
                          {item.name}
                          {!item.enabled && <Badge variant="warn">setup</Badge>}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-xl border border-dashed border-line-strong bg-canvas px-4 py-10 text-center text-[13px] leading-5 text-muted">
                    No integrations match that search.
                  </div>
                )}
              </div>
            </div>
          ) : manualItem ? (
            <form id="integration-form" onSubmit={createManualIntegration} className="grid gap-4 pb-2">
              {err && (
                <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                  {err}
                </div>
              )}
              <div className="rounded-xl border border-line-2 bg-canvas p-4">
                <div className="flex items-start gap-3">
                  <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-white text-ink">
                    <IntegrationLogo kind={manualItem.provider} logoUrl={manualItem.logo_url} size={20} />
                  </span>
                  <div className="min-w-0">
                    <div className="text-[14px] font-semibold leading-5 text-ink">{manualItem.name}</div>
                    <div className="mt-1 text-[13px] leading-5 text-muted">
                      {manualItem.description}
                    </div>
                  </div>
                </div>
              </div>
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={manualItem.name} />
              </Field>
              {manualFields.map((field) => (
                <Field
                  key={field.key}
                  label={field.label}
                  hint={field.hint}
                >
                  {field.type === "headers" ? (
                    <div className="rounded-xl border border-line-2 bg-white p-3">
                      <div className="flex flex-col gap-2">
                        {(manualHeaders[field.key] || []).length === 0 ? (
                          <div className="rounded-lg border border-dashed border-line-strong bg-canvas px-3 py-3 text-[13px] leading-5 text-muted">
                            No headers.
                          </div>
                        ) : (
                          (manualHeaders[field.key] || []).map((header) => (
                            <div key={header.id} className="grid gap-2 md:grid-cols-[minmax(0,220px)_minmax(0,1fr)_auto]">
                              <Input
                                value={header.key}
                                onChange={(e) => setManualHeaders((items) => ({
                                  ...items,
                                  [field.key]: (items[field.key] || []).map((item) => item.id === header.id ? { ...item, key: e.target.value } : item),
                                }))}
                                placeholder="Authorization"
                                className="font-mono"
                              />
                              <Input
                                value={header.value}
                                onChange={(e) => setManualHeaders((items) => ({
                                  ...items,
                                  [field.key]: (items[field.key] || []).map((item) => item.id === header.id ? { ...item, value: e.target.value } : item),
                                }))}
                                placeholder="Bearer {{secrets.MCP_TOKEN}}"
                                className="font-mono"
                              />
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                onClick={() => setManualHeaders((items) => ({
                                  ...items,
                                  [field.key]: (items[field.key] || []).filter((item) => item.id !== header.id),
                                }))}
                                className="text-faint hover:text-danger"
                              >
                                Delete
                              </Button>
                            </div>
                          ))
                        )}
                        <div>
                          <Button
                            type="button"
                            variant="secondary"
                            size="sm"
                            onClick={() => setManualHeaders((items) => ({
                              ...items,
                              [field.key]: [...(items[field.key] || []), { id: newHeaderId(), key: "", value: "" }],
                            }))}
                          >
                            + Header
                          </Button>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <Input
                      type={field.type === "secret_ref" ? "password" : "text"}
                      value={manualValues[field.key] || ""}
                      onChange={(e) => setManualValues((items) => ({ ...items, [field.key]: e.target.value }))}
                      placeholder={
                        field.placeholder ||
                        (field.type === "secret_ref"
                          ? editingIntegration
                            ? "Leave blank to keep current, or paste a new value"
                            : "Paste the secret value"
                          : "")
                      }
                      autoComplete={field.type === "secret_ref" ? "off" : undefined}
                      className={field.type === "secret_ref" || field.type === "string_list" ? "font-mono" : undefined}
                    />
                  )}
                </Field>
              ))}
              {editingIntegration && editingIntegration.capabilities.attachable_as_mcp && (
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line-2 bg-canvas p-4">
                  <div className="min-w-0">
                    <div className="text-[13.5px] font-semibold text-ink">Approved tools</div>
                    <p className="mt-1 text-[12.5px] leading-5 text-muted">
                      {editingIntegration.allowed_tools
                        ? `${editingIntegration.allowed_tools.length} tool${editingIntegration.allowed_tools.length === 1 ? "" : "s"} from this integration can be called by attached agents.`
                        : "Every tool this integration offers can be called by attached agents."}{" "}
                      {/* The prefix belongs beside the count: it is what a prompt
                          has to say, and it is edited behind the same button. */}
                      {editingIntegration.tools_namespace ? (
                        <>
                          Each is prefixed{" "}
                          <span className="font-mono text-ink">
                            {editingIntegration.tools_namespace}_
                          </span>{" "}
                          when shown to the agent.
                        </>
                      ) : (
                        "They are shown to the agent under their own names."
                      )}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      const target = editingIntegration;
                      closeModal(true);
                      setToolsTarget(target);
                    }}
                  >
                    Choose tools
                  </Button>
                </div>
              )}
            </form>
          ) : (
            <div className="pb-6 text-[13px] leading-5 text-muted">
              Select an integration to continue.
            </div>
          )}
        </Modal>
      )}

      {toolsTarget && (
        <ToolApprovalModal
          integration={toolsTarget}
          onClose={() => setToolsTarget(null)}
          onSaved={(saved) => {
            setToolsTarget(null);
            setIntegrations((current) =>
              current.map((item) => (item.id === saved.id ? saved : item)),
            );
            setNotice(
              `${saved.display_name}: ${saved.allowed_tools?.length ?? 0} tools approved for attached agents` +
                (saved.tools_namespace ? `, prefixed ${saved.tools_namespace}_.` : "."),
            );
          }}
        />
      )}

      {deleteTarget && (
        <Modal
          title="Delete integration?"
          sub="This permanently deletes the integration. Published agent versions keep their frozen snapshot until those agents are republished."
          onClose={() => !deleting && setDeleteTarget(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDelete} disabled={deleting}>
                Delete integration
              </Button>
            </>
          }
        >
          <div className="pb-6">
            <div className="rounded-lg border border-line bg-canvas px-3.5 py-3">
              <div className="text-[13px] font-semibold leading-5 text-ink">{deleteTarget.display_name}</div>
              {/* Standalone, with no Provider column beside it — so here the
                  provider IS worth saying, unlike in the table cell. */}
              <div className="mt-1 text-[13px] leading-5 text-muted">
                {deleteTarget.provider_label}
                {integrationAccount(deleteTarget) && ` · ${integrationAccount(deleteTarget)}`}
              </div>
            </div>
          </div>
        </Modal>
      )}
    </AppShell>
  );
}
