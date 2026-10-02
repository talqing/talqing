"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { AgentConfig, CallDetailResponse } from "@talqing/sdk";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { ConfigDiffView } from "@/app/components/ConfigDiffView";
import { diffAgentConfigs } from "@/app/agents/detail/agentDiff";
import { Badge, Button, Disclosure, useToast } from "@/app/components/ui";

/** What this call ran, when it was not simply the agent's published version.
 *
 *  Four shapes, one panel, because they are one mechanism:
 *
 *  - **Overridden** — a stored agent with changes layered on it. Rendered as the
 *    same diff the version history uses, published version → what ran, so "the
 *    voice and the max duration were different on this call" is one screen.
 *  - **Draft** — the agent's unpublished draft, frozen as it stood when the call
 *    started. Rendered in full: there is no version to diff it against.
 *  - **Inline** — a definition that existed only in the request. Rendered
 *    read-only, with a button that turns it into a real agent: the experiment
 *    that worked is one click from something with an editor and a history.
 *  - **Team** — the roster, each member expandable into its own view.
 *
 *  Renders nothing at all on an ordinary call. The starting prompt and the
 *  attached tools above already say what ran; a panel saying "nothing was
 *  different" would be a row that is always there and never news. */
export function CallConfiguration({ call }: { call: CallDetailResponse }) {
  const plan = call.agent_plan;
  const config = call.agent_config ?? null;
  const members = call.team ?? [];
  const entry = plan?.members?.[0] ?? null;
  const [base, setBase] = useState<AgentConfig | null>(null);
  const [names, setNames] = useState<Names | null>(null);

  useEffect(() => {
    if (!plan || !entry) return;
    let live = true;
    void (async () => {
      const [resolved, lookups] = await Promise.all([
        typeof entry.agent_id === "string" && typeof entry.version === "number"
          ? api.getAgentVersion(entry.agent_id, entry.version).then((v) => v.config)
          : Promise.resolve(null),
        loadNames(),
      ]);
      if (!live) return;
      setBase(resolved);
      setNames(lookups);
    })();
    return () => {
      live = false;
    };
  }, [plan, entry]);

  if (!plan || !config) return null;

  const inline = !entry?.agent_id;
  const draft = call.session.agent_version === "draft";
  // A draft's override is the whole draft, not changes layered on a version.
  const overridden = !draft && Object.keys(entry?.override ?? {}).length > 0;
  const label =
    members.length > 1
      ? `Team · ${members.length}`
      : inline
        ? "Inline"
        : draft
          ? "Draft"
          : overridden
            ? "Overridden"
            : `Pinned · v${entry?.version}`;

  return (
    <Disclosure summary="Configuration" meta={label}>
      <div className="grid gap-4">
        <p className="text-[12px] leading-4 text-faint">
          {inline
            ? "This call ran a definition sent in the request that started it. Nothing in the workspace holds it, so there is no version history and no editor — save it as an agent to get both."
            : draft
              ? "This call ran the agent's unpublished draft as it stood when the call started. Later edits and publishes have not changed what is shown here."
              : overridden
                ? "This call ran a modified copy of a published version. The base is pinned, so republishing the agent since has not changed what is shown here."
                : `This call was pinned to version ${entry?.version} when it was placed, so it ran that version whatever has been published since.`}
        </p>

        {members.length > 1 && (
          <div className="divide-y divide-line rounded-lg border border-line-2">
            {members.map((member, index) => (
              <div key={member.name} className="flex items-center gap-3 px-3 py-2.5">
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
                  {member.name}
                </span>
                {index === 0 && <Badge variant="info">Answered</Badge>}
                {member.agent_id ? (
                  <Link
                    href={`/agents/detail?id=${member.agent_id}`}
                    className="flex-none text-[12px] text-muted underline-offset-2 hover:text-ink hover:underline"
                  >
                    {member.version === "draft" ? "draft" : `v${member.version}`} ↗
                  </Link>
                ) : (
                  <span className="flex-none text-[12px] text-faint">inline</span>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Above the listing, not under it: an inline config has no base to diff
            against, so every field renders and the action would sit seventy rows
            down from the sentence that explains why it is there. */}
        {inline && <SaveAsAgent config={config} />}

        {names && (overridden || inline || draft || members.length > 1) && (
          <ConfigDiffView
            diff={diffAgentConfigs(
              base ?? ({ name: "" } as AgentConfig),
              config,
              names,
            )}
            emptyLabel="Nothing was different about this call."
            initial={base === null}
          />
        )}
      </div>
    </Disclosure>
  );
}

type Names = {
  tool: (id: string) => string | null;
  integration: (id: string) => string;
  faq: (id: string) => string;
};

/** Names for the ids a diff would otherwise print raw. Fetched only on a call
 *  that carried a plan, which is a small fraction of them. */
async function loadNames(): Promise<Names> {
  const [tools, integrations, faqs] = await Promise.all([
    api.listTools().then((p) => p.items),
    api.listIntegrations().then((p) => p.items),
    api.listFaqs().then((p) => p.items),
  ]);
  const toolNames = new Map(tools.map((t) => [t.id, t.name]));
  const integrationNames = new Map(integrations.map((i) => [i.id, i.display_name]));
  const faqNames = new Map(faqs.map((f) => [f.id, f.name]));
  return {
    tool: (id) => toolNames.get(id) ?? null,
    integration: (id) => integrationNames.get(id) ?? id,
    faq: (id) => faqNames.get(id) ?? id,
  };
}

/** Promote a transient definition into a real agent.
 *
 *  `POST /v1/agents` materializes any tool this config defined inline, so one
 *  click promotes the whole thing — the agent and its tools — rather than
 *  leaving an agent that references definitions nothing holds. */
function SaveAsAgent({ config }: { config: AgentConfig }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const toast = useToast();

  async function save() {
    setBusy(true);
    setErr("");
    try {
      const agent = await api.createAgentFromConfig(config);
      toast({
        kind: "ok",
        msg: `Saved as “${agent.config.name}”. It is a draft — publish it to take calls.`,
      });
      window.location.href = `/agents/detail?id=${agent.id}`;
    } catch (error) {
      setErr(apiErrorMessage(error, "Could not save this as an agent."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button variant="secondary" size="sm" onClick={save} disabled={busy}>
        {busy ? "Saving…" : "Save as agent"}
      </Button>
      <span className="text-[12px] leading-4 text-faint">
        Creates a draft agent from this definition, tools included. Nothing about this call changes.
      </span>
      {err && <span className="text-[12.5px] text-danger">{err}</span>}
    </div>
  );
}
