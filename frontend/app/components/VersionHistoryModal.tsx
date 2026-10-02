"use client";

import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { Badge, Button, CopyButton, Modal, Select } from "./ui";
import { timeAgo } from "./diff";

/* ── the version history overlay ────────────────────────────────────────────
   Tools, agents and tasks all publish an immutable snapshot every time. This is
   the whole surface for them: what each version holds, what changed between any
   two of them (or between one and the draft you have open), and putting one back
   into production.

   Generic over the snapshot type, because only three things differ between the
   three: how a version is fetched, how two of them are drawn, and what rolling
   back costs. Three copies of this file would drift in behaviour, and a version
   history that behaves differently in three places is worse than one
   abstraction.

   "Draft" is a side here, not a version — it is compared like one, but it has
   no `published_at` and cannot be made live without publishing. */

/** Either a published version number, or the unsaved draft in the editor. */
type Side = number | "draft";

function sideLabel(side: Side, liveVersion: number | null | undefined): string {
  if (side === "draft") return "Draft";
  return side === liveVersion ? `v${side} (live)` : `v${side}`;
}

export type HistoryVersion = {
  version: number;
  published_at: string;
  /** The second line of the rail row — a changelog, a publisher, whatever the
      caller has that tells one version from another at a glance. */
  meta?: React.ReactNode;
};

export function VersionHistoryModal<T>({
  title,
  subject,
  versions,
  live,
  draft,
  draftMeta,
  loadVersion,
  renderDiff,
  rollback,
  copyJson,
  confirmSub,
  onClose,
}: {
  title: string;
  /** What is being rolled back, for the confirm dialog's verbs: "tool", "agent",
      "task". */
  subject: string;
  versions: HistoryVersion[];
  live: number | null;
  /** The draft as it stands in the editor, unsaved edits included. */
  draft: T;
  draftMeta: React.ReactNode;
  loadVersion: (version: number) => Promise<T>;
  renderDiff: (before: T, after: T, emptyLabel: string) => React.ReactNode;
  rollback: (version: number) => Promise<void>;
  copyJson: (loaded: T) => string;
  /** The consequences of going live, in the caller's own words. */
  confirmSub: (version: number) => string;
  onClose: () => void;
}): JSX.Element {
  /* Opens on the comparison that matters most: what is live against what you
     have been editing. */
  const [base, setBase] = useState<Side>(live ?? "draft");
  const [target, setTarget] = useState<Side>("draft");
  const [loaded, setLoaded] = useState<Record<number, T>>({});
  const [confirming, setConfirming] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<{ message: string; errors: string[] } | null>(null);

  const needed = [base, target, confirming].filter((s): s is number => typeof s === "number");
  const missing = needed.filter((v) => !(v in loaded));

  useEffect(() => {
    if (!missing.length) return;
    let active = true;
    Promise.all(missing.map((v) => loadVersion(v).then((detail) => [v, detail] as const)))
      .then((pairs) => {
        if (active) setLoaded((prev) => ({ ...prev, ...Object.fromEntries(pairs) }));
      })
      .catch((error) => {
        if (active) setErr({ message: apiErrorMessage(error, "Could not load that version."), errors: [] });
      });
    return () => { active = false; };
  }, [missing.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps -- keyed by which versions are missing

  function snapshot(side: Side): T | null {
    if (side === "draft") return draft;
    return loaded[side] ?? null;
  }

  const beforeSnapshot = snapshot(base);
  const afterSnapshot = snapshot(target);

  const sides: Side[] = ["draft", ...versions.map((v) => v.version)];

  /* The version the actions act on: strictly the right-hand side, the state the
     diff is read *for*. No falling back to the left — "changes from v2 to Draft"
     is read for what the draft is, and offering to make v2 live there offers to
     throw away the very thing on screen. */
  const acting = typeof target === "number" ? target : null;

  async function confirm(version: number): Promise<void> {
    setBusy(true);
    setErr(null);
    try {
      await rollback(version);
      onClose();
    } catch (error) {
      setErr({
        message: apiErrorMessage(error, `Could not roll back to that ${subject} version.`),
        errors: apiErrorList(error),
      });
      setConfirming(null);
    } finally {
      setBusy(false);
    }
  }

  const confirmTarget = confirming !== null ? (loaded[confirming] ?? null) : null;

  return (
    <>
      <Modal title={title} onClose={onClose} width="max-w-[1120px]">
        <div className="mb-6 grid h-[min(660px,calc(100vh-220px))] min-h-0 grid-cols-1 gap-4 md:grid-cols-[214px_minmax(0,1fr)]">
          {/* ── the rail ── */}
          <div className="scroll-thin -mx-1 min-h-0 overflow-y-auto px-1 md:border-r md:border-line md:pr-3">
            {/* Picks the right-hand side — the version you are inspecting, and
                the one the actions act on. The left side is the reference you
                read it against, and stays on the live version unless changed. */}
            <div className="px-2.5 pb-2 text-[10.5px] font-semibold uppercase leading-4 tracking-[0.06em] text-faint">
              Inspect
            </div>
            <div className="flex flex-col gap-1">
              <RailRow label="Draft" meta={draftMeta} selected={target === "draft"} onSelect={() => setTarget("draft")} />
              {versions.map((v) => (
                <RailRow
                  key={v.version}
                  label={`v${v.version}`}
                  badge={v.version === live ? <Badge variant="live" dot>live</Badge> : undefined}
                  meta={
                    <>
                      <span className="text-[11.5px] leading-4 text-muted">{timeAgo(v.published_at)}</span>
                      {v.meta}
                    </>
                  }
                  selected={target === v.version}
                  onSelect={() => setTarget(v.version)}
                />
              ))}
              {versions.length === 0 && (
                <div className="px-2 py-3 text-[12.5px] leading-5 text-muted">
                  Nothing published yet. Publishing freezes the draft as v1.
                </div>
              )}
            </div>
          </div>

          {/* ── the diff ── */}
          <div className="flex min-h-0 min-w-0 flex-col gap-3">
            <div className="flex flex-none flex-wrap items-center gap-2">
              {/* Any two sides, in either direction. Worded as a direction, not
                  as "compare A with B": the diff is not symmetric — `+` means
                  present in the right-hand side, `−` means dropped from the
                  left — so which side is which has to be readable. */}
              <span className="text-[12px] leading-5 text-muted">Changes from</span>
              <SideSelect label="Compare from" value={base} onChange={setBase} sides={sides} live={live} />
              <span className="text-[12px] leading-5 text-muted">to</span>
              <SideSelect label="Compare to" value={target} onChange={setTarget} sides={sides} live={live} />
              <button
                type="button"
                onClick={() => { setBase(target); setTarget(base); }}
                title="Swap the two sides"
                aria-label="Swap the two sides"
                className="grid h-8 w-8 flex-none place-items-center rounded-lg border border-line-2 bg-white text-muted transition-colors hover:border-line-strong hover:text-ink"
              >
                <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M2 5.5h10L9.5 3M14 10.5H4L6.5 13" />
                </svg>
              </button>
              <div className="ml-auto flex items-center gap-2">
                {acting !== null && loaded[acting] && (
                  <CopyButton
                    value={() => copyJson(loaded[acting])}
                    label={`Copy v${acting} JSON`}
                    ariaLabel={`Copy the definition of version ${acting} as JSON`}
                  />
                )}
                {acting !== null && acting !== live && (
                  <Button variant="secondary" size="sm" onClick={() => setConfirming(acting)} disabled={busy}>
                    Make v{acting} live
                  </Button>
                )}
              </div>
            </div>

            {err && (
              <div
                role="alert"
                className="grid flex-none gap-1 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger"
              >
                <span className="font-medium">{err.message}</span>
                {err.errors.map((line, i) => (
                  <span key={i} className="[overflow-wrap:anywhere]">{line}</span>
                ))}
              </div>
            )}

            <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
              {beforeSnapshot && afterSnapshot ? (
                renderDiff(
                  beforeSnapshot,
                  afterSnapshot,
                  `${sideLabel(base, live)} and ${sideLabel(target, live)} are identical.`,
                )
              ) : (
                <div className="text-[13px] leading-5 text-muted">Loading…</div>
              )}
            </div>
          </div>
        </div>
      </Modal>

      {confirming !== null && (
        <Modal
          title={`Make v${confirming} live`}
          sub={confirmSub(confirming)}
          onClose={() => !busy && setConfirming(null)}
          width="max-w-[720px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirming(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="danger" onClick={() => confirm(confirming)} disabled={busy || !confirmTarget}>
                {busy ? "Rolling back…" : `Make v${confirming} live`}
              </Button>
            </>
          }
        >
          <div className="mb-1 text-[12px] font-semibold uppercase leading-4 tracking-[0.04em] text-ink-soft">
            What the draft loses
          </div>
          <div className="scroll-thin max-h-[42vh] overflow-y-auto pt-1">
            {/* Against the draft specifically, not against the left-hand side:
                the draft is what this replaces. */}
            {confirmTarget ? (
              renderDiff(draft, confirmTarget, `Your draft already matches v${confirming} — nothing is lost.`)
            ) : (
              <div className="text-[13px] leading-5 text-muted">Loading…</div>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}

function RailRow({
  label,
  badge,
  meta,
  selected,
  onSelect,
}: {
  label: string;
  badge?: React.ReactNode;
  meta: React.ReactNode;
  selected: boolean;
  onSelect: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex min-w-0 flex-col gap-0.5 rounded-lg border px-2.5 py-2 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10",
        selected ? "border-line-strong bg-subtle" : "border-transparent hover:bg-hover",
      )}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="flex-none font-mono text-[13px] font-semibold leading-5 text-ink">{label}</span>
        {badge}
      </span>
      <span className="flex min-w-0 flex-col">{meta}</span>
    </button>
  );
}

function SideSelect({
  label,
  value,
  onChange,
  sides,
  live,
}: {
  label: string;
  value: Side;
  onChange: (next: Side) => void;
  sides: Side[];
  live: number | null;
}): JSX.Element {
  return (
    <Select
      aria-label={label}
      value={String(value)}
      onChange={(e) => onChange(e.target.value === "draft" ? "draft" : Number(e.target.value))}
      className="w-auto"
      triggerClassName="min-h-8 py-1 text-[13px]"
      // Anything that gets iterated on passes 20 versions quickly, and by then
      // scrolling to "the one from before the rewrite" is the slow way.
      searchable={sides.length > 12}
    >
      {sides.map((side) => (
        <option key={String(side)} value={String(side)}>
          {sideLabel(side, live)}
        </option>
      ))}
    </Select>
  );
}
