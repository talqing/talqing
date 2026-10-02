"use client";
import { useEffect, useMemo, useState } from "react";
import { BoxCheckbox, Button, Field, Input, Modal } from "../components/ui";
import { cn } from "@/lib/cn";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import type { IntegrationMcpTool, IntegrationResponse } from "@talqing/sdk";
import { IntegrationLogo } from "../components/IntegrationLogo";

/**
 * What the model is shown for one MCP tool — five lines mirrored from
 * `compiler/integrations.py::exposed_tool_name`, so the previews update as the
 * namespace is typed rather than after a round trip. Same two rules: a blank
 * namespace leaves the name alone, and a tool that already carries the prefix
 * keeps it instead of doubling it (Tavily's own tools are `tavily_search`).
 */
function exposedToolName(namespace: string, tool: string): string {
  if (!namespace || tool.startsWith(`${namespace}_`)) return tool;
  return `${namespace}_${tool}`;
}

/**
 * Choose which of an MCP server's tools attached agents may call, and the
 * prefix they are presented to the model under.
 *
 * Opens on every path that produces a connected MCP integration — manual
 * create, OAuth return, and the Tools button on a row — because the tool list
 * only exists once the server is reachable, and a server nobody has looked at
 * is exactly the one whose ninety tools land in an agent unread. It is also the
 * only home that works for both provider families: the manual setup form on the
 * page behind it is unreachable for an OAuth provider.
 */
export function ToolApprovalModal({
  integration,
  onClose,
  onSaved,
}: {
  integration: IntegrationResponse;
  onClose: () => void;
  onSaved: (saved: IntegrationResponse) => void;
}) {
  // null while the live listing is in flight — an empty array is a real answer.
  const [tools, setTools] = useState<IntegrationMcpTool[] | null>(null);
  const [approved, setApproved] = useState<string[]>([]);
  // Blank is a finished state, not an empty required field — it is the default
  // for every server the tenant brought themselves.
  const [namespace, setNamespace] = useState(integration.tools_namespace);
  const [search, setSearch] = useState("");
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const result = await api.listIntegrationMcpTools(integration.id);
        if (cancelled) return;
        setTools(result.tools);
        // Null allowed_tools means nothing has been approved yet, which today
        // exposes everything — so open with everything ticked either way, and
        // drop names the server no longer lists.
        const current = integration.allowed_tools;
        setApproved(
          result.tools
            .map((tool) => tool.name)
            .filter((name) => !current || current.includes(name)),
        );
      } catch (error) {
        if (cancelled) return;
        setErr(apiErrorMessage(error));
        setTools([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [integration.id, integration.allowed_tools]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q || !tools) return tools || [];
    return tools.filter((tool) =>
      `${tool.name} ${exposedToolName(namespace, tool.name)} ${tool.description || ""}`
        .toLowerCase()
        .includes(q),
    );
  }, [tools, search, namespace]);

  function toggle(name: string) {
    setApproved((current) =>
      current.includes(name) ? current.filter((item) => item !== name) : [...current, name],
    );
  }

  async function save() {
    setSaving(true);
    setErr("");
    try {
      // Approval is always the tool's own name; the prefix is applied after.
      const saved = await api.patchIntegration(integration.id, {
        allowed_tools: approved,
        tools_namespace: namespace,
      });
      onSaved(saved);
    } catch (error) {
      setErr(apiErrorMessage(error));
      setSaving(false);
    }
  }

  const total = tools?.length ?? 0;
  const allVisibleApproved =
    visible.length > 0 && visible.every((tool) => approved.includes(tool.name));

  return (
    <Modal
      title="Approve tools"
      sub={
        <>
          Agents with <span className="font-medium text-ink">{integration.display_name}</span>{" "}
          attached can call the tools you approve here. Every approved tool is described to the
          model on each turn, so approving only what the agent needs keeps it fast and accurate.
        </>
      }
      onClose={() => !saving && onClose()}
      width="max-w-[720px]"
      footer={
        <>
          <span className="mr-auto text-[12.5px] leading-5 text-faint">
            {approved.length === 0
              ? "Approve at least one tool, or disable the integration instead."
              : `${approved.length} of ${total} approved`}
          </span>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving || tools === null || approved.length === 0}>
            {saving ? "Saving…" : "Save tools"}
          </Button>
        </>
      }
    >
      <div className="pb-2">
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-line-2 bg-canvas px-4 py-3">
          <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-surface text-ink">
            <IntegrationLogo kind={integration.provider} logoUrl={integration.logo_url} size={20} />
          </span>
          <div className="min-w-0">
            <div className="truncate text-[14px] font-semibold leading-5 text-ink">
              {integration.display_name}
            </div>
            <div className="mt-0.5 text-[12.5px] leading-5 text-muted">
              {tools === null
                ? `Reading the tool list from ${integration.provider_label}…`
                : `${total} tool${total === 1 ? "" : "s"} from this integration`}
            </div>
          </div>
        </div>

        {err && (
          <div className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
            {err}
          </div>
        )}

        <Field
          className="mb-4"
          label="Tool namespace"
          htmlFor="tools-namespace"
          hint={
            <>
              Tools from this integration are shown to the agent as{" "}
              <span className="font-mono text-[12.5px] text-ink">namespace_tool</span>. Leave blank
              to use their own names.
            </>
          }
        >
          <Input
            id="tools-namespace"
            value={namespace}
            onChange={(e) => setNamespace(e.target.value)}
            // Not a greyed-out suggestion: a placeholder that looks like a value
            // is how a tenant ends up believing they have a prefix they do not.
            placeholder="No prefix"
            spellCheck={false}
            className="font-mono"
          />
        </Field>

        {tools === null ? (
          <div className="flex flex-col gap-2">
            {[0, 1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="h-[46px] animate-shimmer rounded-lg bg-[length:200%_100%] bg-gradient-to-r from-subtle via-line to-subtle"
              />
            ))}
          </div>
        ) : total === 0 ? (
          <div className="rounded-xl border border-dashed border-line-strong bg-canvas px-4 py-10 text-center text-[13px] leading-5 text-muted">
            This server listed no tools. Nothing to approve yet.
          </div>
        ) : (
          <>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <div className="relative min-w-0 flex-1">
                <svg
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-placeholder"
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  aria-hidden
                >
                  <circle cx="9" cy="9" r="6" />
                  <path d="m14 14 3 3" />
                </svg>
                <Input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search tools..."
                  className="pl-9"
                />
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  const names = visible.map((tool) => tool.name);
                  setApproved((current) =>
                    allVisibleApproved
                      ? current.filter((name) => !names.includes(name))
                      : Array.from(new Set([...current, ...names])),
                  );
                }}
                disabled={visible.length === 0}
              >
                {allVisibleApproved ? "Clear" : "Select"} {search.trim() ? "matches" : "all"}
              </Button>
            </div>

            <div className="max-h-[46vh] overflow-y-auto rounded-xl border border-line-2 bg-surface">
              {visible.length === 0 ? (
                <div className="px-4 py-10 text-center text-[13px] leading-5 text-muted">
                  No tools match that search.
                </div>
              ) : (
                visible.map((tool) => {
                  const checked = approved.includes(tool.name);
                  // The exposed name is what a prompt has to say, so it leads;
                  // the tool's own name stays visible because that is what the
                  // approval below it is keyed on.
                  const exposed = exposedToolName(namespace, tool.name);
                  return (
                    <div
                      key={tool.name}
                      className={cn(
                        "flex items-start gap-3 border-b border-line px-3.5 py-2.5 transition-colors last:border-b-0 hover:bg-subtle",
                        !checked && "opacity-60",
                      )}
                    >
                      <BoxCheckbox
                        checked={checked}
                        onChange={() => toggle(tool.name)}
                        ariaLabel={`Approve ${exposed}`}
                        className="mt-0.5"
                      />
                      <button
                        type="button"
                        onClick={() => toggle(tool.name)}
                        className="min-w-0 flex-1 cursor-pointer text-left"
                      >
                        <span className="block truncate font-mono text-[12.5px] font-medium text-ink">
                          {exposed}
                        </span>
                        {exposed !== tool.name && (
                          <span className="mt-0.5 block truncate font-mono text-[11.5px] leading-[1.45] text-faint">
                            {tool.name} on {integration.provider_label}
                          </span>
                        )}
                        {tool.description ? (
                          // No `block` here: it would beat line-clamp's own
                          // display and let a 30-line MCP description through.
                          <span className="mt-0.5 line-clamp-2 text-[12.5px] leading-[1.45] text-muted">
                            {tool.description}
                          </span>
                        ) : null}
                      </button>
                    </div>
                  );
                })
              )}
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
