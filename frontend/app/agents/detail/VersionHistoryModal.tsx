"use client";

import { useMemo } from "react";
import type { AgentConfig, AgentResponse, AgentVersionDetailResponse } from "@talqing/sdk";
import { api } from "@/lib/api";
import { VersionHistoryModal as HistoryModal } from "@/app/components/VersionHistoryModal";
import { ConfigDiffView } from "@/app/components/ConfigDiffView";
import { diffAgentConfigs } from "./agentDiff";

/** A version is fetched whole but diffed on its config alone, and the raw
    response is what "Copy JSON" should hand over — so the modal carries both. */
type Side = { config: AgentConfig; detail: AgentVersionDetailResponse | null };

export function VersionHistoryModal({
  agent,
  draft,
  toolNameById,
  integrationNameById,
  faqNameById,
  onClose,
  onRolledBack,
}: {
  agent: AgentResponse;
  /** The draft as it stands in the editor, unsaved edits included. */
  draft: AgentConfig;
  /** Null for a tool that has since been deleted — the diff says so. */
  toolNameById: (id: string) => string | null;
  /** Falls back to the id once the integration has been disconnected. */
  integrationNameById: (id: string) => string;
  /** Falls back to the id for an FAQ an older version names that is gone now. */
  faqNameById: (id: string) => string;
  onClose: () => void;
  onRolledBack: () => Promise<void>;
}): JSX.Element {
  const versions = useMemo(
    () =>
      (agent.versions ?? []).map((v) => ({
        version: v.version,
        published_at: v.published_at,
        meta: v.published_by ? (
          <span className="truncate text-[11.5px] leading-4 text-ink-soft">{v.published_by}</span>
        ) : undefined,
      })),
    [agent.versions],
  );

  return (
    <HistoryModal<Side>
      title={`Version history · ${agent.config.name}`}
      subject="agent"
      versions={versions}
      live={agent.published_version ?? null}
      draft={{ config: draft, detail: null }}
      draftMeta={
        <span className="text-[11.5px] leading-4 text-muted">
          {agent.published_version ? "what you are editing" : "never published"}
        </span>
      }
      loadVersion={async (version) => {
        const detail = await api.getAgentVersion(agent.id, version);
        return { config: detail.config, detail };
      }}
      renderDiff={(before, after, emptyLabel) => (
        <ConfigDiffView
          diff={diffAgentConfigs(before.config, after.config, {
            tool: toolNameById,
            integration: integrationNameById,
            faq: faqNameById,
          })}
          emptyLabel={emptyLabel}
        />
      )}
      copyJson={(side) => JSON.stringify(side.detail, null, 2)}
      confirmSub={(version) =>
        `Your current draft is replaced by v${version}, and v${version} becomes live immediately — every call ` +
        "and conversation that starts from now on runs it, and so does any agent that hands off into this one. " +
        "Calls already in progress finish on the version they started. No new version is created."
      }
      rollback={async (version) => {
        await api.rollbackAgentVersion(agent.id, version);
        await onRolledBack();
      }}
      onClose={onClose}
    />
  );
}
