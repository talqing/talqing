"use client";

import { useMemo } from "react";
import type { ToolResponse, ToolVersionDetailResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { api } from "@/lib/api";
import { VersionHistoryModal as HistoryModal } from "@/app/components/VersionHistoryModal";
import type { ToolDefinitionSnapshot } from "./definition";
import { ToolDiffView } from "./ToolDiffView";
import { diffToolDefinitions, versionSnapshot } from "./toolDiff";

/** A version is fetched as a `ToolVersionDetailResponse` but diffed as a
    snapshot, and the raw response is what "Copy JSON" should hand over — so the
    modal carries both. */
type Side = { snapshot: ToolDefinitionSnapshot; detail: ToolVersionDetailResponse | null };

export function VersionHistoryModal({
  tool,
  draft,
  agentNameById,
  onClose,
  onRolledBack,
}: {
  tool: ToolResponse;
  /** The draft as it stands in the editor, unsaved edits included. */
  draft: ToolDefinitionSnapshot;
  agentNameById: Record<string, string>;
  onClose: () => void;
  onRolledBack: () => Promise<void>;
}): JSX.Element {
  const versions = useMemo(
    () =>
      (tool.versions ?? []).map((v) => ({
        version: v.version,
        published_at: v.published_at,
        meta: (
          <span className={cn("truncate text-[11.5px] leading-4", v.changelog ? "text-ink-soft" : "text-placeholder")}>
            {v.changelog || "no changelog"}
          </span>
        ),
      })),
    [tool.versions],
  );

  return (
    <HistoryModal<Side>
      title={`Version history · ${tool.name}`}
      subject="tool"
      versions={versions}
      live={tool.published_version ?? null}
      draft={{ snapshot: draft, detail: null }}
      draftMeta={
        <span className="text-[11.5px] leading-4 text-muted">
          {tool.published_version ? "what you are editing" : "never published"}
        </span>
      }
      loadVersion={async (version) => {
        const detail = await api.getToolVersion(tool.id, version);
        return { snapshot: versionSnapshot(detail), detail };
      }}
      renderDiff={(before, after, emptyLabel) => (
        <ToolDiffView
          diff={diffToolDefinitions(before.snapshot, after.snapshot)}
          agentNameById={agentNameById}
          emptyLabel={emptyLabel}
        />
      )}
      copyJson={(side) => JSON.stringify(side.detail, null, 2)}
      confirmSub={(version) =>
        `Your current draft is replaced by v${version}, and v${version} becomes live immediately. ` +
        "No new version is created. Agents keep the tool version they were published with until you republish them."
      }
      rollback={async (version) => {
        await api.rollbackToolVersion(tool.id, version);
        await onRolledBack();
      }}
      onClose={onClose}
    />
  );
}
