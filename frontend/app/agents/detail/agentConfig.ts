import type { AgentConfig, AvatarCatalogEntryResponse, CatalogResponse, CostEstimateUsagePerMinute, LLMCatalogEntryResponse, LLMModelSpec, LLMSpec, RealtimeCatalogEntryResponse, RealtimeSpec, STTCatalogEntryResponse, STTModelSpec, STTSpec, TTSCatalogEntryResponse, TTSModelSpec, TTSSpec, CatalogEntry } from "@talqing/sdk";

/* Pure config logic for the agent editor: keeping a draft consistent with the
   catalog as it is edited, and estimating cost. No React, no state, no I/O —
   so the editor component is left with layout and event wiring only. */

export type Channel = NonNullable<AgentConfig["channel"]>;

/** Agent language "unset" — Auto is a real choice, so it needs a value the
    picker can carry. null is what actually goes to the server. */
export const AUTO_LANGUAGE = "__auto";

/** Catalog codes that mean "no specific language" — a provider's own way of
    spelling Auto, so they never become the agent's language. */
export const AUTO_LANGUAGE_VALUES = new Set(["auto", "unknown", "multilingual", "multi"]);

/** What a null `max_steps` runs as — `DEFAULT_MAX_STEPS` in
    backend/services/agents/models.py, which the API field description quotes. */
export const DEFAULT_MAX_STEPS: Record<Channel, number> = { voice: 4, video: 4, text: 25 };

/** The catalog kinds that carry languages, and so can be resolved against. */
export type LanguageAwareEntry = STTCatalogEntryResponse | TTSCatalogEntryResponse | RealtimeCatalogEntryResponse;

/* ── the blocks the forms bind to ────────────────────────────────────────────
   Every config the dashboard reads — the draft, a published version, a call's
   resolved config — comes back through the backend's `AgentConfig` with every
   default filled in, so defaults live in Python and nowhere else. The generated
   types still mark those fields optional only because one schema serves both
   the request and the response; these name the blocks as the server sends them. */

/** What ends the caller's turn. Never stored and never chosen — `resolveTurnDetection`
 *  derives it exactly as the server does, so the editor can say which one this
 *  agent got instead of leaving it invisible. */
export type TurnDetection = "stt" | "vad" | "livekit-turn-detector-v1-mini";

export type TurnHandling = {
  endpointing: { min_silence_duration: number; max_silence_duration: number };
  interruption: {
    enabled: boolean;
    discard_audio_if_uninterruptible: boolean;
    min_speech_duration: number;
    min_words: number;
    resume_false_interruption: boolean;
    false_interruption_timeout: number | null;
  };
  preemptive_generation: {
    enabled: boolean;
    preemptive_tts: boolean;
    max_speech_duration: number;
    max_retries: number;
  };
};

export type Silence = { enabled: boolean; timeout: number; max_check_ins: number };

export type NoiseCancellation = {
  enabled: boolean;
  provider: string;
  model: string;
  enhancement_level: number;
};

/** The three levels ai-coustics publishes guidance for. The control is a free
    slider, so these are marks with words attached rather than the only choices —
    but they are the only values anyone has measured. */
export const ENHANCEMENT_LEVEL_MARKS: { value: number; label: string; help: string }[] = [
  { value: 0.5, label: "conservative", help: "The person talking to the agent is never touched." },
  { value: 0.8, label: "balanced", help: "Fewest misheard words on difficult audio." },
  { value: 1.0, label: "aggressive", help: "Suppresses other voices as hard as the model can." },
];

/* ── catalog lookup ─────────────────────────────────────────────────────── */

export type ProviderOption = { value: string; label: string; logoUrl: string };

export function providerDisplayLabel(catalog: CatalogResponse, provider?: string | null): string {
  return (provider && catalog.providers?.[provider]?.label) || provider || "";
}

export function providerOption(catalog: CatalogResponse, provider: string): ProviderOption {
  return {
    value: provider,
    label: providerDisplayLabel(catalog, provider),
    logoUrl: catalog.providers?.[provider]?.logo_url || "",
  };
}

export function catalogEntryLabel(entry?: CatalogEntry | null): string {
  return entry?.label || entry?.model || "";
}

export function providerModelLabel(catalog: CatalogResponse, entry?: CatalogEntry | null): string {
  if (!entry) return "Not configured";
  return `${providerDisplayLabel(catalog, entry.provider)} · ${catalogEntryLabel(entry)}`;
}

/** Whether the model this config runs can be sent an image.
 *
 *  There is no agent setting for image input: it is a property of the model,
 *  and `vision` on its catalog entry is where that is stated. Realtime first,
 *  because a realtime agent has no separate LLM — and both grok-voice entries
 *  are the reason this is a served flag rather than an assumption. */
export function modelReadsImages(catalog: CatalogResponse, config: AgentConfig): boolean {
  const spec = config.realtime ?? config.llm;
  if (!spec) return false;
  const entries: CatalogEntry[] = config.realtime ? (catalog.realtime ?? []) : catalog.llm;
  const entry = entries.find((e) => e.provider === spec.provider && e.model === spec.model);
  return Boolean((entry as LLMCatalogEntryResponse | RealtimeCatalogEntryResponse | undefined)?.vision);
}

/** The model kinds a provider can have searched rather than listed. */
export type SearchedKind = "llm" | "stt";

/** Whether this provider's models of `kind` are searched rather than listed.
 *
 *  `GET /catalog` carries no entries at all for such a provider — there are
 *  hundreds and they move weekly — so `catalog.llm` / `catalog.stt` is not the
 *  complete set of legal models, and anything that would "correct" a selection
 *  by failing to find it there has to stop and check this first.
 *  `useSearchedModels` puts the entries this agent actually names back into the
 *  catalog, so a lookup that still misses means the model is genuinely gone or
 *  not yet fetched — and in neither case is silently swapping it for another
 *  model the right answer. */
export function isSearchedProvider(
  catalog: CatalogResponse,
  provider: string | null | undefined,
  kind: SearchedKind,
): boolean {
  return catalog.providers?.[provider ?? ""]?.searched_kinds.includes(kind) ?? false;
}

/** An entry with no explicit channel list is available on every channel. */
export function supportsChannel(entry: CatalogEntry | undefined, channel: Channel): boolean {
  const channels = entry?.channel;
  return !Array.isArray(channels) || channels.length === 0 || channels.includes(channel);
}

/** Distinct providers, in catalog order, for a provider <Select>.
 *
 *  `extra` names providers that belong on the list without contributing an
 *  entry to it — a searched provider has no entries here at all, and one that
 *  cannot be selected cannot be used. */
export function providerOptionsFor(
  catalog: CatalogResponse,
  entries: CatalogEntry[],
  extra: string[] = [],
): ProviderOption[] {
  const seen = new Set<string>();
  const out: ProviderOption[] = [];
  for (const provider of [...entries.map((e) => e.provider), ...extra]) {
    if (seen.has(provider)) continue;
    seen.add(provider);
    out.push(providerOption(catalog, provider));
  }
  return out;
}

/** Enabled providers whose models of `kind` are searched rather than listed. */
export function searchedProviders(catalog: CatalogResponse, kind: SearchedKind): string[] {
  return Object.entries(catalog.providers ?? {})
    .filter(([, meta]) => meta.enabled && meta.searched_kinds.includes(kind))
    .map(([provider]) => provider);
}

/** Humanize Anam facet codes for gallery dropdowns (animated_3d → Animated 3D). */
export function avatarFacetLabel(value: string): string {
  return value
    .split(/[_-]/)
    .map((part) => (part === "3d" ? "3D" : part.charAt(0).toUpperCase() + part.slice(1)))
    .join(" ");
}

export function avatarModelEntries(catalog: CatalogResponse): AvatarCatalogEntryResponse[] {
  return (catalog.avatar ?? []).filter((entry) => supportsChannel(entry, "video"));
}

export function avatarEntryFor(
  catalog: CatalogResponse,
  config: AgentConfig,
): AvatarCatalogEntryResponse | null {
  const provider = config.avatar?.provider || "anam";
  const entries = avatarModelEntries(catalog);
  const forProvider = entries.filter((entry) => entry.provider === provider);
  const model = config.avatar?.model;
  if (model) {
    const exact = forProvider.find((entry) => entry.model === model);
    if (exact) return exact;
  }
  return forProvider[0] || entries[0] || null;
}

/* ── pipeline ───────────────────────────────────────────────────────────────
   A voice agent runs one of two pipelines, and `config.realtime` is what says
   which — exactly as the server models it. One speech-to-speech model in place
   of three, or the stt → llm → tts cascade. */

export type Pipeline = "cascade" | "realtime";

export function pipelineOf(config: AgentConfig): Pipeline {
  return config.realtime ? "realtime" : "cascade";
}

export function realtimeModelEntries(
  catalog: CatalogResponse,
  channel: Channel,
): RealtimeCatalogEntryResponse[] {
  return entriesForChannel(catalog.realtime ?? [], channel);
}

export function realtimeEntryFor(
  catalog: CatalogResponse,
  config: AgentConfig,
): RealtimeCatalogEntryResponse | null {
  if (!config.realtime) return null;
  return (
    (catalog.realtime ?? []).find(
      (e) => e.provider === config.realtime!.provider && e.model === config.realtime!.model,
    ) || null
  );
}

/** Re-point a realtime spec at a catalog entry. `preserveVoice` mirrors
 *  ttsSpecForEntry: keep the user's pick when normalizing, adopt the entry's
 *  default when they switch model. */
export function realtimeSpecForEntry(
  entry: RealtimeCatalogEntryResponse,
  current?: RealtimeSpec | null,
  preserveVoice = true,
): RealtimeSpec {
  const sameModel = current?.provider === entry.provider && current?.model === entry.model;
  const keepVoice = Boolean(preserveVoice && sameModel && current?.voice);
  const speed = typeof current?.speed === "number" ? current.speed : 1.0;
  return {
    ...(current || {}),
    provider: entry.provider,
    model: entry.model,
    voice: keepVoice ? current!.voice : entry.default_voice || null,
    voice_name: keepVoice ? current?.voice_name || null : null,
    speed: entry.supports_speed
      ? Math.max(entry.speed_min ?? 0.5, Math.min(entry.speed_max ?? 2.0, speed))
      : 1.0,
  };
}

/** Switch a draft between the two pipelines, restoring catalog defaults for
 *  whichever side is being turned on. Normalization fills in the models. */
export function withPipeline(
  catalog: CatalogResponse,
  config: AgentConfig,
  pipeline: Pipeline,
): AgentConfig {
  if (pipeline === pipelineOf(config)) return config;
  if (pipeline === "cascade") {
    return normalizeCatalogSelections(catalog, { ...config, realtime: null });
  }
  const entry = realtimeModelEntries(catalog, config.channel || "voice")[0];
  if (!entry) return config;
  return normalizeCatalogSelections(catalog, {
    ...config,
    realtime: realtimeSpecForEntry(entry, null, false),
    // A speech-to-speech model detects turns inside the provider's own socket,
    // so there is no moment at which it could be handed the newest frame — the
    // server refuses the pair outright. Cleared here, where the author can see
    // it happen in the publish diff, rather than left to fail on Save with a
    // control that is no longer on screen to fix.
    vision_input: { screenshare: { enabled: false, record: false } },
    // Same reasoning: the model's own server detects interruptions, so a
    // protected greeting is refused on this pipeline.
    greeting_interruptible: true,
  });
}

/* ── language ───────────────────────────────────────────────────────────── */

/** Mirrors `services.catalog.languages.resolve_entry_language`: the provider's
 *  own code for the agent's language, or null when that model auto-detects.
 *  Kept in step with the backend so the editor can show what each model will
 *  actually receive — the server is still the one that decides.
 *
 *  Exact code, else the bare primary subtag, else the single regional variant of
 *  it. Several variants and no matching default is unresolved, never a guess. */
export function resolveEntryLanguage(
  agentLanguage: string | null | undefined,
  entry: LanguageAwareEntry | null | undefined,
): string | null {
  const wanted = `${agentLanguage || ""}`.trim().toLowerCase();
  if (!wanted || AUTO_LANGUAGE_VALUES.has(wanted) || !entry) return null;

  const codes = new Map<string, string>();
  for (const option of entry.languages ?? []) {
    const code = `${option.code || ""}`.trim().toLowerCase();
    if (code && !AUTO_LANGUAGE_VALUES.has(code)) codes.set(code, option.code);
  }
  const exact = codes.get(wanted);
  if (exact) return exact;

  const primary = wanted.split("-", 1)[0];
  const widened = codes.get(primary);
  if (widened) return widened;

  const variants = [...codes].filter(([code]) => code.split("-", 1)[0] === primary);
  if (variants.length === 1) return variants[0][1];

  const fallback = `${entry.default_language || ""}`.trim().toLowerCase();
  if (fallback && fallback.split("-", 1)[0] === primary) return codes.get(fallback) ?? null;
  return null;
}

/** Re-point a TTS spec at a catalog entry.
 *  `preserveVoice` is the difference between normalizing before save (keep what
 *  the user picked) and switching provider/model (adopt the entry's default). */
export function ttsSpecForEntry(
  entry: TTSCatalogEntryResponse,
  current?: TTSModelSpec | null,
  preserveVoice = true,
): TTSSpec {
  const sameProvider = current?.provider === entry.provider;
  const keepVoice = Boolean(preserveVoice && sameProvider && current?.voice);
  const speed = typeof current?.speed === "number" ? current.speed : 1.0;
  return {
    ...(current || {}),
    provider: entry.provider,
    model: entry.model,
    voice: keepVoice ? current!.voice : entry.default_voice || null,
    voice_name: keepVoice ? current?.voice_name || null : null,
    speed: entry.supports_speed
      ? Math.max(entry.speed_min ?? 0.5, Math.min(entry.speed_max ?? 2.0, speed))
      : 1.0,
    // Clamped the way speed is, and for the same reason: the setting only means
    // something on a model that declares it, and one carried onto a model that
    // does not would fail validation at publish rather than at the click that
    // caused it.
    expressive: Boolean(entry.expressive && current?.expressive),
    // Dropped outright when the voice is, because they describe THAT voice and
    // nothing else — carrying one voice's tuning onto another would be worse
    // than having none. Re-read from the gallery on the next pick.
    stability: keepVoice && entry.supports_voice_settings ? (current?.stability ?? null) : null,
    similarity_boost:
      keepVoice && entry.supports_voice_settings ? (current?.similarity_boost ?? null) : null,
  };
}

/* ── keeping a draft valid for its channel ──────────────────────────────── */

export function entriesForChannel<T extends CatalogEntry>(entries: T[], channel: Channel): T[] {
  return entries.filter((entry) => supportsChannel(entry, channel));
}

function pickEntry<T extends CatalogEntry>(
  entries: T[],
  spec: { provider?: string; model?: string } | null | undefined,
  preferredProvider?: string,
): T | null {
  if (!entries.length) return null;
  const exact = spec
    ? entries.find((entry) => entry.provider === spec.provider && entry.model === spec.model)
    : null;
  return (
    exact ||
    (preferredProvider ? entries.find((entry) => entry.provider === preferredProvider) : null) ||
    (spec?.provider ? entries.find((entry) => entry.provider === spec.provider) : null) ||
    entries[0] ||
    null
  );
}

/** The catalog entry a failover selection points at, or null when it no longer
 *  names one this channel supports — unlike the primary it is never re-pointed
 *  at some other provider, because a silently swapped failover target is worse
 *  than none. A fallback equal to the primary is dropped too: the server rejects
 *  it, and it would fail over to the outage it is meant to survive. */
function fallbackEntry<T extends CatalogEntry>(
  entries: T[],
  channel: Channel,
  primary: { provider?: string; model?: string } | null | undefined,
  fallback: { provider?: string; model?: string } | null | undefined,
): T | null {
  if (!fallback) return null;
  if (fallback.provider === primary?.provider && fallback.model === primary?.model) return null;
  return (
    entriesForChannel(entries, channel).find(
      (entry) => entry.provider === fallback.provider && entry.model === fallback.model,
    ) || null
  );
}

/** The failover half of `reconcileModels`, kept whole when the entry that would
 *  justify dropping it simply has not been fetched (see `isSearchedProvider`). */
function keptLlmFallback(catalog: CatalogResponse, channel: Channel, llm: LLMSpec | null | undefined) {
  const fallback = llm?.fallback;
  if (!fallback) return null;
  const entry = fallbackEntry(catalog.llm, channel, llm, fallback);
  if (!entry) {
    const unresolved =
      isSearchedProvider(catalog, fallback.provider, "llm") &&
      !(fallback.provider === llm?.provider && fallback.model === llm?.model);
    return unresolved ? fallback : null;
  }
  // Still the same model (`fallbackEntry` matches it exactly), so the choices
  // that are only about that model — its lane and its hosts — stand.
  return {
    provider: entry.provider,
    model: entry.model,
    priority: fallback.priority,
    hosts: fallback.hosts,
    builtin_tools: keptBuiltinTools(catalog, { ...fallback, provider: entry.provider, model: entry.model }),
    reasoning_effort: keptReasoningEffort(catalog, {
      ...fallback,
      provider: entry.provider,
      model: entry.model,
    }),
  };
}

/** The speech-to-text failover that survives the primary, kept whole while an
 *  entry that would decide it has not been fetched (see `isSearchedProvider`). */
function keptSttFallback(catalog: CatalogResponse, channel: Channel, stt: STTSpec) {
  const fallback = stt.fallback;
  if (!fallback) return null;
  if (fallback.provider === stt.provider && fallback.model === stt.model) return null;
  const primary = catalog.stt.find((e) => e.provider === stt.provider && e.model === stt.model);
  const entry = fallbackEntry(catalog.stt, channel, stt, fallback);
  const unresolved =
    (!primary && isSearchedProvider(catalog, stt.provider, "stt")) ||
    (!entry && isSearchedProvider(catalog, fallback.provider, "stt"));
  if (unresolved) return fallback;
  // Streaming backs streaming, batch backs batch — the pair shares one VAD for
  // the whole call, and only a batch model depends on it for end-of-turn.
  if (!entry || (entry.streaming === false) !== (primary?.streaming === false)) return null;
  return { provider: entry.provider, model: entry.model };
}

/** The built-in tools that survive the currently selected model.
 *
 *  A tool is only meaningful on a model whose catalog entry declares it, and
 *  the options are per entry too — the same `web_search` takes different fields
 *  at xAI and OpenAI. So a model switch keeps a tool by type and re-checks
 *  every option key against the new entry rather than carrying the old values
 *  across. */
function keptBuiltinTools(catalog: CatalogResponse, llm: LLMModelSpec | null | undefined) {
  const chosen = llm?.builtin_tools ?? [];
  if (!chosen.length) return [];
  const entry = catalog.llm.find((e) => e.provider === llm?.provider && e.model === llm?.model);
  return chosen.flatMap((chosenTool) => {
    const tool = entry?.builtin_tools?.find((t) => t.type === chosenTool.type);
    if (!tool) return [];
    const keys = new Set((tool.options ?? []).map((o) => o.key));
    const config = Object.fromEntries(
      Object.entries(chosenTool.config ?? {}).filter(([key]) => keys.has(key)),
    );
    return [{ type: chosenTool.type, config }];
  });
}

/** The thinking effort that survives the currently selected model, or undefined
 *  to fall back to that model's own default.
 *
 *  The accepted values are per model and do not line up — "minimal" is the floor
 *  on xAI and Gemini and is rejected outright by every GPT-5.6 — so a model
 *  switch can strand a perfectly reasonable choice on a model that has never
 *  heard of it. */
function keptReasoningEffort(catalog: CatalogResponse, llm: LLMModelSpec | null | undefined) {
  const chosen = llm?.reasoning_effort;
  if (!chosen) return undefined;
  const entry = catalog.llm.find((e) => e.provider === llm?.provider && e.model === llm?.model);
  // A searched model whose entry has not arrived is not a model that rejects
  // this setting — dropping the author's choice on a fetch that is still in
  // flight would be an edit nobody made.
  if (!entry && isSearchedProvider(catalog, llm?.provider, "llm")) return chosen;
  return entry?.reasoning_efforts?.includes(chosen) ? chosen : undefined;
}

/** Re-check everything a model choice decides — its failover, the built-in
 *  tools it offers, and how hard it may think — and drop what no longer holds.
 *
 *  This runs on every model edit, not only before save. Retargeting a primary
 *  onto its own fallback, switching the primary between streaming and batch
 *  speech-to-text, or moving to a model that has no web search all invalidate
 *  something the user explicitly chose — reconciling here means they watch it
 *  go, instead of the save step quietly throwing it away behind their back. */
export function reconcileModels(catalog: CatalogResponse, config: AgentConfig): AgentConfig {
  const channel: Channel = config.channel || "voice";
  const next: AgentConfig = { ...config };

  // A realtime pipeline has no failover: LiveKit's adapters wrap an LLM, an STT
  // or a TTS, and a speech-to-speech model is none of those. It has no language
  // model either, so no built-in tools to keep.
  if (next.realtime) return next;

  // Each model keeps only the tools its own entry declares — the fallback's are
  // re-checked against the fallback's entry, never inherited from the primary.
  next.llm = {
    ...next.llm,
    fallback: keptLlmFallback(catalog, channel, next.llm),
    builtin_tools: keptBuiltinTools(catalog, next.llm),
    reasoning_effort: keptReasoningEffort(catalog, next.llm),
  };

  if (next.stt) {
    next.stt = { ...next.stt, fallback: keptSttFallback(catalog, channel, next.stt) };
  }

  if (next.tts) {
    const entry = fallbackEntry(catalog.tts, channel, next.tts, next.tts.fallback);
    const { fallback: _nested, ...spec } = entry
      ? ttsSpecForEntry(entry, next.tts.fallback)
      : ({} as TTSSpec);
    next.tts = { ...next.tts, fallback: entry ? spec : null };
  }
  return next;
}

/** Which detector will end the caller's turn, mirroring
 *  `compiler.factories.resolve_turn_detection`.
 *
 *  A streaming model endpoints for itself. A batch model has none — LiveKit's
 *  StreamAdapter synthesises end-of-speech from the local voice detector — so
 *  the audio end-of-turn model takes over when it knows the language, matched on
 *  the primary subtag so `en-GB` rides on `en`, and plain silence otherwise.
 *
 *  Nothing here changes the config. It exists so the editor can *say* which
 *  detector this agent got, rather than leaving a derived decision invisible. */
export function resolveTurnDetection(
  catalog: CatalogResponse,
  stt: { provider?: string; model?: string } | null | undefined,
  language: string | null | undefined,
): TurnDetection {
  const entry = stt
    ? catalog.stt.find((e) => e.provider === stt.provider && e.model === stt.model)
    : undefined;
  if (entry?.streaming !== false) return "stt";
  const primary = (language || "").split("-", 1)[0].toLowerCase();
  const covered = (catalog.turn_detector_languages ?? []).some(
    (l) => l.code.split("-", 1)[0].toLowerCase() === primary,
  );
  return primary && covered ? "livekit-turn-detector-v1-mini" : "vad";
}

/** Force a config to be internally consistent for its channel: models that the
    channel supports, no STT/TTS on text, no avatar off video. */
export function normalizeCatalogSelections(
  catalog: CatalogResponse,
  config: AgentConfig,
): AgentConfig {
  return reconcileModels(catalog, normalizePrimaries(catalog, config));
}

function normalizePrimaries(catalog: CatalogResponse, config: AgentConfig): AgentConfig {
  const next = { ...config };
  const channel: Channel = next.channel || "voice";
  // A searched provider's models are never re-pointed. `catalog.llm` cannot say
  // whether one of them exists, so "not found" is not evidence of anything, and
  // swapping the agent onto the first entry that IS in the list would rewrite a
  // deliberate choice on page load. Publish validation is what refuses a model
  // the provider no longer has.
  const searchedLlm = isSearchedProvider(catalog, next.llm?.provider, "llm");
  const searchedStt = isSearchedProvider(catalog, next.stt?.provider, "stt");

  // Re-point at a model this channel still offers, but only while the setting is
  // on: the model is kept when the toggle is off so turning it back on restores
  // the choice, and re-pointing a switched-off setting would look like an edit
  // nobody made.
  const nc = next.noise_cancellation as NoiseCancellation;
  if (nc.enabled) {
    const entry = pickEntry(entriesForChannel(catalog.noise_cancellation ?? [], channel), nc);
    next.noise_cancellation = entry
      ? { ...nc, provider: entry.provider, model: entry.model }
      : // Nothing on offer for this channel — the server would reject the
        // publish, so drop it rather than save a model that cannot run.
        { ...nc, enabled: false };
  }

  if (channel === "text") {
    const textLlm = searchedLlm ? null : pickEntry(entriesForChannel(catalog.llm, channel), next.llm);
    return {
      ...next,
      llm: textLlm ? { ...next.llm, provider: textLlm.provider, model: textLlm.model } : next.llm,
      stt: null,
      tts: null,
      realtime: null,
      avatar: null,
      // No caller audio to clean up.
      noise_cancellation: { ...nc, enabled: false },
      // No media stack to speak a language; the prompt sets the tone instead.
      language: null,
    };
  }

  // The face a video agent wears, on either pipeline: the avatar renders from
  // whatever audio the agent publishes, so the pick does not depend on which
  // models produced it. Null off the video channel.
  const avatarFor = (avatar: AgentConfig["avatar"]): AgentConfig["avatar"] => {
    if (channel !== "video") return null;
    const preferred =
      avatarModelEntries(catalog).find((entry) => entry.model === avatar?.model) ||
      avatarModelEntries(catalog)[0];
    if (!preferred) return avatar ?? null;
    return {
      ...avatar,
      provider: preferred.provider || avatar?.provider || "anam",
      model: preferred.model,
    };
  };

  const realtimeEntry = next.realtime
    ? pickEntry(realtimeModelEntries(catalog, channel), next.realtime)
    : null;
  if (realtimeEntry) {
    return {
      ...next,
      realtime: realtimeSpecForEntry(realtimeEntry, next.realtime),
      stt: null,
      tts: null,
      llm: null,
      avatar: avatarFor(next.avatar),
    };
  }
  next.realtime = null;

  const llmEntry = searchedLlm ? null : pickEntry(entriesForChannel(catalog.llm, channel), next.llm);
  if (llmEntry) next.llm = { ...next.llm, provider: llmEntry.provider, model: llmEntry.model };

  const sttEntry = searchedStt ? null : pickEntry(entriesForChannel(catalog.stt, channel), next.stt);
  if (sttEntry) {
    next.stt = { ...next.stt, provider: sttEntry.provider, model: sttEntry.model };
  }

  const ttsEntry = pickEntry(entriesForChannel(catalog.tts, channel), next.tts);
  if (ttsEntry) next.tts = ttsSpecForEntry(ttsEntry, next.tts);

  return { ...next, avatar: avatarFor(next.avatar) };
}

/* ── cost estimate ──────────────────────────────────────────────────────── */

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** A rate, per minute of a call or per answered message of a chat. */
export function moneyPerMin(value: number, unit: "min" | "message" = "min"): string {
  if (!Number.isFinite(value)) return "—";
  if (value === 0) return `$0.000/${unit}`;
  return `$${value < 0.01 ? value.toFixed(4) : value.toFixed(3)}/${unit}`;
}

/** A streaming model bills the open socket, so it charges every second of the
 *  call. A batch model only ever transcribes the caller's own utterances, so it
 *  charges the assumed share of the minute they speak for. */
/** Per-minute STT cost, or NaN when no per-second rate is in hand — a
 *  searched entry still being fetched, or one of the two OpenRouter models priced
 *  per token, where the minute's token count is not something we can assume.
 *  Same reasoning as `estimateLlmPerMinute`: "—" beats a $0.00 someone acts on. */
export function estimateSttPerMinute(
  entry: STTCatalogEntryResponse | undefined | null,
  usage: Partial<CostEstimateUsagePerMinute>,
): number {
  const rate = entry?.pricing?.per_audio_second;
  if (typeof rate !== "number") return NaN;
  const seconds =
    entry?.streaming === false
      ? num(usage.stt_batch_audio_seconds)
      : num(usage.stt_streaming_audio_seconds);
  return seconds * rate;
}

/** Per-minute LLM cost, or NaN when the model's rates are not in hand.
 *
 *  NaN rather than zero, and the caller renders it as "—". A searched provider's
 *  entry is fetched separately from the catalog and can be missing for a beat —
 *  or for good, if the model was withdrawn — and an LLM row reading $0.00/min is
 *  a number a person would act on. */
export function estimateLlmPerMinute(
  entry: LLMCatalogEntryResponse | undefined | null,
  usage: Partial<CostEstimateUsagePerMinute>,
  /** Mirrors billing: the priority lane bills off a second rate block on the
   *  same entry, so the estimate has to read the same one the invoice will. */
  priority = false,
  /** Whether the agent watches the caller's screen. The newest frame rides on
   *  every user turn, which is real input tokens the invoice will show — and
   *  always uncached, because the frame is in the request tail and never in the
   *  stored history the provider caches a prefix of. */
  watchesScreen = false,
): number {
  if (!entry) return NaN;
  const pricing = (priority ? entry.priority?.pricing : entry.pricing) || {};
  const input = num(usage.llm_input_tokens);
  const cached = Math.min(input, num(usage.llm_cached_input_tokens));
  const uncached =
    Math.max(0, input - cached) + (watchesScreen ? num(usage.screenshare_input_tokens) : 0);
  // A provider that does not price cached input bills it at the full rate.
  const cachedRate = pricing.cached_input_per_1m == null
    ? num(pricing.input_per_1m)
    : num(pricing.cached_input_per_1m);
  return (
    (uncached / 1_000_000) * num(pricing.input_per_1m) +
    (cached / 1_000_000) * cachedRate +
    (num(usage.llm_output_tokens) / 1_000_000) * num(pricing.output_per_1m)
  );
}

export function estimateTtsPerMinute(
  entry: TTSCatalogEntryResponse | undefined | null,
  usage: Partial<CostEstimateUsagePerMinute>,
): number {
  const pricing = entry?.pricing || {};
  return (
    num(usage.tts_characters) * num(pricing.per_character) +
    60 * num(pricing.per_audio_second) +
    (num(usage.tts_input_tokens) / 1_000_000) * num(pricing.input_per_1m) +
    (num(usage.tts_output_tokens) / 1_000_000) * num(pricing.output_per_1m)
  );
}

/** One model billing both directions of audio, plus the text it reads and
 *  writes. Some providers meter the audio stream by the minute instead of by
 *  the token, which is a flat per-minute term on top. */
export function estimateRealtimePerMinute(
  entry: RealtimeCatalogEntryResponse | undefined | null,
  usage: Partial<CostEstimateUsagePerMinute>,
): number {
  const pricing = entry?.pricing || {};
  const audioIn = num(usage.realtime_audio_input_tokens);
  const cachedAudioIn = Math.min(audioIn, num(usage.realtime_cached_audio_input_tokens));
  // A provider that does not price cached audio bills it at the full rate.
  const cachedAudioRate =
    pricing.cached_audio_input_per_1m == null
      ? num(pricing.audio_input_per_1m)
      : num(pricing.cached_audio_input_per_1m);
  return (
    ((audioIn - cachedAudioIn) / 1_000_000) * num(pricing.audio_input_per_1m) +
    (cachedAudioIn / 1_000_000) * cachedAudioRate +
    (num(usage.realtime_text_input_tokens) / 1_000_000) * num(pricing.text_input_per_1m) +
    (num(usage.realtime_audio_output_tokens) / 1_000_000) * num(pricing.audio_output_per_1m) +
    (num(usage.realtime_text_output_tokens) / 1_000_000) * num(pricing.text_output_per_1m) +
    num(pricing.per_session_minute)
  );
}

export function estimateAvatarPerMinute(entry?: AvatarCatalogEntryResponse | null): number {
  return num(entry?.pricing?.per_minute);
}
