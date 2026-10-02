"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../../lib/api";
import { Select, Input, Skeleton } from "./ui";
import { cn } from "@/lib/cn";
import { accentFlag, flagEmoji, langFlag, withFlag } from "../../lib/flags";

// One voice picker for every TTS provider. A trigger shows the current voice
// (flag + name); opening reveals a searchable, paginated list with provider
// facets. Only one preview plays at a time; audio stops on close/unmount.

export type LibVoice = {
  id: string;
  name?: string | null;
  gender?: string | null;
  language?: string | null;
  accent?: string | null;
  country?: string | null; // ISO 3166-1 alpha-2
  locale?: string | null;
  description?: string | null;
  sample_url?: string | null;
  image_url?: string | null;
  tier?: string | null;
  owner_id?: string | null;
};
type LangOption = { code: string; name: string };
/** A voice's own tuning, as its author left it. Copied onto the agent so the
 *  call sounds like the sample the author auditioned. */
export type VoiceSettings = { stability: number | null; similarity_boost: number | null };

// flag for a voice: real country first, else its accent or language flag
// (Deepgram/xAI voices carry no country) — "multilingual" gets a globe.
function voiceFlag(v: LibVoice): string {
  return (
    flagEmoji(v.country) ||
    accentFlag(v.accent) ||
    (v.language === "multilingual" ? "🌐" : langFlag(v.language))
  );
}

function accentLabel(accent: string): string {
  return accent.toLowerCase() === "telegu" ? "telugu" : accent;
}

// Name/description widths for the placeholder rows, varied so the gallery reads
// as a list of different voices loading rather than a repeating bar. Six rows
// overflow the panel at any height it opens at, which is what keeps it from
// growing under the cursor when the real voices land. Spelled out as whole class
// names because Tailwind generates arbitrary values only from literals it can
// find in the source — an interpolated `w-[${n}%]` produces no CSS at all.
const SKELETON_ROWS: [string, string][] = [
  ["w-[38%]", "w-[66%]"],
  ["w-[52%]", "w-[74%]"],
  ["w-[34%]", "w-[58%]"],
  ["w-[46%]", "w-[70%]"],
  ["w-[40%]", "w-[62%]"],
  ["w-[56%]", "w-[48%]"],
];

// The panel is portalled to <body>, so its place on screen is measured off the
// trigger rather than inherited from it. See the render for why it cannot simply
// hang off the trigger as an absolutely-positioned child.
// It only ever opens downward. Select and DatePicker flip above their trigger
// when space is tight, and that is wrong here: their popups are small, this one
// is 420px, so the flip would fire on almost any field in the middle of the page
// and throw the list up over the settings the user was just reading. When there
// is no room below, make room — scroll the field up — rather than change
// direction.
const PANEL_MAX_HEIGHT = 420;
const PANEL_MIN_HEIGHT = 240;
const PANEL_GAP = 6;
const VIEWPORT_EDGE = 12;

const roomBelow = (rect: DOMRect) => window.innerHeight - rect.bottom - PANEL_GAP - VIEWPORT_EDGE;

function panelPosition(r: DOMRect): React.CSSProperties {
  return {
    position: "fixed",
    left: r.left,
    // Exactly the trigger's width, which is what `left-0 right-0` bought while
    // the panel was still a child of it.
    width: r.width,
    top: r.bottom + PANEL_GAP,
    // Never past the bottom of the window; the voice list scrolls inside. The
    // floor keeps a panel that has followed its trigger toward the bottom of the
    // screen usable rather than a sliver — better to overhang than to collapse.
    maxHeight: Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_MAX_HEIGHT, roomBelow(r))),
  };
}

export function VoicePicker({
  provider,
  providerLabel,
  model,
  kind,
  value,
  valueName,
  onSelect,
  withVoiceSettings = false,
  initialLanguage,
}: {
  provider: string;
  /** Display name for the provider ("ElevenLabs"), used when the gallery is
   *  showing the workspace's own account. Falls back to the provider id. */
  providerLabel?: string;
  model?: string;
  /** Which catalog kind owns the voice. A speech-to-speech model picks from its
   *  own roster, not the TTS one. Defaults to "tts" at the API. */
  kind?: "tts" | "realtime";
  value: string | null | undefined;
  valueName?: string | null;
  onSelect: (
    id: string,
    name: string | null,
    language: string | null,
    settings: VoiceSettings | null,
  ) => void;
  /** Whether the chosen voice's own settings should travel with it — true only
   *  for a model whose catalog entry declares `supports_voice_settings`. They
   *  have to be read here, at the pick, because ElevenLabs will not apply a
   *  voice's own settings to a request that carries any override, and every
   *  Talqing request carries a speed. */
  withVoiceSettings?: boolean;
  /** Seeds the language filter — normally the agent's language, so the gallery
   *  opens on voices that can actually speak it. The user can still widen it. */
  initialLanguage?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [language, setLanguage] = useState("");
  const [accent, setAccent] = useState("");
  const [gender, setGender] = useState("");

  const [voices, setVoices] = useState<LibVoice[]>([]);
  const [languages, setLanguages] = useState<LangOption[]>([]);
  const [accents, setAccents] = useState<string[]>([]);
  const [genders, setGenders] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  // whether this gallery is the workspace's own provider account, not Talqing's
  const [workspaceKey, setWorkspaceKey] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [playingId, setPlayingId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  // id → name, accumulated across every loaded page so the trigger can always
  // show the selected voice's name even when the current filter excludes it.
  const [nameCache, setNameCache] = useState<Record<string, string>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const requestSeq = useRef(0);
  // Where the portalled panel sits, measured off the trigger when it opens.
  const [pos, setPos] = useState<React.CSSProperties | null>(null);

  // debounce the search box (a server round-trip per keystroke would be wasteful)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 250);
    return () => clearTimeout(t);
  }, [search]);

  const filterLanguageValue = initialLanguage || "";

  // a provider/model switch invalidates every filter and the list
  useEffect(() => {
    setSearch(""); setDebounced(""); setLanguage(filterLanguageValue); setAccent(""); setGender("");
    setVoices([]); setAccents([]); setLanguages([]); setGenders([]);
    setPage(0); setHasMore(false); setTotal(null); setError(null); setWorkspaceKey(false);
  // filterLanguageValue is deliberately excluded: the effect below already syncs
  // it, and adding it here would wipe every other filter whenever the language
  // alone changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, model]);

  useEffect(() => {
    setLanguage(filterLanguageValue);
  }, [filterLanguageValue]);

  // a language switch changes which accents are relevant
  useEffect(() => {
    setAccent(""); setAccents([]);
  }, [language]);

  function stopAudio() {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
    setPlayingId(null);
  }

  function togglePreview(v: LibVoice) {
    if (replacing) return;
    if (!v.sample_url) return;
    if (playingId === v.id) { stopAudio(); return; }
    stopAudio();
    const a = new Audio(v.sample_url);
    a.onended = () => setPlayingId((cur) => (cur === v.id ? null : cur));
    a.onerror = () => setPlayingId((cur) => (cur === v.id ? null : cur));
    audioRef.current = a;
    setPlayingId(v.id);
    a.play().catch(() => setPlayingId((cur) => (cur === v.id ? null : cur)));
  }

  const loadPage = useCallback(
    async (pageNum: number, replace: boolean) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      if (replace) setReplacing(true);
      try {
        const r = await api.listVoices({
          provider, model, kind, search: debounced, language, accent, gender, page: pageNum,
        });
        if (seq !== requestSeq.current) return;
        setError(null);
        const got: LibVoice[] = r.voices || [];
        setVoices((cur) => (replace ? got : [...cur, ...got]));
        setNameCache((c) => {
          const n = { ...c };
          for (const v of got) if (v.name) n[v.id] = v.name;
          return n;
        });
        setHasMore(!!r.has_more);
        setTotal(r.total_count ?? got.length);
        setPage(pageNum);
        setWorkspaceKey(!!r.workspace_key);
        if (r.languages) setLanguages(r.languages);
        if (r.genders) setGenders(r.genders);
        if (r.accents?.length) {
          setAccents((cur) => Array.from(new Set([...cur, ...r.accents])));
        }
      } catch {
        if (seq !== requestSeq.current) return;
        setError("voices unavailable");
        if (replace) { setVoices([]); setHasMore(false); setTotal(0); }
      } finally {
        if (seq === requestSeq.current) {
          setLoading(false);
          setReplacing(false);
        }
      }
    },
    [provider, model, kind, debounced, language, accent, gender],
  );

  // (re)load page 0 on mount and whenever provider/model or a filter changes
  useEffect(() => {
    loadPage(0, true);
  }, [loadPage]);

  // Opening is the one moment we may move the page: with no room below, make
  // room — scroll the field up — rather than flip the panel above it. See
  // `panelPosition` for why it never changes direction.
  function openPanel() {
    let r = rootRef.current!.getBoundingClientRect();
    if (roomBelow(r) < PANEL_MIN_HEIGHT) {
      // Instant, not smooth: the rect measured next has to be the one the panel
      // is actually placed against.
      rootRef.current!.scrollIntoView({ block: "center" });
      r = rootRef.current!.getBoundingClientRect();
    }
    setPos(panelPosition(r));
    setOpen(true);
  }

  // close on outside-click / Escape; stop audio + focus search on open
  useEffect(() => {
    if (!open) { stopAudio(); return; }
    searchRef.current?.focus();
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (rootRef.current?.contains(t) || popRef.current?.contains(t)) return;
      // the filter sub-dropdowns (custom Select) portal their option list to
      // <body>, outside rootRef — a click there must NOT close the panel
      if ((t as Element)?.closest?.(".uisel-pop")) return;
      setOpen(false);
    }
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") setOpen(false); }
    // Follow the trigger rather than close. This used to give up and shut the
    // panel, because `pos` was measured once at open and a scroll left it
    // stranded — but losing a 420px gallery mid-browse because the page moved a
    // few pixels is the worse half of that trade. Re-measuring is cheap, so the
    // panel just keeps up. Scrolling the voice list itself is exempt: that is an
    // inner scroll, it moves no trigger, and it is how the next page loads.
    function onAway(e: Event) {
      if (popRef.current?.contains(e.target as Node)) return;
      const el = rootRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      // Only once the field it belongs to is off screen entirely is there
      // nothing left to anchor to.
      if (r.bottom < 0 || r.top > window.innerHeight) {
        setOpen(false);
        return;
      }
      setPos(panelPosition(r));
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onAway, true);
    window.addEventListener("resize", onAway);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onAway, true);
      window.removeEventListener("resize", onAway);
    };
  }, [open]);

  useEffect(() => () => stopAudio(), []);

  const langName = useMemo(() => {
    const m = new Map(languages.map((l) => [l.code, l.name]));
    return (code?: string | null) =>
      !code ? "" : code === "multilingual" ? "Multilingual" : m.get(code) || code.toUpperCase();
  }, [languages]);

  function onListScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    if (hasMore && !loading && !replacing && el.scrollHeight - el.scrollTop - el.clientHeight < 140) {
      loadPage(page + 1, false);
    }
  }

  async function choose(v: LibVoice) {
    if (replacing) return;
    let id = v.id;
    const sharedLibraryProvider = "elevenlabs";
    if (provider === sharedLibraryProvider && v.owner_id) {
      setAdding(true);
      setError(null);
      try {
        const added = await api.addSharedVoice({ voice_id: v.id, owner_id: v.owner_id, name: v.name || v.id });
        id = added.voice_id || v.id;
      } catch {
        setError("add voice failed");
        return;
      } finally {
        setAdding(false);
      }
    }
    // Read AFTER any save above, and with the id that returned: a shared-library
    // voice answers `voice_not_found` until it is in the account.
    let settings: VoiceSettings | null = null;
    if (withVoiceSettings && provider === sharedLibraryProvider) {
      setAdding(true);
      try {
        const s = await api.elevenLabsVoiceSettings(id);
        settings = { stability: s.stability ?? null, similarity_boost: s.similarity_boost ?? null };
      } catch {
        // Deliberately abandons the selection rather than taking the voice with
        // no settings: that quietly renders it in ElevenLabs' generic default
        // instead of its own tuning, which is the exact failure this reads them
        // to avoid. The voice is already saved, so retrying costs nothing.
        setError("could not read this voice's settings — try again");
        return;
      } finally {
        setAdding(false);
      }
    }
    const voiceLanguage = v.language && v.language !== "multilingual" ? v.language : null;
    onSelect(id, v.name || v.id, voiceLanguage, settings);
    setOpen(false);
  }

  // Resolve the label from the ACTUAL voice id first (nameCache accumulates names
  // from every loaded page; small providers load their whole list, so the id
  // always resolves). Only fall back to the stored `voice_name` hint — which a
  // co-pilot/provider switch can leave stale — when the id isn't loaded yet.
  const selectedVoice = useMemo(() => voices.find((v) => v.id === value) || null, [voices, value]);
  const triggerLabel =
    (value ? nameCache[value] : null) || selectedVoice?.name || valueName || value || null;
  const triggerFlag = selectedVoice ? voiceFlag(selectedVoice) : "";

  return (
    <div className="relative" ref={rootRef} data-open={open || undefined}>
      <button
        type="button"
        className={cn(
          "flex min-h-[36px] w-full items-center justify-between gap-2 rounded-lg border border-line-2 bg-surface px-3 py-2 text-left text-sm text-ink transition-colors hover:border-line-strong focus:outline-none focus:border-ink focus:ring-2 focus:ring-ink/10",
          open && "border-ink ring-2 ring-ink/10",
        )}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => (open ? setOpen(false) : openPanel())}
      >
        <span className="flex min-w-0 items-center gap-2 overflow-hidden">
          {triggerLabel ? (
            <>
              {triggerFlag && <span className="flex-none text-[15px] leading-none" aria-hidden>{triggerFlag}</span>}
              <span className="overflow-hidden text-ellipsis whitespace-nowrap">{triggerLabel}</span>
            </>
          ) : (
            <span className="text-muted">Select a voice</span>
          )}
        </span>
        <svg
          className="h-3.5 w-3.5 flex-none text-muted transition-transform"
          style={{ transform: open ? "rotate(180deg)" : undefined }}
          viewBox="0 0 12 12"
          aria-hidden
        >
          <path d="M2.5 4.5L6 8l3.5-3.5" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {/* Portalled to <body>, not hung off the trigger. Every config card wears
          LIFT_ON_HOVER, whose `hover:-translate-y-0.5` puts a transform on the
          card under the cursor — and a transform makes a stacking context, which
          traps this panel's z-index inside the Models card. The Tools card below
          then painted straight over the open list, and flickered as the pointer
          crossed between them, because whichever card was hovered won. A portal
          takes the panel out of that argument entirely. z-[130] matches Select
          and DatePicker: above Modal's z-[120]. */}
      {open &&
        pos &&
        createPortal(
          <div
            ref={popRef}
            style={pos}
            className="z-[130] flex flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-pop animate-slide-up"
            role="dialog"
            aria-label="Select voice"
          >
            <div className="flex items-center justify-between gap-3 border-b border-line px-3.5 py-2.5">
              <div className="text-[14px] font-semibold leading-5 text-ink">Select voice</div>
              <div className="font-mono text-[11px] text-muted">
                {adding
                  ? "Adding voice..."
                  : total != null
                    ? `${total.toLocaleString()} voices`
                    : loading
                      ? "Loading…"
                      : ""}
              </div>
            </div>

            {/* Whose account this is. Only shown once it is not ours: the same
                provider shows a different roster per workspace, and without
                this a voice someone cloned appearing (or not) is a mystery. */}
            {workspaceKey && (
              <div className="flex items-center gap-1.5 border-b border-line bg-subtle px-3.5 py-1.5 text-[11px] text-muted">
                <svg className="h-3 w-3 flex-none" viewBox="0 0 12 12" aria-hidden>
                  <path
                    d="M7.4 1.6a2.6 2.6 0 00-2.34 3.74L1.6 8.8v1.6h1.6v-1.2h1.2V8h1.2l.66-.66A2.6 2.6 0 107.4 1.6zm.7 1.5a.8.8 0 110 1.6.8.8 0 010-1.6z"
                    fill="currentColor"
                  />
                </svg>
                <span>Your {providerLabel || provider} account</span>
              </div>
            )}

            <div className="border-b border-line p-2.5">
              <Input
                ref={searchRef}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search voices…"
                aria-label="Search voices"
              />
            </div>

            {(languages.length > 0 || accents.length > 0 || genders.length > 1) && (
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-2.5 py-2">
                {/* A provider that ships no per-voice language metadata (the static
                    enums: Sarvam, OpenAI, Gemini) returns no language list, so this
                    filter simply does not appear for it. */}
                {languages.length > 0 && (
                  <Select
                    className="min-w-[140px] flex-1"
                    popupClassName="uisel-pop"
                    value={language}
                    title="Filter by language"
                    onChange={(e) => setLanguage(e.target.value)}
                  >
                    <option value="">All languages</option>
                    {languages.map((l) => (
                      <option key={l.code} value={l.code}>{withFlag(langFlag(l.code), l.name)}</option>
                    ))}
                  </Select>
                )}
                {accents.length > 0 && (
                  <Select
                    className="min-w-[140px] flex-1"
                    popupClassName="uisel-pop"
                    value={accent}
                    title="Filter by accent"
                    onChange={(e) => setAccent(e.target.value)}
                  >
                    <option value="">Any accent</option>
                    {accents.map((a) => (
                      <option key={a} value={a}>{withFlag(accentFlag(a), accentLabel(a))}</option>
                    ))}
                  </Select>
                )}
                {genders.length > 1 && (
                  <Select
                    className="min-w-[140px] flex-1"
                    popupClassName="uisel-pop"
                    value={gender}
                    title="Filter by gender"
                    onChange={(e) => setGender(e.target.value)}
                  >
                    <option value="">Any gender</option>
                    {genders.map((g) => (
                      <option key={g} value={g}>{g.charAt(0).toUpperCase() + g.slice(1)}</option>
                    ))}
                  </Select>
                )}
              </div>
            )}

            <div
              className="scroll-thin relative flex-1 overflow-y-auto"
              role="listbox"
              aria-busy={loading}
              onScroll={onListScroll}
            >
              {voices.length === 0 && loading ? (
                /* Switching provider clears the list, so there is nothing for the
                   overlay below to cover — `flex-1` over no content is zero-high
                   and the panel would sit empty until the rows arrived. These
                   stand in at the row's own geometry so the wait reads as a
                   gallery loading rather than as a picker with no voices in it. */
                <div aria-hidden>
                  {SKELETON_ROWS.map(([name, description], i) => (
                    <div
                      key={i}
                      className="flex items-center gap-3 border-b border-line px-3.5 py-2.5 last:border-b-0"
                    >
                      <Skeleton className="h-10 w-10 flex-none rounded-full" />
                      <div className="min-w-0 flex-1">
                        <Skeleton className={cn("h-[13px]", name)} />
                        <Skeleton className={cn("mt-1.5 h-3", description)} />
                      </div>
                      <Skeleton className="h-4 w-14 flex-none rounded-full" />
                    </div>
                  ))}
                </div>
              ) : voices.length === 0 ? (
                error ? (
                  <div className="p-3.5 text-[13px] text-muted">Voice gallery unavailable.</div>
                ) : (
                  <div className="p-3.5 text-[13px] text-muted">No voices match these filters.</div>
                )
              ) : (
                voices.map((v) => {
                  const flag = voiceFlag(v);
                  const isSel = v.id === value;
                  const isPlaying = playingId === v.id;
                  const hasPreview = !!v.sample_url;
                  return (
                    <div
                      key={v.id}
                      role="option"
                      aria-selected={isSel}
                      tabIndex={0}
                      className={cn(
                        "flex cursor-pointer items-center gap-3 border-b border-line px-3.5 py-2.5 transition-colors last:border-b-0 hover:bg-subtle focus:bg-subtle focus:outline-none",
                        isSel && "bg-subtle",
                        replacing && "pointer-events-none",
                      )}
                      onClick={() => choose(v)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          choose(v);
                        }
                      }}
                    >
                      <button
                        type="button"
                        className={cn(
                          "flex h-10 w-10 flex-none items-center justify-center overflow-hidden rounded-full border border-line-2 bg-subtle text-ink-soft transition-colors",
                          hasPreview && "border-ink/30 bg-ink/10 text-ink hover:bg-ink/20",
                          isPlaying && "border-ink bg-ink text-surface",
                          !hasPreview && "cursor-default",
                        )}
                        disabled={!hasPreview || replacing}
                        aria-label={hasPreview ? (isPlaying ? "Stop preview" : "Play preview") : undefined}
                        title={hasPreview ? (isPlaying ? "Stop preview" : "Play preview") : "No preview"}
                        onClick={(e) => { e.stopPropagation(); togglePreview(v); }}
                      >
                        {/* A provider with previews shows a play icon (not a profile pic);
                            providers without previews fall back to an avatar image or a letter. */}
                        {hasPreview ? (
                          <span className="flex h-4 w-4 items-center justify-center" aria-hidden>
                            {isPlaying ? (
                              <svg viewBox="0 0 12 12"><rect x="3" y="2.5" width="2.2" height="7" rx="0.6" fill="currentColor" /><rect x="6.8" y="2.5" width="2.2" height="7" rx="0.6" fill="currentColor" /></svg>
                            ) : (
                              <svg viewBox="0 0 12 12"><path d="M3.5 2.5l5.5 3.5-5.5 3.5z" fill="currentColor" /></svg>
                            )}
                          </span>
                        ) : v.image_url ? (
                          <img src={v.image_url} alt="" loading="lazy" className="h-full w-full object-cover" />
                        ) : (
                          <span className="text-[14px] font-semibold leading-none">{(v.name || v.id).charAt(0).toUpperCase()}</span>
                        )}
                      </button>

                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[13.5px] font-medium text-ink">{v.name || v.id}</div>
                        {v.description && <div className="truncate text-[12.5px] text-muted">{v.description}</div>}
                      </div>

                      <div className="flex flex-none items-center gap-2">
                        {(v.language || flag) && (
                          <span className="inline-flex items-center gap-1 text-[12px] text-muted">
                            {flag && <span aria-hidden>{flag}</span>}
                            {v.language && <span>{langName(v.language)}</span>}
                          </span>
                        )}
                        {v.tier && (
                          <span className="inline-flex items-center rounded-full border border-line bg-subtle px-2 py-0.5 text-[11px] font-medium text-muted">
                            {v.tier}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })
              )}
              {loading && !replacing && <div className="px-3.5 py-2.5 text-[13px] text-muted">Loading…</div>}
              {/* Only over rows worth dimming — with an empty list the skeletons
                  above are the loading state, and this would cover nothing. */}
              {replacing && voices.length > 0 && (
                <div className="absolute inset-0 z-10 grid min-h-[140px] place-items-center bg-surface/75 backdrop-blur-[1px]">
                  <div className="flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-2 text-[12.5px] font-medium text-ink-soft shadow-rest">
                    <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-line-strong border-t-ink" aria-hidden />
                    <span>Loading voices...</span>
                  </div>
                </div>
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
