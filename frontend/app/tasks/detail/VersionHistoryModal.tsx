"use client";

import { useMemo } from "react";
import type { TaskConfig, TaskResponse, TaskVersionDetailResponse } from "@talqing/sdk";
import { api } from "@/lib/api";
import { ConfigDiffView } from "@/app/components/ConfigDiffView";
import { VersionHistoryModal as HistoryModal } from "@/app/components/VersionHistoryModal";
import { diffTaskConfigs } from "./taskDiff";

/** A version is fetched whole but diffed on its config alone, and the raw
    response is what "Copy JSON" should hand over — so the modal carries both. */
type Side = { config: TaskConfig; detail: TaskVersionDetailResponse | null };

export function VersionHistoryModal({
  task,
  draft,
  toolNameById,
  integrationNameById,
  faqNameById,
  onClose,
  onRolledBack,
}: {
  task: TaskResponse;
  /** The draft as it stands in the editor, unsaved edits included. */
  draft: TaskConfig;
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
      (task.versions ?? []).map((v) => ({
        version: v.version,
        published_at: v.published_at,
        meta: v.published_by ? (
          <span className="truncate text-[11.5px] leading-4 text-ink-soft">{v.published_by}</span>
        ) : undefined,
      })),
    [task.versions],
  );

  return (
    <HistoryModal<Side>
      title={`Version history · ${task.config.name}`}
      subject="task"
      versions={versions}
      live={task.published_version ?? null}
      draft={{ config: draft, detail: null }}
      draftMeta={
        <span className="text-[11.5px] leading-4 text-muted">
          {task.published_version ? "what you are editing" : "never published"}
        </span>
      }
      loadVersion={async (version) => {
        const detail = await api.getTaskVersion(task.id, version);
        return { config: detail.config, detail };
      }}
      renderDiff={(before, after, emptyLabel) => (
        <ConfigDiffView
          diff={diffTaskConfigs(before.config, after.config, {
            tool: toolNameById,
            integration: integrationNameById,
            faq: faqNameById,
          })}
          emptyLabel={emptyLabel}
        />
      )}
      copyJson={(side) => JSON.stringify(side.detail, null, 2)}
      confirmSub={(version) =>
        `Your current draft is replaced by v${version}, and v${version} becomes live immediately — ` +
        "every run from now on uses it, and so does every email batch still drafting with this " +
        "task. No new version is created."
      }
      rollback={async (version) => {
        await api.rollbackTaskVersion(task.id, version);
        await onRolledBack();
      }}
      onClose={onClose}
    />
  );
}
