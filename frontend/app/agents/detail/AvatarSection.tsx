"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentConfig, AvatarCatalogEntryResponse, AvatarItemResponse } from "@talqing/sdk";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Badge, CardHead, Label, LIFT_ON_HOVER, Panel, Select } from "@/app/components/ui";
import { avatarFacetLabel } from "./agentConfig";

const PAGE_SIZE = 40;

/** Anam face gallery: filtered, infinitely scrolled, loaded only for video agents. */
function useAvatarGallery(enabled: boolean, activeVersion: string, renderStyle: string) {
  const [avatars, setAvatars] = useState<AvatarItemResponse[] | null>(null);
  const [activeVersions, setActiveVersions] = useState<string[]>([]);
  const [renderStyles, setRenderStyles] = useState<string[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [replacing, setReplacing] = useState(false);
  // Filter changes race with in-flight pages; only the newest request may write.
  const requestSeq = useRef(0);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(
    async (offset: number, replace: boolean) => {
      if (!enabled) return;
      const seq = ++requestSeq.current;
      setLoading(true);
      if (replace) {
        setReplacing(true);
        setAvatars(null);
      }
      try {
        const page = await api.listAvatars({
          active_version: activeVersion || undefined,
          render_style: renderStyle || undefined,
          offset,
          limit: PAGE_SIZE,
        });
        if (seq !== requestSeq.current) return;
        setAvatars((current) => (replace || !current ? page.avatars ?? [] : [...current, ...(page.avatars ?? [])]));
        setHasMore(Boolean(page.has_more));
        if (page.active_versions) setActiveVersions(page.active_versions);
        if (page.render_styles) setRenderStyles(page.render_styles);
      } catch {
        // The gallery is a picker, not the page. A failed page leaves the
        // existing faces in place; a failed reload shows the empty state.
        if (seq !== requestSeq.current) return;
        if (replace) {
          setAvatars([]);
          setHasMore(false);
          setActiveVersions([]);
          setRenderStyles([]);
        }
      } finally {
        if (seq === requestSeq.current) {
          setLoading(false);
          setReplacing(false);
        }
      }
    },
    [enabled, activeVersion, renderStyle],
  );

  useEffect(() => {
    if (!enabled) {
      setAvatars(null);
      setActiveVersions([]);
      setRenderStyles([]);
      setHasMore(false);
      return;
    }
    load(0, true);
  }, [enabled, load]);

  // Fetch the next page once the end of the grid comes into view.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !enabled || !hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries[0]?.isIntersecting) return;
        if (loading || replacing || !hasMore) return;
        const offset = avatars?.length ?? 0;
        if (offset === 0) return;
        load(offset, false);
      },
      { root: null, rootMargin: "240px", threshold: 0 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [enabled, hasMore, loading, replacing, avatars?.length, load]);

  return { avatars, activeVersions, renderStyles, loading, replacing, sentinelRef };
}

function FacetSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Label>{label}</Label>
      <Select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        <option value="">All</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {avatarFacetLabel(option)}
          </option>
        ))}
      </Select>
    </div>
  );
}

export function AvatarSection({
  config,
  avatarEntry,
  onSelect,
}: {
  config: AgentConfig;
  avatarEntry: AvatarCatalogEntryResponse | null;
  onSelect: (avatarId: string, name: string) => void;
}) {
  // Gallery facets only — they filter the picker, never the saved config.
  const [activeVersion, setActiveVersion] = useState("");
  const [renderStyle, setRenderStyle] = useState("");
  const gallery = useAvatarGallery(config.channel === "video", activeVersion, renderStyle);
  const perMinute = avatarEntry?.pricing?.per_minute;

  return (
    <Panel className={LIFT_ON_HOVER}>
      <CardHead title="Avatar" desc="Cara generation + the face callers see">
        {config.avatar?.avatar_id ? (
          <Badge variant="live" dot>selected</Badge>
        ) : (
          <Badge variant="warn">pick one to publish</Badge>
        )}
      </CardHead>

      {(gallery.activeVersions.length > 0 || gallery.renderStyles.length > 0) && (
        <div className="mb-3.5 grid max-w-[560px] grid-cols-1 gap-3 sm:grid-cols-2">
          {gallery.activeVersions.length > 0 && (
            <FacetSelect label="Version" value={activeVersion} options={gallery.activeVersions} onChange={setActiveVersion} />
          )}
          {gallery.renderStyles.length > 0 && (
            <FacetSelect label="Style" value={renderStyle} options={gallery.renderStyles} onChange={setRenderStyle} />
          )}
        </div>
      )}

      {gallery.avatars === null || gallery.replacing ? (
        <p className="text-[12.5px] leading-relaxed text-muted">Loading the avatar gallery…</p>
      ) : gallery.avatars.length === 0 ? (
        <p className="text-[12.5px] leading-relaxed text-muted">No matching avatars for these filters right now.</p>
      ) : (
        <>
          <div
            className={cn(
              // Auto-fill on a fixed minimum rather than a column count: this is
              // a face picker with ~120 entries, so the tile wants to stay
              // thumbnail-sized and the panel wants to fit as many as it can. A
              // fixed count grew each tile with the panel and turned five faces
              // into a wall.
              "grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-2",
              gallery.replacing && "pointer-events-none opacity-60",
            )}
            aria-busy={gallery.loading}
          >
            {gallery.avatars.map((avatar) => {
              const selected = config.avatar?.avatar_id === avatar.id;
              const name = avatar.name || avatar.id;
              const label = avatar.variant ? `${name} (${avatar.variant})` : name;
              return (
                <button
                  key={avatar.id}
                  type="button"
                  onClick={() => onSelect(avatar.id, name)}
                  title={label}
                  aria-pressed={selected}
                  className={cn(
                    "group flex flex-col gap-1 overflow-hidden rounded-lg border bg-surface p-1 text-left transition-colors",
                    selected ? "border-ink ring-2 ring-ink/15" : "border-line hover:border-line-strong hover:bg-subtle",
                  )}
                >
                  {avatar.image_url ? (
                    <img src={avatar.image_url} alt={label} loading="lazy" className="aspect-square w-full rounded-md object-cover" />
                  ) : (
                    <span aria-hidden className="grid aspect-square w-full place-items-center rounded-md bg-subtle text-muted">
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                        <circle cx="12" cy="8" r="4" />
                        <path d="M4 21a8 8 0 0 1 16 0" />
                      </svg>
                    </span>
                  )}
                  <span className="truncate px-0.5 text-[11.5px] font-medium text-ink">{label}</span>
                </button>
              );
            })}
          </div>
          <div ref={gallery.sentinelRef} className="h-1 w-full" aria-hidden />
          {gallery.loading && !gallery.replacing && (
            <p className="mt-2 text-[12.5px] text-muted">Loading more faces…</p>
          )}
        </>
      )}

      <p className="mt-3 text-[12.5px] leading-relaxed text-muted">
        Anam renders the video from the agent&apos;s own audio, whichever model produces it.{" "}
        Version and style only filter the gallery; runtime uses Anam&apos;s default for the selected face.
        Avatar time is billed per second for the whole call
        {perMinute ? ` (≈ $${perMinute}/min on top of the model stack)` : ""}.
      </p>
    </Panel>
  );
}
