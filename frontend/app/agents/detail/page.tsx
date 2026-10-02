"use client";
import { useEffect, useMemo, useRef, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import type { AgentConfig, AgentResponse, AgentVersionDetailResponse, AnalysisSpec, CatalogResponse, ConversationSpec, FaqSummary, IntegrationResponse, BackgroundAudioSpec, LLMCatalogEntryResponse, RecordingSpec, STTCatalogEntryResponse, TTSCatalogEntryResponse, TaskResponse, ToolResponse, ToolSelection, CatalogEntry } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { AppShell } from "@/app/components/AppShell";
import { timezones } from "@/lib/date";
import WebCall from "../../components/WebCall";
import WebChat from "../../components/WebChat";
import CopilotChat, { CopilotHeading } from "../../components/CopilotChat";
import { EditorSkeleton } from "../../components/EditorSkeleton";
import { FaqsSection } from "../../components/FaqsSection";
import {
  HelpDot,
  BoxCheckbox,
  NothingToAttach,
  Select,
  Tooltip,
  STAGE_HUE,
  MODEL_HOSTS_GRID,
  STAGE_GRID,
  type CardAccent,
  ValidationModal,
  btn,
  Badge,
  Button,
  Container,
  Panel,
  LIFT_ON_HOVER,
  CardHead,
  SectionCard,
  Input,
  Textarea,
  Label,
  UnsavedChangesModal,
  useToast,
} from "../../components/ui";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { missingKeyProvider } from "@/lib/byok";
import { useUnsavedChanges } from "@/lib/useUnsavedChanges";
import { VoiceModelSelector } from "../../components/VoiceModelSelector";
import { IntegrationLogo } from "../../components/IntegrationLogo";
import { ProviderKeyModal } from "../../components/ProviderKeyModal";
import { VoicePicker, type VoiceSettings } from "../../components/VoicePicker";
import { api, controlApi } from "@/lib/api";
import { AvatarSection } from "./AvatarSection";
import { NewToolModal, toolNameFrom } from "@/app/tools/NewToolModal";
import { CostEstimate } from "./CostEstimate";
import { HostNote, HostSelect, useModelHosts } from "./HostSelect";
import { LanguageSection } from "./LanguageSection";
import { ModelFallback } from "./ModelFallback";
import { SearchedModelSelect, SearchedProviderNote, useSearchedModels } from "./ModelSearch";
import { AnalysisSection } from "./AnalysisSection";
import { ConversationSection } from "./ConversationSection";
import { BuiltinToolsSection } from "./BuiltinToolsSection";
import { ReasoningEffortSelect } from "./ReasoningEffortSelect";
import { PriorityToggle } from "./PriorityToggle";
import { ExpressiveToggle } from "./ExpressiveToggle";
import { KeypadInputSection } from "./KeypadInputSection";
import { VoicemailSection } from "./VoicemailSection";
import { LimitsSection } from "./LimitsSection";
import { VisionInputSection } from "./VisionInputSection";
import { HandoffsSection } from "./HandoffsSection";
import { TasksSection } from "./TasksSection";
import { VariablesSection } from "./VariablesSection";
import { diffAgentConfigs } from "./agentDiff";
import { VersionHistoryModal } from "./VersionHistoryModal";
import { RecordingFields } from "./PrivacySection";
import { GreetingInterruptible, greetingSpeaksNotice } from "./GreetingInterruptible";
import { TuningGroup, TurnHandlingSection } from "./TurnHandlingSection";
import { NoiseCancellationGroup } from "./NoiseCancellationGroup";
import {
  AUTO_LANGUAGE,
  type Channel,
  type NoiseCancellation,
  type ProviderOption,
  type Silence,
  type TurnHandling,
  avatarEntryFor,
  catalogEntryLabel,
  entriesForChannel,
  isSearchedProvider,
  modelReadsImages,
  pipelineOf,
  realtimeModelEntries,
  realtimeSpecForEntry,
  resolveEntryLanguage,
  withPipeline,
  normalizeCatalogSelections,
  providerOptionsFor,
  reconcileModels,
  providerDisplayLabel,
  searchedProviders,
  resolveTurnDetection,
  ttsSpecForEntry,
} from "./agentConfig";

// Section anchors for the editor nav (scroll-to + scroll-spy); sec-avatar only
// renders on video agents (scroll-spy skips missing elements).
const SECTION_IDS = [
  "sec-channel", "sec-prompt", "sec-avatar", "sec-models", "sec-vision", "sec-keypad", "sec-voicemail",
  "sec-tools", "sec-tasks", "sec-handoffs", "sec-integrations", "sec-faqs", "sec-limits",
  "sec-turn-handling", "sec-conversation", "sec-analysis", "sec-test",
] as const;

/* A test run is an agent run: it executes the agent's tools against the
   workspace's own credentials and bills the organization for it. That is an
   editor's capability, and a read-only role should not acquire it as a side
   effect of a test button. */
const READ_ONLY_TEST =
  "Testing an agent runs it and bills this organization for the call — ask an admin for editor access.";

/* The published definition, carrying which agent and which version it answers
   for. */
type LiveDefinition = { agentId: string; version: number; definition: AgentVersionDetailResponse };


/* Channel marks. Stroke-only on a 20px box so they sit on the same optical
   weight as the sidebar's icon set. */
const strokeProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const ICON = {
  voice: (
    <svg className="h-[18px] w-[18px]" {...strokeProps} aria-hidden>
      <path d="M12 3a3.5 3.5 0 0 1 3.5 3.5v4a3.5 3.5 0 0 1-7 0v-4A3.5 3.5 0 0 1 12 3Z" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
    </svg>
  ),
  video: (
    <svg className="h-[18px] w-[18px]" {...strokeProps} aria-hidden>
      <rect x="3" y="5.5" width="12" height="13" rx="3" />
      <path d="m15 11.5 5.5-3.5v8L15 12.5" />
    </svg>
  ),
  text: (
    <svg className="h-[18px] w-[18px]" {...strokeProps} aria-hidden>
      <path d="M4.5 6.5A2.5 2.5 0 0 1 7 4h10a2.5 2.5 0 0 1 2.5 2.5v6A2.5 2.5 0 0 1 17 15h-5l-4.5 4v-4A2.5 2.5 0 0 1 4.5 12.5v-6Z" />
    </svg>
  ),
};

/* The channel is the first choice on the page because it decides which stages
   the agent even has. `note` is what the choice costs or buys further down —
   every channel carries one, so picking a different card never reflows the
   section. */
const CHANNELS = [
  {
    value: "text",
    label: "Text",
    desc: "A chat session, no audio",
    note: "No speech stack. Prompt, tools and hooks all still apply — hooks follow the chat instead of the call.",
  },
  {
    value: "voice",
    label: "Voice",
    desc: "A phone call or a web call",
    note: "Reached on a phone number or through the web call widget — on a full speech stack, or on one realtime model.",
  },
  {
    value: "video",
    label: "Video",
    desc: "A web call with an avatar",
    note: "Video adds an Anam avatar to a voice agent. It lip-syncs to whatever audio the agent produces, so either pipeline works.",
  },
] as const satisfies readonly { value: Channel; label: string; desc: string; note: string }[];

/* One stage of the pipeline, as a full-bleed band of the Models card. The hue is
   the same token the stage has in the cost meter, so the orange in the bar and
   the orange on this heading are the same thing. */
function Stage({
  accent,
  title,
  children,
}: {
  accent: CardAccent;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="-mx-5 border-t border-line px-5 pb-4 pt-4 last:pb-0">
      <h3 className="mb-3 flex items-center gap-2 text-[15px] font-semibold leading-5 text-ink">
        <span aria-hidden className={cn("h-2.5 w-2.5 flex-none rounded-[3px]", STAGE_HUE[accent])} />
        {title}
      </h3>
      {children}
    </section>
  );
}

/* One placeholder the prompt and greeting accept, with what it resolves to on
   hover. The chips ARE the documentation: this used to be a paragraph that named
   all eight in prose, which ran five lines under a two-field form and buried the
   only part anyone wanted — the token itself, to copy. */
function TokenChip({ token, title }: { token: string; title: string }) {
  return (
    <code
      title={title}
      className="cursor-help rounded-md border border-line-2 bg-canvas px-1.5 py-1 font-mono text-[11.5px] leading-4 text-ink-soft"
    >
      {token}
    </code>
  );
}

/** A caveat the selected model carries, from its catalog entry's `note`.
    Rendered as a warning rather than muted helper text because a note only exists
    when the author has to change something the model itself cannot — which in
    practice means the prompt — and grey text under a dropdown is where that goes
    to die. No entry sets one today; this renders nothing until one does. */
function ModelNote({ entry }: { entry?: CatalogEntry | null }) {
  if (!entry?.note) return null;
  return (
    <p className="mt-2.5 rounded-lg border border-warn/25 bg-warn/[0.05] px-3 py-2 text-[12.5px] leading-5 text-ink">
      {entry.note}
    </p>
  );
}

/** Said under the model, when the model this agent runs cannot read an image.
 *
 *  There is no setting to offer here — image input is a property of the model,
 *  not a preference — so the only useful thing to do is say so where the model
 *  is being chosen. Every language model in the catalog reads images; both
 *  speech-to-speech models from xAI do not, and they do not fail loudly: they
 *  accept the image, drop it, and answer about a photo they never saw. */
function VisionNote({ reads, model }: { reads: boolean; model?: string }) {
  if (reads) return null;
  return (
    <p className="mt-3 text-[13px] leading-5 text-muted">
      {model || "This model"} can&rsquo;t read images.
    </p>
  );
}

/** A hook's name and when it fires. Everything else it needs to say is on the
    help dot, because three of these sit side by side and a paragraph each turned
    the row into a wall. */
function HookHead({ title, help }: { title: string; help: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex items-center gap-3">
      <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold leading-5 text-ink">
        {title}
      </h3>
      <HelpDot label={help} className="flex-none" />
    </div>
  );
}

/** Builds a new tool for a hook, beside the picker rather than instead of it —
    the picker still has to be reachable once one published tool exists. */
function HookCreateButton({ hook, onClick }: { hook: string; onClick: () => void }) {
  return (
    <Tooltip label={`Build a new ${hook} tool`}>
      <button
        type="button"
        onClick={onClick}
        aria-label={`Build a new ${hook} tool`}
        className="grid h-10 w-10 flex-none place-items-center rounded-[10px] border border-line-2 bg-white text-ink-soft transition-colors hover:border-line-strong hover:bg-subtle focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
          <path d="M12 5v14M5 12h14" />
        </svg>
      </button>
    </Tooltip>
  );
}

/** Wraps an attachment row that cannot be ticked, so hovering anywhere on it
    explains why. Rows that are fine to tick render untouched — no extra wrapper,
    no extra tab stop. */
function Unattachable({
  reason,
  children,
}: {
  reason: string | null;
  children: React.ReactNode;
}) {
  if (!reason) return <>{children}</>;
  return (
    <Tooltip label={reason} className="block w-full cursor-not-allowed">
      {children}
    </Tooltip>
  );
}

function AgentEditorInner() {
  // Query param, not a path segment: the dashboard ships as a static
  // export, which cannot prerender an unbounded set of ids.
  const id = useSearchParams().get("id") ?? "";
  const [agent, setAgent] = useState<AgentResponse | null>(null);
  const [loadedCatalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [tools, setTools] = useState<ToolResponse[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationResponse[]>([]);
  const [faqs, setFaqs] = useState<FaqSummary[]>([]);
  /* Every agent in the workspace, so a handoff destination can be picked by
     name. Loaded with the rest rather than lazily: the Handoffs card renders on
     first paint like every other, and a picker that fills in late reads broken. */
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  /* Every task in the workspace, for the same reason and on the same terms as
     `agents` above: the Tasks card renders on first paint. */
  const [taskLibrary, setTasks] = useState<TaskResponse[]>([]);
  const [draftConfig, setCfg] = useState<AgentConfig | null>(null);  // the full draft, incl. name
  // Two distinct failures, two distinct surfaces. `loadError` means we never got
  // an agent to edit, so the page has nothing to render but the error. `actionError`
  // is a failed save/publish — the editor and the user's unsaved edits must survive
  // it, so it renders as an inline banner above the form and never replaces it.
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [validationModal, setValidationModal] = useState<{
    title: string;
    /** Overrides the modal's default line, which speaks of publishing. */
    sub?: string;
    errors: string[];
    warnings: string[];
  } | null>(null);
  /* The provider whose missing key blocked the publish. Fixing it here rather
     than sending the author to /byok keeps the draft, the modal and the publish
     they were in the middle of — the key verifies and the publish resumes. */
  const [missingKey, setMissingKey] = useState<{ id: string; label: string } | null>(null);
  const [activeSection, setActiveSection] = useState<string>(SECTION_IDS[0]);
  // A tool the editor is about to create: what to prefill the name with, and how
  // to explain why we are asking. Null when the modal is closed.
  const [newTool, setNewTool] = useState<{ name: string; title: string; sub: string } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  /* Placing a test call now needs the editor role — it runs the agent on the
     workspace's own provider keys — so the button says so instead of letting
     the click come back a 403. */
  const [readOnly, setReadOnly] = useState(false);
  /* The definition calls are actually running — read here rather than when the
     history opens, because the header badge answers "has my draft moved on from
     live?" from it, and that question is on screen the whole time.

     Derived, never assigned: it is whatever `agent.published_version` names, so
     it is fetched from that name and stamped with it. Publishing, rolling back
     and a CoPilot turn all move the name and the read follows on its own — no
     code path has to remember to refresh it, and one that forgot is what used
     to leave the header diffing a fresh publish against the version it had just
     replaced. The stamp is also what keeps agent A's definition from being read
     as agent B's while B's is still in flight. */
  const publishedVersion = agent?.published_version ?? null;
  const [live, setLive] = useState<LiveDefinition | null>(null);
  const toast = useToast();
  useEffect(() => {
    if (!publishedVersion) return;
    let current = true;
    api
      .getAgentVersion(id, publishedVersion)
      .then((definition) => {
        if (current) setLive({ agentId: id, version: publishedVersion, definition });
      })
      .catch((error) => {
        if (current)
          toast({ kind: "err", msg: apiErrorMessage(error, "Could not read the published definition.") });
      });
    return () => {
      current = false;
    };
  }, [id, publishedVersion, toast]);
  /* Anything stamped with another agent or another version answers a question
     the page is no longer asking. */
  const liveVersion =
    live && live.agentId === id && live.version === publishedVersion ? live.definition : null;

  /* Callable, not just an effect: a rollback rewrites the name, config, tools
     and history server-side, so the refresh has to be a reload rather than a
     state patch. */
  async function load(): Promise<void> {
    try {
      const [a, c, t, i, all, tasks, f] = await Promise.all([
        api.getAgent(id), api.catalog(), api.listTools().then((p) => p.items), api.listIntegrations().then((p) => p.items),
        api.listAgents().then((p) => p.items),
        api.listTasks().then((p) => p.items),
        api.listFaqs().then((p) => p.items),
      ]);
      setAgent(a);
      setCfg(a.config);
      setCatalog(c);
      setTools(t);
      setIntegrations(i);
      setFaqs(f);
      setAgents(all);
      setTasks(tasks);
      void controlApi.me().then((ctx) => setReadOnly(ctx.user.role === "VIEWER"));
    } catch (error) {
      setLoadError(apiErrorMessage(error, "Could not load this agent."));
    }
  }

  useEffect(() => {
    void load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps -- one load per agent id

  // live sync: the embedded AgentCoPilot calls this the moment one of its
  // actions lands (and at turn end) — no background polling. The form ALWAYS
  // reflects the latest server draft: if the server copy moved, we adopt it
  // (the CoPilot is the other editor of the same draft).
  const liveRef = useRef<{ agent: AgentResponse | null }>({ agent: null });
  liveRef.current = { agent };
  const channelGroup = useRef<HTMLDivElement>(null);
  async function refreshFromServer() {
    const cur = liveRef.current;
    if (!cur.agent) return;
    try {
      const [a, tl, il, al, tk, fl] = await Promise.all([
        api.getAgent(id),
        api.listTools().then((p) => p.items),
        api.listIntegrations().then((p) => p.items),
        api.listAgents().then((p) => p.items),
        api.listTasks().then((p) => p.items),
        api.listFaqs().then((p) => p.items),
      ]);

      setTools(tl);
      setIntegrations(il);
      setFaqs(fl);
      setAgents(al);
      setTasks(tk);
      // publish bumps published_version WITHOUT touching updated_at, so
      // compare both — otherwise a CoPilot publish leaves a stale DRAFT badge
      if (
        a.updated_at !== cur.agent.updated_at ||
        a.published_version !== cur.agent.published_version
      ) {
        setAgent(a);
        setCfg(a.config);
      }
    } catch {}
  }

  // scroll-spy: the section nav follows manual scrolling. Active = the last
  // section whose top has passed under the sticky bars; pinned to the last
  // section once the page is scrolled to the bottom.
  useEffect(() => {
    function onScroll() {
      let current: string = SECTION_IDS[0];
      for (const s of SECTION_IDS) {
        const el = document.getElementById(s);
        if (el && el.getBoundingClientRect().top <= 130) current = s;
      }
      // at the very bottom, the last section wins even if its top never
      // reaches the sticky bars — but only once the page actually scrolls
      const scrollable = document.body.scrollHeight > window.innerHeight + 60;
      if (scrollable && window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
        current = SECTION_IDS[SECTION_IDS.length - 1];
      }
      setActiveSection((a) => (a === current ? a : current));
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    onScroll();
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, [agent]);

  function jumpTo(sid: string) {
    setActiveSection(sid);
    document.getElementById(sid)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Computed before the early returns below, because the guard it feeds is a
  // hook and hooks cannot live after a conditional return.
  const dirty =
    !!agent && !!draftConfig && JSON.stringify(draftConfig) !== JSON.stringify(agent.config);
  const unsaved = useUnsavedChanges(dirty);

  /* Names, so the diff never prints a bare uuid. `null` from `toolNameById`
     means the tool was deleted after a version pinned it — which the diff says
     out loud rather than hiding. */
  const toolNameById = (toolId: string): string | null =>
    tools.find((t) => t.id === toolId)?.name ?? null;
  const integrationNameById = (integrationId: string): string =>
    integrations.find((i) => i.id === integrationId)?.display_name ?? integrationId;
  const faqNameById = (faqId: string): string => faqs.find((f) => f.id === faqId)?.name ?? faqId;

  /* Against the live version, not against the last save — and by content, so
     editing a field and editing it back reads as no change. This is what answers
     "do I need to republish?". */
  const liveDiff = useMemo(
    () =>
      liveVersion && draftConfig
        ? diffAgentConfigs(liveVersion.config, draftConfig, {
            tool: (toolId) => tools.find((t) => t.id === toolId)?.name ?? null,
            integration: (integrationId) =>
              integrations.find((i) => i.id === integrationId)?.display_name ?? integrationId,
            faq: (faqId) => faqs.find((f) => f.id === faqId)?.name ?? faqId,
          })
        : null,
    [liveVersion, draftConfig, tools, integrations, faqs],
  );

  /* A provider whose models are searched rather than listed contributes nothing
     to `GET /catalog`, so the entries this agent names are fetched separately
     and merged back in. Every lookup below then works unchanged. */
  const { catalog: resolvedCatalog, remember: rememberModel } = useSearchedModels(
    loadedCatalog,
    draftConfig,
  );
  // Each OpenRouter model's hosts, fetched once per model and shared by its host
  // picker and — for the primary — the cost estimate.
  const llmHosts = useModelHosts(resolvedCatalog, draftConfig?.llm);
  const llmFallbackHosts = useModelHosts(resolvedCatalog, draftConfig?.llm?.fallback);

  // Only a failed *load* can replace the page — there is no agent to edit. A
  // failed save must never get here (see actionError), or it would take the
  // user's unsaved edits down with the editor.
  if (loadError) return (
    <AppShell>
      <Container>
        <div className="mt-6 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
          {loadError}
        </div>
      </Container>
    </AppShell>
  );
  if (!agent || !draftConfig || !resolvedCatalog) return <EditorSkeleton agent />;

  // Re-bound after the guards above: narrowing on the state variables does not
  // survive into the event handlers below, and every one of them needs it.
  const catalog: CatalogResponse = resolvedCatalog;
  const cfg: AgentConfig = draftConfig;

  const channel: Channel = cfg.channel || "voice";
  const isTextChannel = channel === "text";
  const isVideoChannel = channel === "video";
  // Text agents have no speech stack, but the pickers still render the voice
  // catalog so switching back to voice does not start from nothing.
  const pickerChannel: Channel = isTextChannel ? "voice" : channel;
  const turnHandling = cfg.turn_handling as TurnHandling;

  // Named separately rather than as one run of six chips: the two halves have
  // different rules — a call field is empty off a phone call, a clock field needs
  // the timezone above — and the hint reads as the sentence that says so.
  const callFields = channel === "voice" ? (catalog.system_vars ?? []).filter((f) => f.voice_only) : [];
  const clockFields = (catalog.system_vars ?? []).filter((f) => !f.voice_only);

  const sttEntries = entriesForChannel(catalog.stt, pickerChannel);
  const ttsEntries = entriesForChannel(catalog.tts, pickerChannel);
  const llmEntries = entriesForChannel(catalog.llm, channel);
  // One speech-to-speech model in place of all three. Offered on voice and on
  // video — the avatar lip-syncs to the audio the agent publishes, and a
  // realtime model publishes audio exactly as a text-to-speech model does.
  const realtimeEntries = realtimeModelEntries(catalog, channel);
  const isRealtime = pipelineOf(cfg) === "realtime";
  const canPickPipeline = channel !== "text" && realtimeEntries.length > 0;
  const realtimeEntry = cfg.realtime
    ? realtimeEntries.find(
        (e) => e.provider === cfg.realtime!.provider && e.model === cfg.realtime!.model,
      )
    : undefined;
  const realtimeProviders: ProviderOption[] = providerOptionsFor(catalog, realtimeEntries);
  const realtimeModels = cfg.realtime
    ? realtimeEntries.filter((e) => e.provider === cfg.realtime!.provider)
    : [];

  // What the form shows before the draft has ever named a provider: the first
  // entry the channel supports, so the pickers are never blank.
  const defaultStt = sttEntries[0];
  const defaultTts = ttsEntries[0];
  const activeStt = cfg.stt || (defaultStt ? {
    provider: defaultStt.provider,
    model: defaultStt.model,
  } : null);
  const activeTts = cfg.tts || (defaultTts ? {
    provider: defaultTts.provider,
    model: defaultTts.model,
    voice: defaultTts.default_voice || null,
    voice_name: null,
    speed: 1.0,
  } : null);

  const sttEntry = activeStt
    ? sttEntries.find((e) => e.provider === activeStt.provider && e.model === activeStt.model)
    : undefined;
  const ttsEntry = activeTts
    ? ttsEntries.find((e) => e.provider === activeTts.provider && e.model === activeTts.model)
    : undefined;
  const avatarEntry = avatarEntryFor(catalog, cfg);

  const ttsSupportsSpeed = Boolean(ttsEntry?.supports_speed);

  // Derived from the entries everywhere else, but a searched provider has none
  // in the catalog until one of its models is picked — and a provider you cannot
  // select is a provider you cannot use.
  const sttProviders: ProviderOption[] = providerOptionsFor(catalog, sttEntries, searchedProviders(catalog, "stt"));
  const llmProviders: ProviderOption[] = providerOptionsFor(catalog, llmEntries, searchedProviders(catalog, "llm"));
  const sttSearched = isSearchedProvider(catalog, activeStt?.provider, "stt");
  // OpenRouter publishes no per-model language list, and a code a speech model
  // does not really know comes back as the WRONG language rather than an error —
  // so its models are never sent one and always detect. Said beside the setting
  // it qualifies, once a language is actually chosen.
  const autoDetectingStt = [activeStt, cfg.stt?.fallback].find((spec) =>
    isSearchedProvider(catalog, spec?.provider, "stt"),
  );
  const sttLanguageNote =
    !isRealtime && cfg.language && autoDetectingStt
      ? `${providerDisplayLabel(catalog, autoDetectingStt.provider)} speech-to-text detects the language itself and is not sent this one. It still applies to text-to-speech and turn detection.`
      : null;
  const ttsProviders: ProviderOption[] = providerOptionsFor(catalog, ttsEntries);
  const sttModels = activeStt ? sttEntries.filter((e) => e.provider === activeStt.provider) : [];
  const llmModels = llmEntries.filter((e) => e.provider === cfg.llm?.provider);
  // The catalog entries behind the two chosen language models. Both the built-in
  // tools and the thinking settings on offer come from these and nowhere else.
  const llmEntry = llmEntries.find(
    (e) => e.provider === cfg.llm?.provider && e.model === cfg.llm?.model,
  );
  const llmFallbackEntry = llmEntries.find(
    (e) => e.provider === cfg.llm?.fallback?.provider && e.model === cfg.llm?.fallback?.model,
  );
  // What a provider tool costs an agent: it runs before the agent replies, and
  // on a call that is silence the caller is listening to. Said once, for the
  // primary's section and the failover's.
  const builtinToolsNote =
    channel !== "text"
      ? "It runs before the agent replies, so the caller hears a few seconds of silence — tell the agent in its prompt to say it is looking something up."
      : undefined;
  const ttsModels = activeTts ? ttsEntries.filter((e) => e.provider === activeTts.provider) : [];

  // A model cannot fail over to itself, so the primary is off its own list.
  const notPrimary = <T extends { provider: string; model: string }>(
    entries: T[],
    primary: { provider?: string; model?: string } | null | undefined,
  ) => entries.filter((e) => e.provider !== primary?.provider || e.model !== primary?.model);
  // A failover model has to be the same kind as the one it backs. One VAD
  // serves both for the whole call and only a batch model leans on it for
  // end-of-turn, so a mixed pair would tune it for a model that may never run.
  const sttFallbackEntries = notPrimary(sttEntries, activeStt).filter(
    (e) => (e.streaming === false) === (sttEntry?.streaming === false),
  );
  // A searched provider's speech models are all batch — OpenRouter has no
  // streaming transcription — so it can back a batch primary and nothing else.
  const sttFallbackSearched = sttEntry?.streaming === false ? searchedProviders(catalog, "stt") : [];
  const ttsFallbackEntry = cfg.tts?.fallback
    ? ttsEntries.find(
        (e) => e.provider === cfg.tts!.fallback!.provider && e.model === cfg.tts!.fallback!.model,
      )
    : undefined;
  // While expressive delivery is on, the failover voice has to speak delivery
  // tags too: the dialect is taught once from the primary and the agent keeps
  // writing tags after a failover, so a plain-text voice would read them out.
  // Narrowing the list is how the author never has to be told that after the fact.
  const ttsFallbackEntries = notPrimary(ttsEntries, activeTts).filter(
    (e) => !cfg.tts?.expressive || Boolean(e.expressive),
  );
  // Derived, never chosen — the server derives the same thing from the same two
  // inputs. Shown so the numbers below have a stated context.
  const turnDetection = resolveTurnDetection(catalog, cfg.stt, cfg.language);

  // Whether the model the draft selects can read an image — noted under the
  // model so the author learns it here rather than from a caller, and what the
  // test panel below runs.
  const draftReadsImages = modelReadsImages(catalog, cfg);

  /** Every edit funnels through here so `cfg` is never null inside a handler. */
  function editConfig(update: (config: AgentConfig) => AgentConfig) {
    setCfg((current) => (current ? update(current) : current));
  }

  /** Model edits can invalidate a failover selection — a primary retargeted onto
      its own fallback, a streaming/batch flip on speech-to-text — and a batch
      speech-to-text model cannot do speech-to-text endpointing at all. Each edit
      reconciles immediately rather than letting save discard or refuse the
      user's choice unannounced. */
  function editModels(update: (config: AgentConfig) => AgentConfig) {
    editConfig((c) => reconcileModels(catalog, update(c)));
  }

  function setLanguage(language: string) {
    editConfig((c) => ({ ...c, language: language === AUTO_LANGUAGE ? null : language }));
  }

  function pickLlmProvider(provider: string) {
    if (isSearchedProvider(catalog, provider, "llm")) {
      // Nothing to preselect: this provider's models are not in the catalog, and
      // the first page of the picker is one keystroke away. An empty model is
      // refused at publish and the picker says "Select…", which is honest —
      // guessing a model on the author's behalf out of hundreds is not.
      editModels((c) => ({ ...c, llm: { ...c.llm, provider, model: "", hosts: null } }));
      return;
    }
    const entry = llmEntries.find((e) => e.provider === provider);
    if (!entry) return;
    editModels((c) => ({ ...c, llm: { ...c.llm, provider, model: entry.model, hosts: null } }));
  }

  function pickSearchedLlmModel(entry: LLMCatalogEntryResponse) {
    // Remembered before the edit: `reconcileModels` runs on the config that
    // comes back and reads this entry's thinking settings out of the catalog.
    rememberModel("llm", entry);
    // Hosts are reset with the model: a host set means something only for the
    // model it was chosen for.
    editModels((c) => ({
      ...c,
      llm: { ...c.llm, provider: entry.provider, model: entry.model, hosts: null },
    }));
  }

  function pickLlmModel(model: string) {
    if (!llmEntries.some((e) => e.provider === cfg?.llm?.provider && e.model === model)) return;
    editModels((c) => ({ ...c, llm: { ...c.llm, model, hosts: null } }));
  }

  function applyStt(c: AgentConfig, entry: STTCatalogEntryResponse): AgentConfig {
    return { ...c, stt: { ...c.stt, provider: entry.provider, model: entry.model } };
  }

  function pickSttProvider(provider: string) {
    if (isSearchedProvider(catalog, provider, "stt")) {
      // Nothing to preselect, for the reason `pickLlmProvider` gives.
      editModels((c) => ({ ...c, stt: { ...c.stt, provider, model: "" } }));
      return;
    }
    const entry = sttEntries.find((e) => e.provider === provider);
    if (entry) editModels((c) => applyStt(c, entry));
  }

  function pickSearchedSttModel(entry: STTCatalogEntryResponse) {
    // Remembered before the edit: `reconcileModels` reads whether it is batch.
    rememberModel("stt", entry);
    editModels((c) => applyStt(c, entry));
  }

  function pickSttModel(model: string) {
    const entry = sttEntries.find((e) => e.provider === activeStt?.provider && e.model === model);
    if (entry) editModels((c) => applyStt(c, entry));
  }

  function pickTtsProvider(provider: string) {
    const entry = ttsEntries.find((e) => e.provider === provider);
    if (entry) editModels((c) => ({ ...c, tts: ttsSpecForEntry(entry, c.tts, false) }));
  }

  function pickTtsModel(model: string) {
    const entry = ttsEntries.find((e) => e.provider === activeTts?.provider && e.model === model);
    if (entry) editModels((c) => ({ ...c, tts: ttsSpecForEntry(entry, c.tts, false) }));
  }

  /* Fallback pickers. Each rebuilds the failover spec from the chosen entry
     rather than patching the old one, so a switch to another provider cannot
     leave a language or voice behind that the new model does not offer. */

  function pickLlmFallback(entry: LLMCatalogEntryResponse | null) {
    // Remembered before the edit, so `reconcileModels` can read the failover's
    // own thinking settings out of the catalog on the very same pass.
    if (entry) rememberModel("llm", entry);
    editConfig((c) => ({
      ...c,
      llm: { ...c.llm, fallback: entry && { provider: entry.provider, model: entry.model } },
    }));
  }

  function pickSttFallback(entry: STTCatalogEntryResponse | null) {
    if (entry) rememberModel("stt", entry);
    editConfig((c) => ({
      ...c,
      stt: {
        ...c.stt,
        fallback: entry && { provider: entry.provider, model: entry.model },
      },
    }));
  }

  function pickTtsFallback(entry: TTSCatalogEntryResponse | null) {
    editConfig((c) => {
      if (!entry) return { ...c, tts: { ...c.tts, fallback: null } };
      // A brand-new fallback starts from the primary, so settings that carry
      // across providers (speed) match the voice it stands in for; after that it
      // keeps its own. The voice itself never carries — voice ids are
      // provider-specific — so it lands on the catalog default until the user
      // picks one from the gallery below.
      const { fallback: _nested, ...spec } = ttsSpecForEntry(entry, c.tts?.fallback ?? c.tts, false);
      // Expressive is the one setting that must match rather than merely carry:
      // the prompt teaches one dialect for the whole call, so a fallback that
      // disagreed would be handed tagged text it cannot speak.
      return {
        ...c,
        tts: { ...c.tts, fallback: { ...spec, expressive: c.tts?.expressive === true } },
      };
    });
  }

  /** Turning delivery tags on or off, for the primary voice and its failover
   *  together — the two are validated as a pair, so the editor never leaves them
   *  disagreeing. */
  function setExpressive(expressive: boolean) {
    editConfig((c) => ({
      ...c,
      tts: {
        ...(c.tts || activeTts),
        expressive,
        ...(c.tts?.fallback ? { fallback: { ...c.tts.fallback, expressive } } : {}),
      },
    }));
  }

  function setTtsFallbackVoice(
    id: string,
    name: string | null,
    _language: string | null,
    settings: VoiceSettings | null,
  ) {
    editConfig((c) => ({
      ...c,
      tts: {
        ...c.tts,
        fallback: c.tts?.fallback
          ? {
              ...c.tts.fallback,
              voice: id,
              voice_name: name,
              // The failover carries its own voice, so it carries that voice's
              // own tuning too — it is the one that will be speaking.
              stability: settings?.stability ?? null,
              similarity_boost: settings?.similarity_boost ?? null,
            }
          : null,
      },
    }));
  }

  /* Realtime pickers. Each rebuilds the spec from the chosen entry rather than
     patching the old one, so switching model cannot leave behind a voice or a
     language the new one does not offer. */

  function pickRealtimeProvider(provider: string) {
    const entry = realtimeEntries.find((e) => e.provider === provider);
    if (entry) editModels((c) => ({ ...c, realtime: realtimeSpecForEntry(entry, c.realtime, false) }));
  }

  function pickRealtimeModel(model: string) {
    const entry = realtimeEntries.find(
      (e) => e.provider === cfg?.realtime?.provider && e.model === model,
    );
    if (entry) editModels((c) => ({ ...c, realtime: realtimeSpecForEntry(entry, c.realtime, false) }));
  }

  /* Arrow keys walk the channel cards and select as they go, with focus
     following the selection — how a radio group is expected to behave. */
  function moveChannel(e: React.KeyboardEvent, from: number) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const to = (from + step + CHANNELS.length) % CHANNELS.length;
    setChannel(CHANNELS[to].value);
    channelGroup.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]')[to]?.focus();
  }

  function setChannel(next: Channel) {
    editConfig((current) => {
      const c = normalizeCatalogSelections(catalog, { ...current, channel: next });
      // The server refuses both off their channel rather than dropping them, so
      // they are switched off here, where the author sees it in the publish
      // diff, instead of failing on Save behind a control that is gone.
      return {
        ...c,
        keypad_input: next === "voice" ? c.keypad_input : { ...c.keypad_input, enabled: false },
        voicemail_detection:
          next === "voice" ? c.voicemail_detection : { ...c.voicemail_detection, enabled: false },
        vision_input:
          next === "text"
            ? { screenshare: { ...c.vision_input?.screenshare, enabled: false } }
            : c.vision_input,
      };
    });
  }

  /* Both of these leave `tool_version` unset: a draft always tracks each tool's
     latest published version, and publishing is what pins it. */
  function hookRef(toolId: string): ToolSelection | null {
    return toolId ? { tool_id: toolId } : null;
  }

  function toggleTool(toolId: string) {
    editConfig((c) => {
      const current = c.tools ?? [];
      return {
        ...c,
        tools: current.some((ref) => ref.tool_id === toolId)
          ? current.filter((ref) => ref.tool_id !== toolId)
          : [...current, { tool_id: toolId }],
      };
    });
  }

  function toggleIntegration(integrationId: string) {
    editConfig((c) => {
      const current = c.mcps ?? [];
      return {
        ...c,
        mcps: current.some((ref) => ref.integration_id === integrationId)
          ? current.filter((ref) => ref.integration_id !== integrationId)
          : [...current, { integration_id: integrationId }],
      };
    });
  }

  function setBackgroundAudio(patch: Partial<BackgroundAudioSpec>) {
    editConfig((c) => ({ ...c, background_audio: { ...c.background_audio, ...patch } }));
  }

  function setNoiseCancellation(patch: Partial<NoiseCancellation>) {
    editConfig((c) => ({
      ...c,
      noise_cancellation: { ...c.noise_cancellation, ...patch },
    }));
  }

  function setRecording(patch: Partial<RecordingSpec>) {
    editConfig((c) => ({ ...c, recording: { ...c.recording, ...patch } }));
  }

  function setAnalysis(patch: Partial<AnalysisSpec>) {
    editConfig((c) => ({ ...c, analysis: { ...c.analysis, ...patch } }));
  }

  function setConversation(patch: Partial<ConversationSpec>) {
    editConfig((c) => ({ ...c, conversation: { ...c.conversation, ...patch } }));
  }

  function setTurnHandling(patch: Partial<TurnHandling>) {
    editConfig((c) => ({
      ...c,
      turn_handling: { ...c.turn_handling, ...patch },
    }));
  }

  const hookTools = tools.filter((t) => t.published_version);

  /** A plain new tool, named after the agent. */
  function startNewTool() {
    setNewTool({
      name: toolNameFrom(cfg.name || "agent", "tool"),
      title: "Add a tool",
      sub: "Name it now and add what it does on the next screen.",
    });
  }

  /** A tool for one lifecycle hook, named after the agent and the moment it runs. */
  function startHookTool(hook: string, label: string) {
    setNewTool({
      name: toolNameFrom(cfg.name || "agent", hook),
      title: `New ${label.toLowerCase()} tool`,
      sub: "Name it now and add what it does on the next screen, then attach it here.",
    });
  }
  const attachedTools = (cfg.tools || []).length;
  const attachedIntegrations = (cfg.mcps || []).length;

  // Every failed action lands here: banner + toast, editor and edits left intact.
  function reportActionFailure(error: unknown, fallback: string, title: string, sub?: string) {
    const errors = apiErrorList(error);
    if (errors.length) setValidationModal({ title, sub, errors, warnings: [] });
    const message = apiErrorMessage(error, fallback);
    setActionError(message);
    toast({ kind: "err", msg: message });
  }

  /** The draft write Save, Publish and a test all begin with. Returns false when
      it failed, so what follows does not run on a draft that is not stored. */
  async function saveDraft(): Promise<boolean> {
    try {
      const a = await api.updateAgent(id, cfg);
      setAgent(a); setCfg(a.config);
      return true;
    } catch (error) {
      reportActionFailure(
        error,
        "Could not save the draft.",
        "This agent cannot be saved",
        "Fix these issues before saving.",
      );
      return false;
    }
  }

  async function save(): Promise<boolean> {
    setBusy(true); setActionError("");
    try {
      const saved = await saveDraft();
      if (saved) toast({ kind: "ok", msg: "Draft saved." });
      return saved;
    } finally { setBusy(false); }
  }

  /* The test panel runs the stored draft, so unsaved edits are saved first —
     the same save-then-act Publish does. */
  async function saveBeforeTest(): Promise<boolean> {
    return dirty ? save() : true;
  }

  async function finishPublish() {
    setBusy(true); setActionError("");
    try {
      const r = await api.publishAgent(id);
      const a = await api.getAgent(id);
      setAgent(a);
      setCfg(a.config);
      const warningCount = Array.isArray(r.warnings) ? r.warnings.length : 0;
      toast({
        kind: "ok",
        msg: `Published v${r.version}${warningCount ? ` with ${warningCount} warning${warningCount === 1 ? "" : "s"}` : ""} — live for new calls.`,
      });
    } catch (error) {
      reportActionFailure(error, "Could not publish the agent.", "This agent cannot be published");
    } finally { setBusy(false); }
  }

  async function publish() {
    setBusy(true); setActionError("");
    try {
      if (!(await saveDraft())) return;
      const result = await api.validateAgent(id);
      if (result.errors.length || result.warnings.length) {
        setValidationModal({
          title: "Publish checks",
          errors: result.errors,
          warnings: result.warnings,
        });
        return;
      }
      await finishPublish();
    } catch (error) {
      reportActionFailure(error, "Could not publish the agent.", "This agent cannot be published");
    } finally {
      setBusy(false);
    }
  }

  const sectionTabs: [string, string][] = [
    ["sec-channel", "Channel"],
    ["sec-prompt", "Prompt"],
    ...(cfg.channel === "video" ? [["sec-avatar", "Avatar"]] : []),
    ["sec-models", "Models"],
    // Voice and video only: a chat has no call and no tracks, so the server
    // clears this on a text agent.
    ...(cfg.channel !== "text" ? [["sec-vision", "Vision"]] : []),
    // Voice only, and narrower than Vision beside it: the keypad is a phone-line
    // feature, and only a voice agent can hold a phone number or answer a media
    // stream. A video agent is a web call wearing an avatar, so the server
    // clears the setting there too.
    ...(cfg.channel === "voice" ? [["sec-keypad", "Keypad"]] : []),
    // Voice only for the same reason: only a voice agent places calls.
    ...(cfg.channel === "voice" ? [["sec-voicemail", "Voicemail"]] : []),
    ["sec-tools", "Tools"],
    // Tools, then tasks, then handoffs: what it can do, what it can hand out
    // and get back, where it can send the caller.
    ["sec-tasks", "Tasks"],
    ["sec-handoffs", "Handoffs"],
    ["sec-integrations", "Integrations"],
    ["sec-faqs", "FAQs"],
    // Every agent has one: a realtime one hides max steps (LiveKit never checks
    // the cap there) but still bounds the call.
    ["sec-limits", "Limits"],
    ...(cfg.channel !== "text" ? [["sec-turn-handling", "Tuning"]] : []),
    // A chat is to a text agent what a call is to a voice one, so both of these
    // mean the same thing on every channel.
    ["sec-conversation", cfg.channel === "text" ? "Past chats" : "Past calls"],
    ["sec-analysis", "Analysis"],
    ["sec-test", cfg.channel === "text" ? "Test chat" : "Test call"],
  ] as [string, string][];

  return (
    <AppShell>
      <div className="min-h-screen bg-surface text-ink">
      <Container className="min-h-screen">
        {/* Sticky chrome, in two tiers: who this is and what you can do to it,
            then where you are inside it. Both sit on the page's own canvas so
            they read as chrome rather than as the first card. */}
        <div className="sticky top-0 z-30 -mx-6 border-b border-line bg-surface/90 px-6 backdrop-blur-md">
          <div className="flex min-h-[68px] flex-wrap items-center gap-x-3 gap-y-2 pb-2 pt-3">
            <Link
              href="/agents"
              className="-ml-1.5 flex h-8 flex-none items-center gap-1 rounded-lg px-1.5 text-[13px] font-medium text-muted transition-colors hover:bg-hover hover:text-ink"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M14 6l-6 6 6 6" />
              </svg>
              Agents
            </Link>
            <span aria-hidden className="h-4 w-px flex-none bg-line-2" />
            <input
              className="min-w-[200px] flex-1 basis-[260px] rounded-lg border border-transparent bg-transparent px-1.5 py-1 text-[21px] font-semibold leading-7 text-ink transition-colors hover:border-line-2 focus:border-ink focus:outline-none focus:ring-2 focus:ring-ink/10"
              value={cfg.name || ""}
              onChange={(e) => editConfig((c) => ({ ...c, name: e.target.value }))}
              aria-label="Agent name"
            />
            <div className="flex flex-none items-center gap-2">
              {agent.published_version ? (
                <Badge variant="live" dot>v{agent.published_version} live</Badge>
              ) : (
                <Badge>draft</Badge>
              )}
              {dirty && <Badge variant="warn">unsaved</Badge>}
              {/* "unsaved" is the editor against the server draft; this is the
                  server draft against what calls are running. Both can be true,
                  and only this one means "republish". */}
              {liveDiff && (
                <Badge
                  variant={liveDiff.changed ? "warn" : "default"}
                  title={
                    liveDiff.changed
                      ? `Publish to put these changes live. MCP integrations are not part of a version — they apply the moment you save.`
                      : `Calls are running exactly this definition.`
                  }
                >
                  {liveDiff.changed ? `differs from v${agent.published_version}` : `matches v${agent.published_version}`}
                </Badge>
              )}
            </div>
            <div className="ml-auto flex flex-none items-center gap-2">
              <Button
                variant="secondary"
                onClick={() => setHistoryOpen(true)}
                disabled={!agent.published_version}
                title={
                  agent.published_version
                    ? "Compare versions and roll back"
                    : "Nothing published yet — publishing freezes the draft as v1"
                }
              >
                History
              </Button>
              <Button variant="secondary" onClick={() => void save()} disabled={busy}>Save draft</Button>
              <Button onClick={publish} disabled={busy}>
                {agent.published_version ? "Republish" : "Publish"}
              </Button>
            </div>
          </div>

          {/* section nav: scrolls to a section; scroll-spy keeps it in sync.
              The outdent matches a tab's own padding, so the first tab's label
              and its underline start on the same left edge as the back link
              above them and the cards below. */}
          <div className="scroll-thin -mx-2.5 flex gap-0.5 overflow-x-auto">
            {sectionTabs.map(([sid, label]) => (
              <button
                key={sid}
                type="button"
                onClick={() => jumpTo(sid)}
                className={cn(
                  "relative flex-none px-2.5 pb-2.5 pt-1 text-[13px] transition-colors",
                  "after:absolute after:inset-x-2.5 after:bottom-0 after:h-[2px] after:rounded-full after:transition-colors",
                  activeSection === sid
                    ? "font-semibold text-ink after:bg-ink"
                    : "font-medium text-muted after:bg-transparent hover:text-ink-soft",
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {actionError && (
          <div
            role="alert"
            className="mt-3 flex items-start gap-3 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger"
          >
            <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{actionError}</span>
            <button
              type="button"
              aria-label="Dismiss error"
              onClick={() => setActionError("")}
              className="-mr-1 grid h-5 w-5 flex-none place-items-center rounded opacity-70 transition-opacity hover:opacity-100"
            >
              <svg width="10" height="10" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden><path d="M3 3l8 8M11 3l-8 8" /></svg>
            </button>
          </div>
        )}

        <div className="mt-6 grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_clamp(360px,30vw,480px)]">
          {/* ── the chain + the AgentCoPilot ──
              First in the DOM so a narrow screen opens on the summary rather
              than on the top of a very long form; placed explicitly on xl, where
              it pins to the viewport as a single column. */}
          <aside className="flex flex-col gap-4 xl:sticky xl:top-[112px] xl:col-start-2 xl:row-start-1 xl:h-[calc(100vh-136px)]">
            <CostEstimate catalog={catalog} config={cfg} llmHosts={llmHosts} />
            {/* Below xl the rail is not pinned, so flex-1 has no height to grow
                into — it takes a fixed one instead of collapsing to nothing. */}
            <Panel className="flex min-h-0 flex-1 flex-col p-4 max-xl:h-[560px] max-xl:flex-none">
              <CopilotHeading subject="agent" />
              <CopilotChat subject="agent" subjectId={id} subjectName={cfg.name || ""} onChanged={refreshFromServer} />
            </Panel>
          </aside>

          {/* ── config stream ── */}
          <div className="flex flex-col gap-5 xl:col-start-1 xl:row-start-1">
            <SectionCard
              id="sec-channel"
              className={cn("scroll-mt-[112px]", LIFT_ON_HOVER)}
              title="Channel"
              helper="How a person reaches this agent"
            >
              {/* Three cards rather than a dropdown: the channel decides which
                  stages the agent even has, so the two you did not pick are
                  worth seeing. */}
              <div
                ref={channelGroup}
                role="radiogroup"
                aria-label="Channel"
                className="grid gap-2 sm:grid-cols-3"
              >
                {CHANNELS.map(({ value, label, desc }, i) => {
                  const on = channel === value;
                  return (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={on}
                      /* Roving tabindex: the group is one stop, and the arrow
                         keys move within it — the radiogroup contract. */
                      tabIndex={on ? 0 : -1}
                      onKeyDown={(e) => moveChannel(e, i)}
                      onClick={() => setChannel(value)}
                      className={cn(
                        "flex flex-col gap-0.5 rounded-lg border bg-white px-3 py-2.5 text-left transition-colors",
                        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/15",
                        on ? "border-ink shadow-rest" : "border-line-2 hover:border-line-strong hover:bg-subtle/60",
                      )}
                    >
                      <span className="flex items-center gap-2">
                        <span aria-hidden className={cn("flex-none", on ? "text-ink" : "text-faint")}>
                          {ICON[value]}
                        </span>
                        <span className="text-[14px] font-medium leading-5 text-ink">{label}</span>
                        <span className="flex-1" />
                        {on && (
                          <span
                            aria-hidden
                            className="grid h-4 w-4 flex-none place-items-center rounded-full bg-ink text-white"
                          >
                            <svg className="h-2.5 w-2.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <path d="m3.5 8.5 3 3 6-7" />
                            </svg>
                          </span>
                        )}
                      </span>
                      {/* Indented past the mark so the two lines share a left
                          edge — 18px glyph + the gap-2 beside it. */}
                      <span className="pl-[26px] text-[13px] leading-5 text-faint">{desc}</span>
                    </button>
                  );
                })}
              </div>
              <p className="-mx-5 -mb-4 border-t border-line px-5 py-3 text-[13px] leading-5 text-muted">
                {CHANNELS.find((c) => c.value === channel)!.note}
              </p>
            </SectionCard>

            <SectionCard
              id="sec-prompt"
              className={cn("scroll-mt-[112px]", LIFT_ON_HOVER)}
              title={isTextChannel ? "Prompt" : "Prompt & voice"}
              helper={isTextChannel ? "The system prompt" : "The system prompt and opening line"}
            >
              <div className="flex flex-col gap-2">
                <Label htmlFor="agent-prompt">System prompt</Label>
                <Textarea
                  id="agent-prompt"
                  autoGrow
                  className="min-h-[150px]"
                  value={cfg.prompt || ""}
                  onChange={(e) => editConfig((c) => ({ ...c, prompt: e.target.value }))}
                />
              </div>
              {!isTextChannel && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="agent-greeting">Greeting (spoken on connect)</Label>
                  <Input
                    id="agent-greeting"
                    value={cfg.greeting || ""}
                    onChange={(e) => editConfig((c) => ({ ...c, greeting: e.target.value }))}
                  />
                  <p className="text-[13px] leading-5 text-muted">
                    Leave empty to have the agent wait for the caller to speak first.
                  </p>
                  {/* Realtime has no switch to offer: the model's own server
                      cuts it off on the caller's voice, and the server refuses
                      `false` there (`withPipeline` resets it on the way in). */}
                  {cfg.greeting && !isRealtime && (
                    <GreetingInterruptible
                      checked={cfg.greeting_interruptible ?? true}
                      speaksNotice={greetingSpeaksNotice(cfg)}
                      onChange={(greeting_interruptible) =>
                        editConfig((c) => ({ ...c, greeting_interruptible }))
                      }
                    />
                  )}
                </div>
              )}
              {/* Reference, not a field — hence the quieter caption above it than
                  the labels on the two inputs it describes. */}
              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium leading-5 text-muted">
                    {isTextChannel ? "This field accepts" : "Both fields accept"}
                  </span>
                  <HelpDot label="Typed into the prompt or greeting, each of these is filled in when the agent starts. One that isn't there resolves to nothing, and anything else in braces is refused when you save." />
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <TokenChip
                    token="{{userdata.field}}"
                    title="Any field of the session's userdata, filled in when the agent starts."
                  />
                  <TokenChip token="{{vars.name}}" title="One of the variables declared below." />
                  {[...callFields, ...clockFields].map((f) => (
                    <TokenChip
                      key={f.key}
                      token={`{{system_vars.${f.key}}}`}
                      title={`${f.description} e.g. ${f.example}`}
                    />
                  ))}
                </div>
              </div>
              {/* Here rather than beside the language picker: the clock's only
                  visible effect is the three chips above. */}
              <div className="flex flex-col gap-2">
                <Label>Timezone</Label>
                <div className="w-full md:w-[260px]">
                  <Select
                    aria-label="Timezone"
                    searchable
                    value={cfg.timezone ?? ""}
                    onChange={(e) =>
                      editConfig((c) => ({ ...c, timezone: e.target.value || null }))
                    }
                  >
                    <option value="">Not set</option>
                    {timezones(cfg.timezone).map((zone) => (
                      <option key={zone} value={zone}>
                        {zone}
                      </option>
                    ))}
                  </Select>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-[13px] leading-5 text-muted">
                  {clockFields.map((f) => (
                    <TokenChip
                      key={f.key}
                      token={`{{system_vars.${f.key}}}`}
                      title={`${f.description} e.g. ${f.example}`}
                    />
                  ))}
                  <span className="ml-0.5">
                    in the {isTextChannel ? "prompt" : "prompt, greeting"} and tools are given in
                    this timezone.
                  </span>
                </div>
              </div>

              <VariablesSection
                vars={cfg.vars ?? []}
                channel={channel}
                withRequired
                onChange={(vars) => editConfig((c) => ({ ...c, vars }))}
              />
              {!isTextChannel && (
                <RecordingFields
                  recording={cfg.recording ?? {}}
                  greeting={cfg.greeting || ""}
                  onChange={setRecording}
                  onGreetingChange={(greeting: string) => editConfig((c) => ({ ...c, greeting }))}
                />
              )}
            </SectionCard>

            {cfg.channel === "video" && (
              <section id="sec-avatar" className="scroll-mt-[112px]">
                <AvatarSection
                  config={cfg}
                  avatarEntry={avatarEntry}
                  onSelect={(avatarId, name) =>
                    editConfig((c) => ({ ...c, avatar: { ...c.avatar, avatar_id: avatarId, name } }))
                  }
                />
              </section>
            )}

            <section id="sec-models" className="scroll-mt-[112px]">
              <Panel className={LIFT_ON_HOVER}>
                <CardHead title="Models" desc="The speech and reasoning stack" />
                {/* pb-4 answers the pt-4 every Stage band carries, so the first
                    rule has the same air above it as below. Rendered only when
                    it holds something: on text there is no pipeline choice and
                    no language row, and the padding alone would show. */}
                {(canPickPipeline || !isTextChannel) && (
                <div className="flex flex-col gap-3.5 pb-4">
                  {canPickPipeline && (
                    <div className="flex flex-col gap-2">
                      <Label>Pipeline</Label>
                      <div
                        role="radiogroup"
                        aria-label="Pipeline"
                        className="inline-flex w-fit gap-0.5 rounded-[10px] border border-line-2 bg-canvas p-0.5"
                      >
                        {([
                          ["cascade", "Speech-to-text \u2192 LLM \u2192 Text-to-speech"],
                          ["realtime", "Realtime speech-to-speech"],
                        ] as const).map(([value, label]) => (
                          <button
                            key={value}
                            type="button"
                            role="radio"
                            aria-checked={isRealtime === (value === "realtime")}
                            onClick={() => editConfig((c) => withPipeline(catalog, c, value))}
                            className={cn(
                              "rounded-lg px-3 py-1.5 text-[13px] font-medium transition-colors",
                              isRealtime === (value === "realtime")
                                ? "bg-white text-ink shadow-sm"
                                : "text-muted hover:text-ink-soft",
                            )}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                      <p className="text-[13px] leading-5 text-muted">
                        {isRealtime
                          ? "One model hears and speaks directly \u2014 lower latency and more expressive speech, at a higher per-minute cost. It also detects turns itself, so it cannot run the \u201cafter each user turn\u201d hook, watch the caller\u2019s screen, read a script word for word, or fall back to a second provider."
                          : "Three specialist models, one per stage. Each is swappable, every stage leaves a text trail, and any of them can fail over to a backup."}
                      </p>
                    </div>
                  )}

                  {!isTextChannel && (
                    <LanguageSection
                      languages={catalog.languages ?? []}
                      value={cfg.language}
                      onChange={setLanguage}
                      note={sttLanguageNote}
                    />
                  )}
                </div>
                )}

                  {/* One band per stage, in flow order, carrying the same hue the
                      stage has in the cost meter. A band rather than a nested
                      card: these belong to Models, and a box inside a box inside
                      a box is what this section used to be. */}
                  {isRealtime && cfg.realtime && (
                    <Stage accent="realtime" title="Realtime model">
                      <VoiceModelSelector
                        kind="realtime"
                        provider={cfg.realtime.provider ?? ""}
                        model={cfg.realtime.model ?? ""}
                        value={cfg.realtime.voice || null}
                        valueName={cfg.realtime.voice_name || null}
                        onSelect={(id, name) =>
                          editConfig((c) => ({
                            ...c,
                            realtime: { ...c.realtime, voice: id, voice_name: name },
                          }))
                        }
                        speed={cfg.realtime.speed ?? 1.0}
                        supportsSpeed={Boolean(realtimeEntry?.supports_speed)}
                        speedMin={realtimeEntry?.speed_min ?? 0.5}
                        speedMax={realtimeEntry?.speed_max ?? 2.0}
                        onSpeed={(speed) =>
                          editConfig((c) => ({ ...c, realtime: { ...c.realtime, speed } }))
                        }
                        providerOptions={realtimeProviders.map((e) => ({
                          value: e.value,
                          label: e.label,
                          logoUrl: e.logoUrl,
                        }))}
                        modelOptions={realtimeModels.map((entry) => ({
                          value: entry.model,
                          label: entry.label || entry.model,
                        }))}
                        onProvider={pickRealtimeProvider}
                        onModel={pickRealtimeModel}
                      />
                      <p className="mt-3 text-[13px] leading-5 text-muted">
                        Transcripts arrive after the reply rather than alongside it, and a
                        fixed greeting or a &ldquo;say&rdquo; step is delivered as an
                        instruction to say it word for word &mdash; expect the wording to drift.
                        Put anything that has to be exact on the cascade instead.
                      </p>
                      <VisionNote
                        reads={draftReadsImages}
                        model={catalogEntryLabel(realtimeEntry) || undefined}
                      />
                    </Stage>
                  )}

                  {!isRealtime && !isTextChannel && (
                    <Stage accent="stt" title="Speech-to-text">
                      <div className={STAGE_GRID}>
                        <div className="flex min-w-0 flex-col gap-2">
                          <Label>Provider</Label>
                          <Select value={activeStt?.provider || ""} onChange={(e) => pickSttProvider(e.target.value)}>
                            {sttProviders.map((provider) => (
                              <option
                                key={provider.value}
                                value={provider.value}
                                data-logo-url={provider.logoUrl}
                              >
                                {provider.label}
                              </option>
                            ))}
                          </Select>
                        </div>
                        <div className="flex min-w-0 flex-col gap-2">
                          <Label>Model</Label>
                          {sttSearched ? (
                            <SearchedModelSelect
                              kind="stt"
                              entry={sttEntry}
                              value={activeStt?.model}
                              onChange={pickSearchedSttModel}
                              ariaLabel="Speech-to-text model"
                            />
                          ) : (
                            <Select value={activeStt?.model || ""} onChange={(e) => pickSttModel(e.target.value)}>
                              {sttModels.map((entry) => (
                                <option key={entry.model} value={entry.model}>{entry.label || entry.model}</option>
                              ))}
                            </Select>
                          )}
                        </div>
                      </div>
                      {/* A searched provider says this in its own note below. */}
                      {sttEntry?.streaming === false && !sttSearched && (
                        <p className="mt-3 text-[13px] leading-5 text-muted">
                          This is a batch STT model and would have higher latency compared to
                          streaming STT models.
                        </p>
                      )}
                      <SearchedProviderNote catalog={catalog} provider={activeStt?.provider} />
                      <ModelNote entry={sttEntry} />
                      <ModelFallback
                        catalog={catalog}
                        kind="stt"
                        entries={sttFallbackEntries}
                        extraProviders={sttFallbackSearched}
                        searchedEntry={sttEntries.find(
                          (e) => e.provider === cfg.stt?.fallback?.provider && e.model === cfg.stt?.fallback?.model,
                        )}
                        onProvider={(provider) =>
                          editModels((c) => ({ ...c, stt: { ...c.stt, fallback: { provider, model: "" } } }))
                        }
                        value={cfg.stt?.fallback}
                        onChange={(entry) => pickSttFallback(entry as STTCatalogEntryResponse | null)}
                      />
                    </Stage>
                  )}

                  {!isRealtime && (
                  <Stage accent="llm" title="Language model">
                    <div className={isSearchedProvider(catalog, cfg.llm?.provider, "llm") ? MODEL_HOSTS_GRID : STAGE_GRID}>
                      <div className="flex min-w-0 flex-col gap-2">
                        <Label>Provider</Label>
                        <Select value={cfg.llm?.provider ?? ""} onChange={(e) => pickLlmProvider(e.target.value)}>
                          {llmProviders.map((provider) => (
                            <option
                              key={provider.value}
                              value={provider.value}
                              data-logo-url={provider.logoUrl}
                            >
                              {provider.label}
                            </option>
                          ))}
                        </Select>
                      </div>
                      <div className="flex min-w-0 flex-col gap-2">
                        <Label>Model</Label>
                        {isSearchedProvider(catalog, cfg.llm?.provider, "llm") ? (
                          <SearchedModelSelect
                            kind="llm"
                            entry={llmEntry}
                            value={cfg.llm?.model}
                            onChange={pickSearchedLlmModel}
                            ariaLabel="Model"
                          />
                        ) : (
                          <Select value={cfg.llm?.model ?? ""} onChange={(e) => pickLlmModel(e.target.value)}>
                            {llmModels.map((entry) => (
                              <option key={entry.model} value={entry.model}>{catalogEntryLabel(entry)}</option>
                            ))}
                          </Select>
                        )}
                      </div>
                      <ReasoningEffortSelect
                        entry={llmEntry}
                        value={cfg.llm?.reasoning_effort}
                        onChange={(reasoning_effort) =>
                          editConfig((c) => ({ ...c, llm: { ...c.llm, reasoning_effort } }))
                        }
                      />
                      <HostSelect
                        catalog={catalog}
                        spec={cfg.llm}
                        hosts={llmHosts}
                        onChange={(hosts) => editConfig((c) => ({ ...c, llm: { ...c.llm, hosts } }))}
                      />
                    </div>
                    <HostNote
                      value={cfg.llm?.hosts}
                      hosts={llmHosts}
                      subject="the turn"
                      failover={cfg.llm?.fallback ? "present" : "absent"}
                    />
                    <VisionNote
                      reads={draftReadsImages}
                      model={catalogEntryLabel(llmEntry) || undefined}
                    />
                    <PriorityToggle
                      catalog={catalog}
                      entry={llmEntry}
                      value={cfg.llm?.priority}
                      onChange={(priority) =>
                        editConfig((c) => ({ ...c, llm: { ...c.llm, priority } }))
                      }
                    />
                    <BuiltinToolsSection
                      entry={llmEntry}
                      value={cfg.llm?.builtin_tools ?? []}
                      runtimeNote={builtinToolsNote}
                      onChange={(builtin_tools) =>
                        editConfig((c) => ({ ...c, llm: { ...c.llm, builtin_tools } }))
                      }
                    />
                    <ModelFallback
                      catalog={catalog}
                      kind="llm"
                      entries={notPrimary(llmEntries, cfg.llm)}
                      extraProviders={searchedProviders(catalog, "llm")}
                      searchedEntry={llmFallbackEntry}
                      onProvider={(provider) =>
                        editModels((c) => ({
                          ...c,
                          llm: { ...c.llm, fallback: { provider, model: "" } },
                        }))
                      }
                      value={cfg.llm?.fallback}
                      onChange={(entry) => pickLlmFallback(entry as LLMCatalogEntryResponse | null)}
                      footer={
                        /* The failover's own tools and its own lane, against its
                           own catalog entry — the same tool name takes different
                           options at each provider, and the priority lane may not
                           exist there at all, so nothing is copied down from the
                           primary. A failover offering fewer is fine: it keeps
                           the call and loses the search. */
                        <>
                        <HostNote
                          value={cfg.llm?.fallback?.hosts}
                          hosts={llmFallbackHosts}
                          subject="the turn"
                          failover="impossible"
                        />
                        <PriorityToggle
                          catalog={catalog}
                          entry={llmFallbackEntry}
                          value={cfg.llm?.fallback?.priority}
                          onChange={(priority) =>
                            editConfig((c) => ({
                              ...c,
                              llm: c.llm?.fallback
                                ? { ...c.llm, fallback: { ...c.llm.fallback, priority } }
                                : c.llm,
                            }))
                          }
                        />
                        <BuiltinToolsSection
                          entry={llmFallbackEntry}
                          value={cfg.llm?.fallback?.builtin_tools ?? []}
                          runtimeNote={builtinToolsNote}
                          onChange={(builtin_tools) =>
                            editConfig((c) => ({
                              ...c,
                              llm: c.llm?.fallback
                                ? { ...c.llm, fallback: { ...c.llm.fallback, builtin_tools } }
                                : c.llm,
                            }))
                          }
                        />
                        </>
                      }
                    >
                      {/* The failover thinks as hard as its own entry allows —
                          the value is not copied down from the primary, whose
                          model may not offer it at all. */}
                      <ReasoningEffortSelect
                        entry={llmFallbackEntry}
                        value={cfg.llm?.fallback?.reasoning_effort}
                        onChange={(reasoning_effort) =>
                          editConfig((c) => ({
                            ...c,
                            llm: c.llm?.fallback
                              ? { ...c.llm, fallback: { ...c.llm.fallback, reasoning_effort } }
                              : c.llm,
                          }))
                        }
                      />
                      <HostSelect
                        catalog={catalog}
                        spec={cfg.llm?.fallback}
                        hosts={llmFallbackHosts}
                        ariaLabel="Fallback hosts"
                        onChange={(hosts) =>
                          editConfig((c) => ({
                            ...c,
                            llm: c.llm?.fallback
                              ? { ...c.llm, fallback: { ...c.llm.fallback, hosts } }
                              : c.llm,
                          }))
                        }
                      />
                    </ModelFallback>
                  </Stage>
                  )}

                  {!isRealtime && !isTextChannel && activeTts && (
                    <Stage accent="tts" title="Text-to-speech">
                      <VoiceModelSelector
                        provider={activeTts.provider ?? ""}
                        model={activeTts.model ?? ""}
                        value={activeTts.voice || null}
                        valueName={activeTts.voice_name || null}
                        onSelect={(id, name, _language, settings) =>
                          editConfig((c) => ({
                            ...c,
                            tts: {
                              ...(c.tts || activeTts),
                              voice: id,
                              voice_name: name,
                              // The voice's own tuning travels with it. Null on a
                              // model that reads none, which is also what clears
                              // the previous voice's numbers.
                              stability: settings?.stability ?? null,
                              similarity_boost: settings?.similarity_boost ?? null,
                            },
                          }))
                        }
                        withVoiceSettings={Boolean(ttsEntry?.supports_voice_settings)}
                        language={resolveEntryLanguage(cfg.language, ttsEntry)}
                        speed={activeTts.speed ?? 1.0}
                        supportsSpeed={ttsSupportsSpeed}
                        speedMin={ttsEntry?.speed_min ?? 0.5}
                        speedMax={ttsEntry?.speed_max ?? 2.0}
                        onSpeed={(speed) =>
                          editConfig((c) => ({ ...c, tts: { ...(c.tts || activeTts), speed } }))
                        }
                        providerOptions={ttsProviders.map((e) => ({
                          value: e.value,
                          label: e.label,
                          logoUrl: e.logoUrl,
                        }))}
                        modelOptions={ttsModels.map((entry) => ({
                          value: entry.model,
                          label: entry.label || entry.model,
                        }))}
                        onProvider={pickTtsProvider}
                        onModel={pickTtsModel}
                      />
                      <ModelNote entry={ttsEntry} />
                      <ExpressiveToggle
                        entry={ttsEntry}
                        fallbackEntry={ttsFallbackEntry}
                        value={cfg.tts?.expressive}
                        onChange={setExpressive}
                      />
                      <ModelFallback
                        catalog={catalog}
                        kind="tts"
                        entries={ttsFallbackEntries}
                        value={cfg.tts?.fallback}
                        onChange={(entry) => pickTtsFallback(entry as TTSCatalogEntryResponse | null)}
                        footer={
                          cfg.tts?.fallback && (
                            <>
                              <Label>Voice</Label>
                              <VoicePicker
                                provider={cfg.tts.fallback.provider ?? ""}
                                providerLabel={
                                  catalog.providers?.[cfg.tts.fallback.provider ?? ""]?.label
                                }
                                model={cfg.tts.fallback.model ?? ""}
                                value={cfg.tts.fallback.voice || null}
                                valueName={cfg.tts.fallback.voice_name || null}
                                onSelect={setTtsFallbackVoice}
                                withVoiceSettings={Boolean(ttsFallbackEntry?.supports_voice_settings)}
                                initialLanguage={resolveEntryLanguage(cfg.language, ttsFallbackEntry)}
                              />
                              <p className="text-[13px] leading-5 text-muted">
                                Callers hear this voice for the rest of the call once it takes
                                over, so pick one that sits close to the primary.
                              </p>
                            </>
                          )
                        }
                      />
                    </Stage>
                  )}
              </Panel>
            </section>

            {!isTextChannel && (
              <section id="sec-vision" className="scroll-mt-[112px]">
                <VisionInputSection
                  screenshare={cfg.vision_input?.screenshare?.enabled === true}
                  record={cfg.vision_input?.screenshare?.record === true}
                  modelReadsImages={draftReadsImages}
                  modelLabel={catalogEntryLabel(llmEntry) || "This model"}
                  onScreenshare={(enabled) =>
                    editConfig((c) => ({
                      ...c,
                      vision_input: {
                        screenshare: {
                          enabled,
                          // Turning the source off takes its recording with it:
                          // a stored `record: true` under a disabled parent is a
                          // setting that reads as on and does nothing.
                          record: enabled && c.vision_input?.screenshare?.record === true,
                        },
                      },
                    }))
                  }
                  onRecord={(record) =>
                    editConfig((c) => ({
                      ...c,
                      vision_input: { screenshare: { enabled: true, record } },
                    }))
                  }
                />
              </section>
            )}

            {cfg.channel === "voice" && (
              <section id="sec-keypad" className="scroll-mt-[112px]">
                <KeypadInputSection
                  enabled={cfg.keypad_input?.enabled === true}
                  timeout={cfg.keypad_input?.timeout ?? 2}
                  terminator={cfg.keypad_input?.terminator ?? "#"}
                  onEnabled={(enabled) =>
                    editConfig((c) => ({
                      ...c,
                      keypad_input: {
                        enabled,
                        timeout: c.keypad_input?.timeout ?? 2,
                        terminator: c.keypad_input?.terminator ?? "#",
                      },
                    }))
                  }
                  onTimeout={(timeout) =>
                    editConfig((c) => ({
                      ...c,
                      keypad_input: {
                        enabled: true,
                        timeout,
                        terminator: c.keypad_input?.terminator ?? "#",
                      },
                    }))
                  }
                  onTerminator={(terminator) =>
                    editConfig((c) => ({
                      ...c,
                      keypad_input: {
                        enabled: true,
                        timeout: c.keypad_input?.timeout ?? 2,
                        terminator,
                      },
                    }))
                  }
                />
              </section>
            )}

            {cfg.channel === "voice" && (
              <section id="sec-voicemail" className="scroll-mt-[112px]">
                <VoicemailSection
                  enabled={cfg.voicemail_detection?.enabled === true}
                  message={cfg.voicemail_detection?.message ?? null}
                  onEnabled={(enabled) =>
                    editConfig((c) => ({
                      ...c,
                      voicemail_detection: { enabled, message: c.voicemail_detection?.message ?? null },
                    }))
                  }
                  onMessage={(message) =>
                    editConfig((c) => ({ ...c, voicemail_detection: { enabled: true, message } }))
                  }
                />
              </section>
            )}

            <section id="sec-tools" className="scroll-mt-[112px]">
              <Panel className={LIFT_ON_HOVER}>
                <CardHead title="Tools" desc="What the agent can do mid-call">
                  {tools.length > 0 && <Badge>{attachedTools} attached</Badge>}
                  <Button variant="secondary" size="sm" onClick={() => startNewTool()}>
                    Add a tool
                  </Button>
                </CardHead>
                {tools.length === 0 ? (
                  <NothingToAttach what="tools">
                    Tools are the operations this agent can run mid-call — look something up,
                    write a record, hand off.
                  </NothingToAttach>
                ) : (
                  <div className="flex flex-col gap-1">
                    {tools.map((t) => {
                      const attached = (cfg.tools || []).some((ref) => ref.tool_id === t.id);
                      const published = !!t.published_version;
                      return (
                        <Unattachable
                          key={t.id}
                          reason={published ? null : "Publish this tool before you can attach it here."}
                        >
                        <div
                          className={cn(
                            "flex items-center gap-3 rounded-lg border border-transparent px-3 py-2.5 transition-colors hover:bg-subtle",
                            !published && "opacity-50",
                          )}
                        >
                          <BoxCheckbox
                            checked={attached}
                            disabled={!published}
                            onChange={() => toggleTool(t.id)}
                            ariaLabel={`Attach ${t.name}`}
                          />
                          <button type="button" onClick={() => published && toggleTool(t.id)} className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-2 text-left">
                            <strong className="font-mono text-[13.5px] font-semibold text-ink">{t.name}</strong>
                            <span className="truncate text-[12.5px] text-muted">{t.description}</span>
                          </button>
                          <Badge>{published ? `v${t.published_version}` : "draft"}</Badge>
                          <Link
                            href={`/tools/detail?id=${t.id}`}
                            aria-label={`Open ${t.name}`}
                            title={`Open ${t.name}`}
                            className="grid h-8 w-8 flex-none place-items-center rounded-md text-muted transition-colors hover:bg-surface hover:text-ink"
                          >
                            <span aria-hidden>↗</span>
                          </Link>
                        </div>
                        </Unattachable>
                      );
                    })}
                  </div>
                )}
                <p className="mt-3 text-[12.5px] leading-5 text-muted">
                  A published agent holds a frozen copy of its tools — republish after changing one.
                </p>
              </Panel>

              {/* One card, three columns — the hooks are one idea (a tool that
                  runs on its own at a fixed moment), and as three loose panels
                  under Tools they read as three unrelated settings. */}
              <Panel className={cn(LIFT_ON_HOVER, "mt-3.5")}>
                <CardHead title="Lifecycle hooks" desc="Tools that run on their own, at fixed moments" />
                {/* Every column is padded, not just the ones after a divider —
                    otherwise the first hook's select is 20px wider than the
                    other two. The grid is pulled out by that padding so the
                    first column still starts on the card's text column and the
                    last still ends on it. */}
                <div className="grid grid-cols-1 gap-5 md:-mx-5 md:grid-cols-3 md:gap-0 md:[&>*+*]:border-l md:[&>*+*]:border-line md:[&>*]:px-5">
                <div>
                  <HookHead
                    title="On Enter"
                    help={
                      cfg.channel === "text"
                        ? "Runs once, when the chat starts — on its first message."
                        : "Runs when the agent joins the call, just before the greeting — the greeting waits for it, and can read what it writes to userdata. Handoff targets run it too."
                    }
                  />
                  <div className="flex items-center gap-2">
                    <Select
                      className="min-w-0 flex-1"
                      value={cfg.on_enter?.tool_id || ""}
                      onChange={(e) => editConfig((c) => ({ ...c, on_enter: hookRef(e.target.value) }))}
                    >
                      <option value="">
                        {cfg.channel === "text" ? "— none —" : "— none (use the greeting) —"}
                      </option>
                      {hookTools.map((t) => (<option key={t.id} value={t.id}>{t.name}</option>))}
                    </Select>
                    <HookCreateButton hook="on enter" onClick={() => startHookTool("on_enter", "On enter")} />
                  </div>
                </div>

                <div>
                  <HookHead
                    title="On Exit"
                    help={
                      <>
                        {cfg.channel === "text"
                          ? "Runs when the chat ends or hands off, e.g. to send the lead somewhere. Set "
                          : "Runs when the call ends or hands off, e.g. to send the lead somewhere. Set "}
                        <code className="font-mono">on error: continue</code> so a failure never breaks a
                        finished {cfg.channel === "text" ? "chat" : "call"}.
                      </>
                    }
                  />
                  <div className="flex items-center gap-2">
                    <Select
                      className="min-w-0 flex-1"
                      value={cfg.on_exit?.tool_id || ""}
                      onChange={(e) => editConfig((c) => ({ ...c, on_exit: hookRef(e.target.value) }))}
                    >
                      <option value="">— none —</option>
                      {hookTools.map((t) => (<option key={t.id} value={t.id}>{t.name}</option>))}
                    </Select>
                    <HookCreateButton hook="on exit" onClick={() => startHookTool("on_exit", "On exit")} />
                  </div>
                </div>

                <div>
                  <HookHead
                    title="On User Turn Complete"
                    help={
                      <>
                        Runs after each user turn, before the reply. Their words are{" "}
                        <code className="font-mono">{"{{args.user_message}}"}</code>; use{" "}
                        <code className="font-mono">add_message</code> to add context to the reply.
                      </>
                    }
                  />
                  <div className="flex items-center gap-2">
                    <Select
                      className="min-w-0 flex-1"
                      value={cfg.on_user_turn_completed?.tool_id || ""}
                      onChange={(e) => editConfig((c) => ({ ...c, on_user_turn_completed: hookRef(e.target.value) }))}
                    >
                      <option value="">— none —</option>
                      {hookTools.map((t) => (<option key={t.id} value={t.id}>{t.name}</option>))}
                    </Select>
                    <HookCreateButton
                      hook="on user turn"
                      onClick={() => startHookTool("on_user_turn", "On user turn")}
                    />
                  </div>
                </div>
                </div>
              </Panel>
            </section>

            <section id="sec-tasks" className="scroll-mt-[112px]">
              <TasksSection
                tasks={cfg.tasks ?? []}
                agentVars={cfg.vars ?? []}
                library={taskLibrary}
                onChange={(tasks) => editConfig((c) => ({ ...c, tasks }))}
              />
            </section>

            <section id="sec-handoffs" className="scroll-mt-[112px]">
              <HandoffsSection
                handoffs={cfg.handoffs ?? []}
                agents={agents}
                selfId={id}
                channel={cfg.channel ?? "voice"}
                realtime={cfg.realtime != null}
                onChange={(handoffs) => editConfig((c) => ({ ...c, handoffs }))}
              />
            </section>

            <section id="sec-integrations" className="scroll-mt-[112px]">
              <Panel className={LIFT_ON_HOVER}>
                <CardHead title="MCP integrations" desc="External servers whose tools this agent can call">
                  {integrations.some((i) => i.capabilities.attachable_as_mcp) && (
                    <Badge>{attachedIntegrations} attached</Badge>
                  )}
                </CardHead>
                {(() => {
                  const mcpIntegrations = integrations.filter(
                    (i) => i.capabilities.attachable_as_mcp,
                  );
                  if (mcpIntegrations.length === 0) {
                    return (
                      <NothingToAttach
                        what="MCP integrations"
                        href="/integrations"
                        action="Connect an integration"
                      >
                        Connect an external MCP server and this agent can call its tools directly.
                        Telegram and WhatsApp are deployed from Triggers on that page, not here.
                      </NothingToAttach>
                    );
                  }
                  return (
                    <div className="flex flex-col gap-1">
                      {mcpIntegrations.map((integration) => {
                        const attached = (cfg.mcps || []).some(
                          (ref) => ref.integration_id === integration.id,
                        );
                        const active = integration.status === "active";
                        // You can always untick; you just cannot tick. One that
                        // stopped being active after it was attached stays
                        // attached — calls run without its tools until it is
                        // fixed — and detaching has to stay possible.
                        // `Unattachable` only wraps the case that really is
                        // blocked; the attached one says why on a row the user
                        // can still act on.
                        const disabled = !active && !attached;
                        const stale = !active && attached;
                        return (
                          <Unattachable
                            key={integration.id}
                            reason={disabled ? "Enable this integration before you can attach it here." : null}
                          >
                          <div
                            className={cn(
                              "flex items-center gap-3 rounded-lg border border-transparent px-3 py-2.5 transition-colors hover:bg-subtle",
                              disabled && "opacity-50",
                            )}
                          >
                            <BoxCheckbox
                              checked={attached}
                              disabled={disabled}
                              onChange={() => toggleIntegration(integration.id)}
                              ariaLabel={`Attach ${integration.display_name}`}
                            />
                            <span className="grid h-7 w-7 flex-none place-items-center rounded-lg border border-line-2 bg-white text-ink">
                              <IntegrationLogo kind={integration.provider} logoUrl={integration.logo_url} size={17} />
                            </span>
                            <button type="button" onClick={() => !disabled && toggleIntegration(integration.id)} className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-2 text-left">
                              <strong className="text-[13.5px] font-semibold text-ink">{integration.display_name}</strong>
                              <span className="truncate text-[12.5px] text-muted">{integration.provider_label}</span>
                            </button>
                            <Badge variant={active ? "live" : "warn"}>{integration.status === "needs_reconnect" ? "reconnect" : integration.status}</Badge>
                          </div>
                          {stale && (
                            <p className="px-3 pb-2 text-[12px] leading-4 text-warn">
                              This integration is {integration.status === "disabled" ? "disabled" : "not connected"} — the agent runs without its tools until you fix it on the{" "}
                              <Link href="/integrations" className="underline underline-offset-2">Integrations page</Link>.
                            </p>
                          )}
                          </Unattachable>
                        );
                      })}
                    </div>
                  );
                })()}
                <p className="mt-3 text-[12.5px] leading-5 text-muted">
                  Published with the agent, like its tools. Which of a server&rsquo;s
                  tools it may call is set on the Integrations page and applies immediately.
                </p>
              </Panel>
            </section>

            <section id="sec-faqs" className="scroll-mt-[112px]">
              <FaqsSection
                subject="agent"
                faqs={faqs}
                selected={cfg.faqs ?? []}
                onChange={(next) => editConfig((c) => ({ ...c, faqs: next }))}
              />
            </section>

            <section id="sec-limits" className="scroll-mt-[112px]">
              <LimitsSection
                channel={channel}
                isRealtime={isRealtime}
                maxSteps={cfg.max_steps}
                onMaxSteps={(max_steps) => editConfig((c) => ({ ...c, max_steps }))}
                maxDurationSeconds={cfg.max_duration_seconds ?? null}
                onMaxDurationSeconds={(max_duration_seconds) =>
                  editConfig((c) => ({ ...c, max_duration_seconds }))
                }
                silence={cfg.silence as Silence}
                onSilence={(silence) => editConfig((c) => ({ ...c, silence }))}
              />
            </section>

            {cfg.channel !== "text" && (
              <section id="sec-turn-handling" className="scroll-mt-[112px]">
                <TurnHandlingSection
                  turnHandling={turnHandling}
                  isVideo={isVideoChannel}
                  isRealtime={isRealtime}
                  turnDetection={turnDetection}
                  onChange={setTurnHandling}
                >
                  <NoiseCancellationGroup
                    catalog={catalog}
                    channel={cfg.channel || "voice"}
                    value={cfg.noise_cancellation as NoiseCancellation}
                    onChange={setNoiseCancellation}
                  />
                  <TuningGroup
                    title="Background audio"
                    help="Room tone under the whole call, and a “thinking” sound that covers the gap while the language model works."
                    muted={!(cfg.background_audio?.enabled ?? false)}
                    toggle={
                      <div className="flex items-center gap-2 text-[13px] text-ink-soft">
                        <BoxCheckbox
                          checked={cfg.background_audio?.enabled ?? false}
                          onChange={(enabled) => setBackgroundAudio({ enabled })}
                          ariaLabel="Background audio enabled"
                        />
                        <button
                          type="button"
                          className="text-left"
                          onClick={() => setBackgroundAudio({ enabled: !(cfg.background_audio?.enabled ?? false) })}
                        >
                          Enabled
                        </button>
                      </div>
                    }
                  >
                    <div className="flex flex-wrap items-center gap-2.5">
                      <Select
                        className="max-w-[175px]"
                        aria-label="Ambient sound"
                        value={cfg.background_audio?.ambient || ""}
                        onChange={(e) => setBackgroundAudio({ ambient: (e.target.value || null) as BackgroundAudioSpec["ambient"] })}
                      >
                        <option value="">no ambience</option>
                        {["office", "city", "forest", "crowd", "hold_music"].map((a) => (
                          <option key={a} value={a}>{a}</option>
                        ))}
                      </Select>
                      <Select
                        className="max-w-[190px]"
                        aria-label="Thinking sound"
                        value={cfg.background_audio?.thinking || ""}
                        onChange={(e) => setBackgroundAudio({ thinking: (e.target.value || null) as BackgroundAudioSpec["thinking"] })}
                      >
                        <option value="">no thinking sound</option>
                        {["keyboard", "keyboard2"].map((a) => (
                          <option key={a} value={a}>{a}</option>
                        ))}
                      </Select>
                    </div>
                  </TuningGroup>
                </TurnHandlingSection>
              </section>
            )}

            <section id="sec-conversation" className="scroll-mt-[112px]">
              <ConversationSection
                session={cfg.channel === "text" ? "chat" : "call"}
                conversation={cfg.conversation ?? {}}
                analysis={cfg.analysis ?? {}}
                onChange={setConversation}
                onAnalysisChange={setAnalysis}
              />
            </section>

            <section id="sec-analysis" className="scroll-mt-[112px]">
              <AnalysisSection
                session={cfg.channel === "text" ? "chat" : "call"}
                analysis={cfg.analysis ?? {}}
                config={cfg}
                catalog={catalog}
                onChange={setAnalysis}
                onRemember={(entry) => rememberModel("llm", entry)}
              />
            </section>

            <section id="sec-test" className="scroll-mt-[112px]">
              <Panel className={LIFT_ON_HOVER}>
                <CardHead
                  title={cfg.channel === "text" ? "Test chat" : "Test call"}
                  desc="Talk to this draft right here"
                />
                <div className="overflow-hidden rounded-xl border border-line-2 bg-subtle">
                  <div className="flex items-center gap-2 border-b border-line bg-surface px-3.5 py-2.5">
                    <span className="flex items-center gap-1.5" aria-hidden>
                      <span className="h-2 w-2 rounded-full bg-ink" />
                      <span className="h-2 w-2 rounded-full bg-line-strong" />
                      <span className="h-2 w-2 rounded-full bg-line-strong" />
                    </span>
                    <span className="ml-2 font-mono text-[11.5px] text-muted">
                      {cfg.channel === "text" ? "chat session" : cfg.channel === "video" ? "video call" : "web-call"}
                    </span>
                  </div>
                  {/* Each surface owns its own inset: the pre-call state is a
                      centered hero that needs room, the in-call layout is a
                      tighter grid. Both run the draft, saved first, so what the
                      form shows is what they are given. */}
                  {cfg.channel === "text"
                    ? <WebChat
                        agentId={id}
                        agentVersion="draft"
                        vars={cfg.vars ?? []}
                        vision={draftReadsImages}
                        disabledReason={readOnly ? READ_ONLY_TEST : null}
                        beforeStart={saveBeforeTest}
                      />
                    : <WebCall
                        agentId={id}
                        agentVersion="draft"
                        vars={cfg.vars ?? []}
                        video={cfg.channel === "video"}
                        vision={draftReadsImages}
                        screenShare={cfg.vision_input?.screenshare?.enabled === true}
                        disabledReason={readOnly ? READ_ONLY_TEST : null}
                        beforeStart={saveBeforeTest}
                      />}
                </div>
                <p className="mt-3 text-[12.5px] leading-5 text-muted">
                  Runs this draft, saving your edits first.{" "}
                  {agent.published_version
                    ? `Callers stay on v${agent.published_version} until you publish.`
                    : "Nothing is live until you publish."}
                </p>
              </Panel>
            </section>
          </div>
        </div>
      </Container>
      </div>

      {unsaved.pendingPath && (
        <UnsavedChangesModal
          sub="This agent has draft changes that have not been saved."
          onKeepEditing={unsaved.cancelLeave}
          onDiscard={unsaved.confirmLeave}
        />
      )}
      {historyOpen && (
        <VersionHistoryModal
          agent={agent}
          draft={cfg}
          toolNameById={toolNameById}
          integrationNameById={integrationNameById}
          faqNameById={faqNameById}
          onClose={() => setHistoryOpen(false)}
          onRolledBack={async () => {
            await load();
            toast({ kind: "ok", msg: "Rolled back. That version is live now." });
          }}
        />
      )}
      {newTool && (
        <NewToolModal
          defaultName={newTool.name}
          title={newTool.title}
          sub={newTool.sub}
          onClose={() => setNewTool(null)}
          // Creating a tool takes the user off this page, so the draft goes with
          // them — otherwise deciding you need a tool costs you every unsaved
          // edit that led you to that conclusion.
          beforeLeave={async () => {
            if (!dirty) return;
            try {
              await api.updateAgent(id, cfg);
            } catch {
              // Best effort: the tool exists either way, and blocking the
              // redirect on a save failure would strand the user on a modal.
            }
          }}
        />
      )}
      {validationModal && (
        <ValidationModal
          title={validationModal.title}
          sub={validationModal.sub}
          errors={validationModal.errors}
          warnings={validationModal.warnings}
          confirmLabel="Publish anyway"
          errorAction={(error) => {
            const provider = missingKeyProvider(error, catalog);
            if (!provider) return null;
            return (
              <Button
                variant="secondary"
                size="sm"
                className="flex-none"
                onClick={() => {
                  setValidationModal(null);
                  setMissingKey(provider);
                }}
              >
                Add key
              </Button>
            );
          }}
          onClose={() => setValidationModal(null)}
          onConfirm={() => {
            setValidationModal(null);
            finishPublish();
          }}
        />
      )}
      {missingKey && (
        <ProviderKeyModal
          provider={missingKey}
          // No key is exactly what the error said, so there is none to show and
          // none to remove.
          storedKey={null}
          onClose={() => setMissingKey(null)}
          onSaved={() => {
            setMissingKey(null);
            void publish();
          }}
        />
      )}
    </AppShell>
  );
}

// useSearchParams() suspends during prerender, so the exported page provides the
// boundary and AgentEditorInner does the work. Without this, `next build` fails with
// "useSearchParams() should be wrapped in a suspense boundary".
export default function AgentEditorPage() {
  return (
    <Suspense fallback={<EditorSkeleton agent />}>
      <AgentEditorInner />
    </Suspense>
  );
}
