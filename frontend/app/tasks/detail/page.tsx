"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import type {
  AgentConfig,
  CatalogResponse,
  FaqSummary,
  IntegrationResponse,
  LLMCatalogEntryResponse,
  TaskConfig,
  TaskResponse,
  TaskVersionDetailResponse,
  ToolResponse,
} from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import CopilotChat, { CopilotHeading } from "@/app/components/CopilotChat";
import { EditorSkeleton } from "@/app/components/EditorSkeleton";
import { FaqsSection } from "@/app/components/FaqsSection";
import { IntegrationLogo } from "@/app/components/IntegrationLogo";
import { ProviderKeyModal } from "@/app/components/ProviderKeyModal";
import {
  Badge,
  BoxCheckbox,
  Button,
  CardHead,
  Container,
  HelpDot,
  Input,
  Label,
  NothingToAttach,
  LIFT_ON_HOVER,
  MODEL_HOSTS_GRID,
  Panel,
  Segment,
  Select,
  Textarea,
  Tooltip,
  UnsavedChangesModal,
  ValidationModal,
  btn,
  useToast,
} from "@/app/components/ui";
import { BuiltinToolsSection } from "@/app/agents/detail/BuiltinToolsSection";
import { HostNote, HostSelect, useModelHosts } from "@/app/agents/detail/HostSelect";
import { ModelFallback } from "@/app/agents/detail/ModelFallback";
import {
  SearchedModelSelect,
  useSearchedModels,
} from "@/app/agents/detail/ModelSearch";
import { PriorityToggle } from "@/app/agents/detail/PriorityToggle";
import { ReasoningEffortSelect } from "@/app/agents/detail/ReasoningEffortSelect";
import { VariablesSection } from "@/app/agents/detail/VariablesSection";
import {
  catalogEntryLabel,
  isSearchedProvider,
  providerOptionsFor,
  searchedProviders,
  supportsChannel,
} from "@/app/agents/detail/agentConfig";
import { api } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { missingKeyProvider } from "@/lib/byok";
import { cn } from "@/lib/cn";
import { timezones } from "@/lib/date";
import { useUnsavedChanges } from "@/lib/useUnsavedChanges";
import { FinishingSection } from "./FinishingSection";
import { OutputSection, duplicateFields, nameCollisions } from "./OutputSection";
import WebCall from "@/app/components/WebCall";
import { RunPanel } from "./RunPanel";
import { VersionHistoryModal } from "./VersionHistoryModal";
import { diffTaskConfigs } from "./taskDiff";

const SECTIONS: [string, string][] = [
  ["sec-prompt", "Prompt"],
  ["sec-model", "Model"],
  ["sec-io", "Inputs and output"],
  ["sec-tools", "Tools"],
  ["sec-faqs", "FAQs"],
  ["sec-limits", "Limits"],
  ["sec-run", "Run"],
];
const SECTION_IDS = SECTIONS.map(([id]) => id);

/* What a provider tool costs a task, appended to every row of the section the
   agent editor shares with this one. Nobody is on a phone here, so the price is
   the clock rather than dead air — and it is worth saying out loud because the
   two budgets in the Limits card measure different things and only one of them
   moves: the run reaches its `timeout` having spent no steps at all, so the
   number the author reaches for first is the wrong one. */
const BUILTIN_TOOLS_NOTE =
  "It runs inside a step rather than as one, so it costs no steps and can spend tens of seconds of the run's timeout.";

/** The published definition, stamped with the task and version it was fetched
    for — so an answer for a version the page is no longer asking about is
    discarded rather than rendered. */
type LiveDefinition = { taskId: string; version: number; definition: TaskVersionDetailResponse };

/* One band per part of the task, the shape the agent editor's Models section
   uses. A band rather than a nested card: these belong to the section they sit
   in, and a box inside a box inside a box is what that section used to be. */
function Band({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-canvas p-4">
      <div className="mb-3">
        <div className="font-display text-[13.5px] font-semibold leading-5 tracking-tight text-ink">
          {title}
        </div>
        {desc && <div className="mt-0.5 text-[12.5px] leading-5 text-muted">{desc}</div>}
      </div>
      {children}
    </div>
  );
}

function TaskEditorInner() {
  // Query param, not a path segment: the dashboard ships as a static export,
  // which cannot prerender an unbounded set of ids.
  const id = useSearchParams().get("id") ?? "";
  const [task, setTask] = useState<TaskResponse | null>(null);
  const [cfg, setCfg] = useState<TaskConfig | null>(null);
  const [loadedCatalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [tools, setTools] = useState<ToolResponse[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationResponse[]>([]);
  const [faqs, setFaqs] = useState<FaqSummary[]>([]);
  // Two distinct failures, two distinct surfaces. `loadError` means we never got
  // a task to edit, so the page has nothing to render but the error. `saveError`
  // is a failed save — the editor and the user's unsaved edits must survive it,
  // so it renders as an inline banner above the form and never replaces it.
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [busy, setBusy] = useState(false);
  const [activeSection, setActiveSection] = useState<string>(SECTION_IDS[0]);
  const [historyOpen, setHistoryOpen] = useState(false);
  /* A 400's errors from a save, or what a pre-publish validate came back with.
     A list read as one run-on sentence is the reason nobody fixes the second
     problem. `sub` says which of the two this is. */
  const [validation, setValidation] = useState<{
    title: string;
    sub?: string;
    errors: string[];
    warnings: string[];
    onConfirm?: () => void;
  } | null>(null);
  /* The provider whose missing key blocked the publish. Fixing it here rather
     than sending the author to /byok keeps the draft, the modal and the publish
     they were in the middle of — the key verifies and the publish resumes. */
  const [missingKey, setMissingKey] = useState<{ id: string; label: string } | null>(null);
  const toast = useToast();

  /* The definition runs and email batches are actually using — read here rather
     than when History opens, because the header badge answers "has my draft
     moved on from live?" from it, and that question is on screen the whole time.

     Derived, never assigned: it is whatever `task.published_version` names, so
     it is fetched from that name and stamped with it. Publishing, rolling back
     and a CoPilot turn all move the name and the read follows on its own — no
     code path has to remember to refresh it. */
  const publishedVersion = task?.published_version ?? null;
  const [live, setLive] = useState<LiveDefinition | null>(null);
  useEffect(() => {
    if (!publishedVersion) return;
    let current = true;
    api
      .getTaskVersion(id, publishedVersion)
      .then((definition) => {
        if (current) setLive({ taskId: id, version: publishedVersion, definition });
      })
      .catch((error) => {
        if (current)
          toast({ kind: "err", msg: apiErrorMessage(error, "Could not read the published definition.") });
      });
    return () => {
      current = false;
    };
  }, [id, publishedVersion, toast]);
  /* Anything stamped with another task or another version answers a question
     the page is no longer asking. */
  const liveVersion =
    live && live.taskId === id && live.version === publishedVersion ? live.definition : null;

  async function load(): Promise<void> {
    try {
      const [t, c, tl, il, fl] = await Promise.all([
        api.getTask(id),
        api.catalog(),
        api.listTools().then((p) => p.items),
        api.listIntegrations().then((p) => p.items),
        api.listFaqs().then((p) => p.items),
      ]);
      setTask(t);
      setCfg(t.config);
      setCatalog(c);
      setTools(tl);
      setIntegrations(il);
      setFaqs(fl);
    } catch (error) {
      setLoadError(apiErrorMessage(error, "Could not load this task."));
    }
  }

  useEffect(() => {
    void load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps -- one load per task id

  /* Live sync: the embedded TaskCoPilot calls this the moment one of its actions
     lands. The form always reflects the latest saved config — the CoPilot is the
     other editor of the same object. */
  const liveRef = useRef<TaskResponse | null>(null);
  liveRef.current = task;
  async function refreshFromServer() {
    if (!liveRef.current) return;
    try {
      const [t, tl, il, fl] = await Promise.all([
        api.getTask(id),
        api.listTools().then((p) => p.items),
        api.listIntegrations().then((p) => p.items),
        api.listFaqs().then((p) => p.items),
      ]);
      setTools(tl);
      setIntegrations(il);
      setFaqs(fl);
      if (t.updated_at !== liveRef.current.updated_at) {
        setTask(t);
        setCfg(t.config);
      } else if (t.published_version !== liveRef.current.published_version) {
        /* A publish or rollback moves the version without touching `updated_at`
           — a rollback deliberately restores the version's own stamp. Taking the
           row anyway is what makes the badges and the Run panel follow. */
        setTask(t);
      }
    } catch {}
  }

  // Scroll-spy: the section nav follows manual scrolling.
  useEffect(() => {
    function onScroll() {
      let current: string = SECTION_IDS[0];
      for (const s of SECTION_IDS) {
        const el = document.getElementById(s);
        if (el && el.getBoundingClientRect().top <= 130) current = s;
      }
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
  }, [task]);

  // Computed before the early returns below, because the guard it feeds is a
  // hook and hooks cannot live after a conditional return.
  const dirty = !!task && !!cfg && JSON.stringify(cfg) !== JSON.stringify(task.config);
  const unsaved = useUnsavedChanges(dirty);

  /* OpenRouter's models are not in `GET /catalog`, so the ones this task names
     are fetched and merged back in — the same as the agent editor — and every
     lookup below (the trigger label, thinking, priority) works unchanged. */
  const { catalog: resolvedCatalog, remember: rememberModel } = useSearchedModels(loadedCatalog, cfg);
  const llmHosts = useModelHosts(resolvedCatalog, cfg?.llm);
  const llmFallbackHosts = useModelHosts(resolvedCatalog, cfg?.llm?.fallback);
  const llmEntries = useMemo(
    () => (resolvedCatalog?.llm ?? []).filter((e) => supportsChannel(e, "text")),
    [resolvedCatalog],
  );
  const llmEntry = llmEntries.find(
    (e) => e.provider === cfg?.llm?.provider && e.model === cfg?.llm?.model,
  ) as LLMCatalogEntryResponse | undefined;
  const llmFallbackEntry = llmEntries.find(
    (e) => e.provider === cfg?.llm?.fallback?.provider && e.model === cfg?.llm?.fallback?.model,
  ) as LLMCatalogEntryResponse | undefined;

  /* What is on screen against what actually runs. Different from `dirty`, which
     is the editor against the server draft: both can be true, and only this one
     means "republish". */
  const liveDiff = useMemo(
    () =>
      liveVersion && cfg
        ? diffTaskConfigs(liveVersion.config, cfg, {
            tool: (toolId) => tools.find((t) => t.id === toolId)?.name ?? null,
            integration: (integrationId) =>
              integrations.find((i) => i.id === integrationId)?.display_name ?? integrationId,
            faq: (faqId) => faqs.find((f) => f.id === faqId)?.name ?? faqId,
          })
        : null,
    [liveVersion, cfg, tools, integrations, faqs],
  );

  /* Which of the two ways to try a task this panel is showing. */
  const [runMode, setRunMode] = useState<"headless" | "call">("headless");

  /* A voice agent that exists for the length of one test call and is stored
     nowhere — `TokenRequest.agent` carries the whole definition, and the call
     plan runs it inline exactly as it runs an inline agent from the API.

     It declares the task's own variables so the caller can fill them in the
     setup dialog before the call. That also means the model is asked for none
     of them: a variable the calling agent declares is answered by the call, so
     a harness that declares all of them exercises the auto-filled half of the
     entry rule and not the "from the model" half. Testing that half
     needs an agent that declares less than the task does, which is the real
     agent, not this one.

     Everything else is left at its default, which is the catalog's default voice
     stack — the honest limit of testing a thing defined by what it inherits. */
  const harnessAgent = useMemo<AgentConfig | null>(() => {
    if (!cfg) return null;
    return {
      name: `Try ${cfg.name}`,
      channel: "voice",
      /* It has to say something, or the call opens on silence: an agent's
         opening line is spoken, not generated, so nothing prompts the model
         until the caller speaks. This one line is also the cue to speak. */
      greeting: "Ready when you are — say anything and I'll start.",
      prompt:
        `You exist to try out one job: "${cfg.name}". On the caller's first words, ` +
        "call the `try_this_task` tool straight away and let it take over — do not ask them " +
        "anything yourself. When it hands back, read out what it produced and say nothing else.",
      vars: cfg.vars ?? [],
      tasks: [
        {
          name: "try_this_task",
          // Inline: an attachment by id runs the published version, and this
          // tries the draft — the definition on screen, saved before the call.
          task: cfg,
          description: `Do this: ${cfg.name}.`,
        },
      ],
    } as AgentConfig;
  }, [cfg]);

  if (loadError) {
    return (
      <AppShell>
        <Container className="min-h-screen">
          <div className="mt-8 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {loadError}
          </div>
        </Container>
      </AppShell>
    );
  }

  if (!task || !cfg || !resolvedCatalog) return <EditorSkeleton />;
  // Re-bound after the guard: narrowing does not survive into the handlers below.
  const catalog: CatalogResponse = resolvedCatalog;

  function edit(mutate: (config: TaskConfig) => TaskConfig) {
    setCfg((current) => (current ? mutate(current) : current));
  }

  function jumpTo(sid: string) {
    setActiveSection(sid);
    document.getElementById(sid)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /** Every failed action lands here: the list in the modal, the headline in the
      banner. A 400 carries a list, and a list read as one run-on sentence is the
      reason nobody fixes the second problem. */
  function reportFailure(error: unknown, fallback: string, title: string, sub?: string) {
    const errors = apiErrorList(error);
    const message = apiErrorMessage(error, fallback);
    if (errors.length) setValidation({ title, sub, errors, warnings: [] });
    setSaveError(message);
  }

  /** The draft write both Save and Publish begin with. Returns false when it
      failed, so Publish stops rather than validating a config that is not
      stored. */
  async function saveDraft(): Promise<boolean> {
    if (!cfg) return false;
    setSaveError("");
    try {
      const saved = await api.updateTask(id, { config: cfg });
      // `versions` comes back null on a write — only `get_task` fills it — and a
      // save cannot change the publish history, so carry it forward. Dropping it
      // would leave History empty until the next full load.
      setTask((prev) => ({ ...saved, versions: prev?.versions ?? null }));
      setCfg(saved.config);
      return true;
    } catch (error) {
      reportFailure(
        error,
        "Could not save the task.",
        "This task cannot be saved",
        // The modal's default line talks about publishing, which is right for
        // the two publish paths below and wrong here.
        "Fix these issues before saving.",
      );
      return false;
    }
  }

  async function save(): Promise<boolean> {
    setBusy(true);
    try {
      const saved = await saveDraft();
      if (saved) toast({ kind: "ok", msg: "Draft saved." });
      return saved;
    } finally {
      setBusy(false);
    }
  }

  /* The Run panel runs the stored draft, so unsaved edits are saved first —
     the same save-then-act Publish does. */
  async function saveBeforeRun(): Promise<boolean> {
    return dirty ? save() : true;
  }

  async function finishPublish() {
    setBusy(true);
    setSaveError("");
    setValidation(null);
    try {
      const published = await api.publishTask(id);
      setTask(await api.getTask(id));
      const warned = published.warnings?.length ?? 0;
      toast({
        kind: "ok",
        msg: `Published v${published.version}${warned ? ` with ${warned} warning${warned === 1 ? "" : "s"}` : ""} — this is what runs now.`,
      });
    } catch (error) {
      reportFailure(error, "Could not publish the task.", "This task cannot be published");
    } finally {
      setBusy(false);
    }
  }

  /* Save, then pre-flight, then publish — the agent editor's flow. The
     pre-flight exists so a blocker is a list you can read and fix, rather than
     a bare "Could not publish". */
  async function publish() {
    setBusy(true);
    try {
      if (!(await saveDraft())) return;
      const result = await api.validateTask(id);
      if (result.errors.length || result.warnings.length) {
        setValidation({
          title: result.errors.length ? "This task cannot be published" : "Publish with warnings?",
          errors: result.errors,
          warnings: result.warnings,
          onConfirm: result.errors.length ? undefined : finishPublish,
        });
        return;
      }
      await finishPublish();
    } catch (error) {
      reportFailure(error, "Could not publish the task.", "This task cannot be published");
    } finally {
      setBusy(false);
    }
  }

  const llmProviders = providerOptionsFor(catalog, llmEntries, searchedProviders(catalog, "llm"));
  const llmModels = llmEntries.filter((e) => e.provider === cfg.llm?.provider);
  const attachedTools = (cfg.tools ?? []).length;
  const attachedMcps = (cfg.mcps ?? []).length;
  const collisions = nameCollisions((cfg.vars ?? []).map((v) => v.name), cfg.output ?? []);
  const duplicates = duplicateFields(cfg.output ?? []);

  function pickProvider(provider: string) {
    if (isSearchedProvider(catalog, provider, "llm")) {
      // Nothing to preselect: this provider's models are not in the catalog, and
      // guessing one out of hundreds on the author's behalf is not ours to do.
      // The picker says "Select…" and publish refuses an empty model.
      edit((c) => ({ ...c, llm: { provider, model: "", fallback: c.llm?.fallback } }));
      return;
    }
    const first = llmEntries.find((e) => e.provider === provider);
    if (!first) return;
    /* A model change drops `reasoning_effort` and `builtin_tools` rather than
       carrying them: both are per-model, and the new entry may accept neither. */
    edit((c) => ({ ...c, llm: { provider, model: first.model, fallback: c.llm?.fallback } }));
  }

  function pickModel(model: string) {
    // `hosts` goes too: a host set means something only for its own model.
    edit((c) => ({
      ...c,
      llm: { ...c.llm, model, reasoning_effort: null, builtin_tools: [], hosts: null },
    }));
  }

  function pickSearchedModel(entry: LLMCatalogEntryResponse) {
    // Remembered before the edit, so the thinking control finds its entry.
    rememberModel("llm", entry);
    edit((c) => ({
      ...c,
      llm: {
        ...c.llm,
        provider: entry.provider,
        model: entry.model,
        reasoning_effort: null,
        builtin_tools: [],
        hosts: null,
      },
    }));
  }

  function toggleTool(toolId: string) {
    edit((c) => {
      const attached = (c.tools ?? []).some((sel) => sel.tool_id === toolId);
      return {
        ...c,
        tools: attached
          ? (c.tools ?? []).filter((sel) => sel.tool_id !== toolId)
          : [...(c.tools ?? []), { tool_id: toolId }],
      };
    });
  }

  function toggleIntegration(integrationId: string) {
    edit((c) => {
      const attached = (c.mcps ?? []).some((sel) => sel.integration_id === integrationId);
      return {
        ...c,
        mcps: attached
          ? (c.mcps ?? []).filter((sel) => sel.integration_id !== integrationId)
          : [...(c.mcps ?? []), { integration_id: integrationId }],
      };
    });
  }

  /* A hook is one stored tool, referenced by id. An inline one is possible
     through the API and has no editor, so it is left alone rather than
     flattened into a picker that cannot represent it. */
  function hookRef(toolId: string) {
    return toolId ? { tool_id: toolId } : null;
  }

  const mcpIntegrations = integrations.filter((i) => i.capabilities.attachable_as_mcp);
  const hookTools = tools.filter((t) => t.published_version);

  return (
    <AppShell>
      <div className="min-h-screen bg-surface text-ink">
        <Container className="min-h-screen">
          {/* Sticky chrome, in two tiers: who this is and what you can do to it,
              then where you are inside it. */}
          <div className="sticky top-0 z-30 -mx-6 border-b border-line bg-surface/90 px-6 backdrop-blur-md">
            <div className="flex min-h-[68px] flex-wrap items-center gap-x-3 gap-y-2 pb-2 pt-3">
              <Link
                href="/tasks"
                className="-ml-1.5 flex h-8 flex-none items-center gap-1 rounded-lg px-1.5 text-[13px] font-medium text-muted transition-colors hover:bg-hover hover:text-ink"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M14 6l-6 6 6 6" />
                </svg>
                Tasks
              </Link>
              <span aria-hidden className="h-4 w-px flex-none bg-line-2" />
              <input
                className="min-w-[200px] flex-1 basis-[260px] rounded-lg border border-transparent bg-transparent px-1.5 py-1 font-display text-[21px] font-semibold leading-7 tracking-[-0.015em] text-ink transition-colors hover:border-line-2 focus:border-ink focus:outline-none focus:ring-2 focus:ring-ink/10"
                value={cfg.name}
                onChange={(e) => edit((c) => ({ ...c, name: e.target.value }))}
                aria-label="Task name"
              />
              <div className="flex flex-none items-center gap-2">
                {task.published_version ? (
                  <Badge variant="live" dot>Live v{task.published_version}</Badge>
                ) : (
                  <Badge>Draft</Badge>
                )}
                {dirty && <Badge variant="warn">unsaved</Badge>}
                {/* "unsaved" is the editor against the server draft; this is what
                    is on screen against what actually runs. Both can be true,
                    and only this one means "republish". */}
                {liveDiff && (
                  <Badge
                    variant={liveDiff.changed ? "warn" : "default"}
                    title={
                      liveDiff.changed
                        ? "Publish to put these changes live. Runs and email batches keep using the published version until you do."
                        : "Runs are using exactly this definition."
                    }
                  >
                    {liveDiff.changed
                      ? `differs from v${task.published_version}`
                      : `matches v${task.published_version}`}
                  </Badge>
                )}
              </div>
              <div className="ml-auto flex flex-none items-center gap-2">
                <Button
                  variant="secondary"
                  onClick={() => setHistoryOpen(true)}
                  disabled={!task.published_version}
                  title={
                    task.published_version
                      ? "Compare versions and roll back"
                      : "Nothing published yet — publishing freezes the draft as v1"
                  }
                >
                  History
                </Button>
                <Button variant="secondary" onClick={save} disabled={busy}>
                  Save draft
                </Button>
                <Button onClick={publish} disabled={busy}>
                  {task.published_version ? "Republish" : "Publish"}
                </Button>
              </div>
            </div>

            <div className="scroll-thin -mx-2.5 flex gap-0.5 overflow-x-auto">
              {SECTIONS.map(([sid, label]) => (
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

          {saveError && (
            <div role="alert" className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger [overflow-wrap:anywhere]">
              {saveError}
            </div>
          )}

          <div className="mt-6 grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_clamp(360px,30vw,480px)]">
            {/* The TaskCoPilot rail. First in the DOM so a narrow screen opens
                on it rather than on the top of a long form. */}
            <aside className="flex flex-col gap-4 xl:sticky xl:top-[112px] xl:col-start-2 xl:row-start-1 xl:h-[calc(100vh-136px)]">
              <Panel className="flex min-h-0 flex-1 flex-col p-4 max-xl:h-[560px] max-xl:flex-none">
                <CopilotHeading subject="task" />
                <CopilotChat
                  subject="task"
                  subjectId={id}
                  subjectName={cfg.name}
                  onChanged={refreshFromServer}
                />
              </Panel>
            </aside>

            <div className="flex flex-col gap-5 xl:col-start-1 xl:row-start-1">
              {/* ── Prompt ── */}
              <section id="sec-prompt" className="scroll-mt-[112px]">
                <Panel className={LIFT_ON_HOVER}>
                  <CardHead title="Prompt" desc="What the task does, and how to do it" />
                  <Textarea
                    value={cfg.prompt ?? ""}
                    onChange={(e) => edit((c) => ({ ...c, prompt: e.target.value }))}
                    rows={12}
                    aria-label="System prompt"
                    placeholder={
                      "You research companies from a work email address.\n\nGiven {{vars.email}}, find the company behind the domain, what it sells and who it sells to, then write one opening line that shows you did the reading."
                    }
                  />
                  <p className="mt-3 text-[12.5px] leading-5 text-muted">
                    Reads <code className="font-mono">{"{{vars.name}}"}</code> for the values a run
                    is given{cfg.timezone ? "" : ""}, and{" "}
                    <code className="font-mono">{"{{system_vars.now}}"}</code> /{" "}
                    <code className="font-mono">.date</code> /{" "}
                    <code className="font-mono">.time</code> once a timezone is set below. Nothing
                    is appended to this — how the run finishes is said once, in the{" "}
                    <code className="font-mono">submit_result</code> description under Inputs and
                    output.
                  </p>

                  <div className="mt-4 flex max-w-[420px] flex-col gap-2 border-t border-line pt-3.5">
                    <Label>Timezone</Label>
                    <Select
                      value={cfg.timezone ?? ""}
                      onChange={(e) => edit((c) => ({ ...c, timezone: e.target.value || null }))}
                    >
                      <option value="">— none —</option>
                      {timezones(cfg.timezone).map((zone) => (
                        <option key={zone} value={zone}>{zone}</option>
                      ))}
                    </Select>
                    <p className="text-[12px] leading-5 text-muted">
                      Only needed if the prompt or one of the tools reads the clock.
                    </p>
                  </div>
                </Panel>
              </section>

              {/* ── Model ── */}
              <section id="sec-model" className="scroll-mt-[112px]">
                <Panel className={LIFT_ON_HOVER}>
                  <CardHead title="Model" desc="The one model this task runs on" />
                  <Band title="Language model">
                    <div
                      className={
                        isSearchedProvider(catalog, cfg.llm?.provider, "llm")
                          ? MODEL_HOSTS_GRID
                          : "grid grid-cols-1 gap-3.5 md:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)_minmax(140px,180px)]"
                      }
                    >
                      <div className="flex min-w-0 flex-col gap-2">
                        <Label>Provider</Label>
                        <Select
                          value={cfg.llm?.provider ?? ""}
                          onChange={(e) => pickProvider(e.target.value)}
                        >
                          {llmProviders.map((provider) => (
                            <option key={provider.value} value={provider.value} data-logo-url={provider.logoUrl}>
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
                            onChange={pickSearchedModel}
                            ariaLabel="Model"
                          />
                        ) : (
                          <Select value={cfg.llm?.model ?? ""} onChange={(e) => pickModel(e.target.value)}>
                            {llmModels.map((entry) => (
                              <option key={entry.model} value={entry.model}>
                                {catalogEntryLabel(entry)}
                              </option>
                            ))}
                          </Select>
                        )}
                      </div>
                      <ReasoningEffortSelect
                        entry={llmEntry}
                        value={cfg.llm?.reasoning_effort}
                        onChange={(reasoning_effort) =>
                          edit((c) => ({ ...c, llm: { ...c.llm, reasoning_effort } }))
                        }
                      />
                      <HostSelect
                        catalog={catalog}
                        spec={cfg.llm}
                        hosts={llmHosts}
                        onChange={(hosts) => edit((c) => ({ ...c, llm: { ...c.llm, hosts } }))}
                      />
                    </div>
                    <HostNote
                      value={cfg.llm?.hosts}
                      hosts={llmHosts}
                      subject="the run"
                      failover={cfg.llm?.fallback ? "present" : "absent"}
                    />
                    <PriorityToggle
                      catalog={catalog}
                      entry={llmEntry}
                      value={cfg.llm?.priority}
                      onChange={(priority) => edit((c) => ({ ...c, llm: { ...c.llm, priority } }))}
                    />
                    <BuiltinToolsSection
                      entry={llmEntry}
                      value={cfg.llm?.builtin_tools ?? []}
                      runtimeNote={BUILTIN_TOOLS_NOTE}
                      onChange={(builtin_tools) =>
                        edit((c) => ({ ...c, llm: { ...c.llm, builtin_tools } }))
                      }
                    />
                    <ModelFallback
                      catalog={catalog}
                      kind="llm"
                      entries={llmEntries.filter(
                        (e) => !(e.provider === cfg.llm?.provider && e.model === cfg.llm?.model),
                      )}
                      extraProviders={searchedProviders(catalog, "llm")}
                      searchedEntry={llmFallbackEntry}
                      onProvider={(provider) =>
                        edit((c) => ({ ...c, llm: { ...c.llm, fallback: { provider, model: "" } } }))
                      }
                      value={cfg.llm?.fallback}
                      onChange={(entry) => {
                        // Remembered before the edit, so its thinking control
                        // finds the entry. Rebuilt rather than patched, so the
                        // old model's hosts and settings do not come along.
                        if (entry) rememberModel("llm", entry as LLMCatalogEntryResponse);
                        edit((c) => ({
                          ...c,
                          llm: {
                            ...c.llm,
                            fallback: entry ? { provider: entry.provider, model: entry.model } : null,
                          },
                        }));
                      }}
                      footer={
                        /* The failover's own tools and its own lane, against its
                           own catalog entry — the same tool name takes different
                           options at each provider, and the priority lane may not
                           exist there at all, so nothing is copied down from the
                           primary. */
                        <>
                          <HostNote
                            value={cfg.llm?.fallback?.hosts}
                            hosts={llmFallbackHosts}
                            subject="the run"
                            failover="impossible"
                          />
                          <PriorityToggle
                            catalog={catalog}
                            entry={llmFallbackEntry}
                            value={cfg.llm?.fallback?.priority}
                            onChange={(priority) =>
                              edit((c) => ({
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
                            runtimeNote={BUILTIN_TOOLS_NOTE}
                            onChange={(builtin_tools) =>
                              edit((c) => ({
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
                      <ReasoningEffortSelect
                        entry={llmFallbackEntry}
                        value={cfg.llm?.fallback?.reasoning_effort}
                        onChange={(reasoning_effort) =>
                          edit((c) => ({
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
                          edit((c) => ({
                            ...c,
                            llm: c.llm?.fallback
                              ? { ...c.llm, fallback: { ...c.llm.fallback, hosts } }
                              : c.llm,
                          }))
                        }
                      />
                    </ModelFallback>
                  </Band>
                </Panel>
              </section>

              {/* ── Inputs & output ── */}
              <section id="sec-io" className="scroll-mt-[112px]">
                <Panel className={LIFT_ON_HOVER}>
                  <CardHead
                    title="Inputs and output"
                    desc="What a run is given, and what it has to hand back"
                  />
                  <div className="flex flex-col gap-5">
                    <VariablesSection
                      vars={cfg.vars ?? []}
                      subject="task"
                      withRequired
                      onChange={(vars) => edit((c) => ({ ...c, vars }))}
                    />
                    <div className="border-t border-line pt-5">
                      <div className="mb-3 flex items-center gap-2">
                        <Label>Output</Label>
                        {/* A help dot rather than a paragraph, matching Inputs
                            above: two sub-sections of one card explaining
                            themselves two different ways is the inconsistency,
                            not the prose. */}
                        <HelpDot label="The typed result. Each field becomes one argument of the submit_result tool the model is given, and its description becomes that argument's — so the description is the prompt for the value, not a note about it." />
                      </div>
                      <OutputSection
                        output={cfg.output ?? []}
                        onChange={(output) => edit((c) => ({ ...c, output }))}
                      />
                      {/* The two rules the save enforces, said here rather than
                          left for the save to be the first to mention them. */}
                      {collisions.length > 0 && (
                        <p className="mt-3 text-[12.5px] leading-5 text-danger">
                          {collisions.join(", ")} {collisions.length === 1 ? "is" : "are"} both an
                          input and an output field. A run&rsquo;s inputs and its result are read
                          side by side, so one of the two would be unreachable.
                        </p>
                      )}
                      {duplicates.length > 0 && (
                        <p className="mt-3 text-[12.5px] leading-5 text-danger">
                          {duplicates.join(", ")} appears twice in the output — the later one would
                          silently win.
                        </p>
                      )}
                    </div>
                    {/* The output fields say what a result contains; these two
                        say when the model may hand one back and when it may
                        walk away instead. Same card, because that is one
                        contract read top to bottom. */}
                    <div className="border-t border-line pt-5">
                      <div className="mb-3 flex items-center gap-2">
                        <Label>Finishing</Label>
                        <HelpDot label="Every task is given two tools: one that submits the result, one that abandons the job. Nothing is added to the prompt about either, so these two sentences are the whole of what the model is told about how a run ends." />
                      </div>
                      <FinishingSection
                        cfg={cfg}
                        onChange={(patch) => edit((c) => ({ ...c, ...patch }))}
                      />
                    </div>
                  </div>
                </Panel>
              </section>

              {/* ── Tools ── */}
              <section id="sec-tools" className="scroll-mt-[112px]">
                <Panel className={LIFT_ON_HOVER}>
                  <CardHead title="Tools" desc="What the task can do while it works">
                    {tools.length > 0 && <Badge>{attachedTools} attached</Badge>}
                  </CardHead>
                  {tools.length === 0 ? (
                    <NothingToAttach what="tools" href="/tools" action="Build a tool">
                      A tool is something this task can run while it works — call an API, run a few
                      lines of TypeScript, look something up.
                    </NothingToAttach>
                  ) : (
                    <div className="flex flex-col gap-1">
                      {tools.map((t) => {
                        const attached = (cfg.tools ?? []).some((sel) => sel.tool_id === t.id);
                        const published = !!t.published_version;
                        const row = (
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
                            <button
                              type="button"
                              onClick={() => published && toggleTool(t.id)}
                              className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-2 text-left"
                            >
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
                        );
                        return published ? (
                          <div key={t.id}>{row}</div>
                        ) : (
                          <Tooltip
                            key={t.id}
                            label="Publish this tool before you can attach it here."
                            className="block w-full cursor-not-allowed"
                          >
                            {row}
                          </Tooltip>
                        );
                      })}
                    </div>
                  )}
                  <p className="mt-3 text-[12.5px] leading-5 text-muted">
                    Publishing this task pins each attached tool to the version live at that
                    moment, so republishing a tool does not change what this task does until you
                    publish it again.
                  </p>
                </Panel>

                <Panel className={cn(LIFT_ON_HOVER, "mt-3.5")}>
                  <CardHead
                    title="MCP servers"
                    desc="External servers whose tools this task can call"
                  >
                    {mcpIntegrations.length > 0 && <Badge>{attachedMcps} attached</Badge>}
                  </CardHead>
                  {mcpIntegrations.length === 0 ? (
                    <NothingToAttach
                      what="MCP integrations"
                      href="/integrations"
                      action="Connect an integration"
                    >
                      Connect an external MCP server and this task can call its tools directly —
                      the usual way a research task reaches a data provider.
                    </NothingToAttach>
                  ) : (
                    <div className="flex flex-col gap-1">
                      {mcpIntegrations.map((integration) => {
                        const attached = (cfg.mcps ?? []).some(
                          (sel) => sel.integration_id === integration.id,
                        );
                        const active = integration.status === "active";
                        // You can always untick; you just cannot tick. One that
                        // stopped being active after it was attached stays
                        // attached — runs go without its tools until it is fixed.
                        const disabled = !active && !attached;
                        return (
                          <div key={integration.id}>
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
                              <button
                                type="button"
                                onClick={() => !disabled && toggleIntegration(integration.id)}
                                className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-2 text-left"
                              >
                                <strong className="text-[13.5px] font-semibold text-ink">{integration.display_name}</strong>
                                <span className="truncate text-[12.5px] text-muted">{integration.provider_label}</span>
                              </button>
                              <Badge variant={active ? "live" : "warn"}>
                                {integration.status === "needs_reconnect" ? "reconnect" : integration.status}
                              </Badge>
                            </div>
                            {!active && attached && (
                              <p className="px-3 pb-2 text-[12px] leading-4 text-warn">
                                This integration is{" "}
                                {integration.status === "disabled" ? "disabled" : "not connected"} — the
                                task runs without its tools until you fix it on the{" "}
                                <Link href="/integrations" className="underline underline-offset-2">
                                  Integrations page
                                </Link>
                                .
                              </p>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </Panel>

                <Panel className={cn(LIFT_ON_HOVER, "mt-3.5")}>
                  <CardHead
                    title="Lifecycle"
                    desc="Tools that run around the work rather than during it"
                  />
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <Label>On enter</Label>
                      <p className="mb-2 mt-1 text-[12.5px] leading-5 text-muted">
                        Runs before the first thing the task does — fetch what it needs, seed
                        userdata.
                      </p>
                      <Select
                        value={cfg.on_enter?.tool_id || ""}
                        onChange={(e) => edit((c) => ({ ...c, on_enter: hookRef(e.target.value) }))}
                      >
                        <option value="">— none —</option>
                        {hookTools.map((t) => (
                          <option key={t.id} value={t.id}>{t.name}</option>
                        ))}
                      </Select>
                    </div>
                    <div>
                      <Label>On exit</Label>
                      <p className="mb-2 mt-1 text-[12.5px] leading-5 text-muted">
                        Runs once the task is done, however it ended — post the result somewhere,
                        clean up.
                      </p>
                      <Select
                        value={cfg.on_exit?.tool_id || ""}
                        onChange={(e) => edit((c) => ({ ...c, on_exit: hookRef(e.target.value) }))}
                      >
                        <option value="">— none —</option>
                        {hookTools.map((t) => (
                          <option key={t.id} value={t.id}>{t.name}</option>
                        ))}
                      </Select>
                    </div>
                    <div className="sm:col-span-2">
                      <Label>After each user turn</Label>
                      <p className="mb-2 mt-1 text-[12.5px] leading-5 text-muted">
                        Only when an agent enters this task and it is talking to someone — a run
                        started from here or from an email batch has nobody to take a turn. Their
                        words are <code className="font-mono">{"{{args.user_message}}"}</code>.
                      </p>
                      <Select
                        value={cfg.on_user_turn_completed?.tool_id || ""}
                        onChange={(e) =>
                          edit((c) => ({ ...c, on_user_turn_completed: hookRef(e.target.value) }))
                        }
                      >
                        <option value="">— none —</option>
                        {hookTools.map((t) => (
                          <option key={t.id} value={t.id}>{t.name}</option>
                        ))}
                      </Select>
                    </div>
                  </div>
                </Panel>
              </section>

              {/* ── FAQs ── */}
              <section id="sec-faqs" className="scroll-mt-[112px]">
                <FaqsSection
                  subject="task"
                  faqs={faqs}
                  selected={cfg.faqs ?? []}
                  onChange={(next) => edit((c) => ({ ...c, faqs: next }))}
                />
              </section>

              {/* ── Limits ── */}
              <section id="sec-limits" className="scroll-mt-[112px]">
                <Panel className={LIFT_ON_HOVER}>
                  <CardHead title="Limits" desc="What stops a run that has gone wrong" />
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="flex flex-col gap-2">
                      <Label htmlFor="task-max-steps">Steps</Label>
                      <Input
                        id="task-max-steps"
                        type="number"
                        min={1}
                        max={50}
                        value={cfg.max_steps ?? 25}
                        onChange={(e) =>
                          edit((c) => ({ ...c, max_steps: Number(e.target.value) || 1 }))
                        }
                      />
                      {/* An author who lowers this to be tidy will break a
                          research task and get `step_limit` with no idea why —
                          so say what a step actually is, right here. */}
                      <p className="text-[12px] leading-5 text-muted">
                        A step is one round of <em>model → tools → model</em>, not one tool call:
                        four tools in one reply cost one step. 25 is the default and is a lot of
                        research. Run out and the run fails as{" "}
                        <code className="font-mono">step_limit</code>.
                      </p>
                    </div>
                    <div className="flex flex-col gap-2">
                      <Label htmlFor="task-timeout">Timeout</Label>
                      <div className="flex items-center gap-2">
                        <Input
                          id="task-timeout"
                          type="number"
                          min={10}
                          max={600}
                          className="max-w-[140px]"
                          value={cfg.timeout_seconds ?? 300}
                          onChange={(e) =>
                            edit((c) => ({ ...c, timeout_seconds: Number(e.target.value) || 10 }))
                          }
                        />
                        <span className="text-[13px] leading-5 text-muted">seconds</span>
                      </div>
                      <p className="text-[12px] leading-5 text-muted">
                        The real budget, and the one that keeps a stuck run cheap. Steps are the
                        runaway-loop guard; this is the clock. 300s is the default.
                      </p>
                      {/* Only for a task that has one switched on, and only
                          here: a provider tool is the one thing that spends this
                          budget without spending the other, so an author who
                          hits `timeout` reaches for Steps and finds it
                          untouched. The Steps paragraph beside it already says
                          what a step is; this says what does not cost one. */}
                      {Boolean(
                        cfg.llm?.builtin_tools?.length || cfg.llm?.fallback?.builtin_tools?.length,
                      ) && (
                        <p className="text-[12px] leading-5 text-muted">
                          This task has provider tools on. Each search or code run happens{" "}
                          <em>inside</em> a step, so it costs none of the step budget and real
                          seconds of this one.
                        </p>
                      )}
                    </div>
                  </div>
                </Panel>
              </section>

              {/* ── Run ── */}
              <section id="sec-run" className="scroll-mt-[112px]">
                <Panel className={LIFT_ON_HOVER}>
                  <CardHead
                    title="Run"
                    desc="Give this draft values and watch what it does. This spends real tokens."
                  />
                  <Segment
                    value={runMode}
                    onChange={setRunMode}
                    options={[
                      { value: "headless", label: "Run it" },
                      { value: "call", label: "Talk to it" },
                    ]}
                    className="mb-4"
                  />
                  {runMode === "headless" ? (
                    <RunPanel
                      taskId={id}
                      config={cfg}
                      catalog={catalog}
                      publishedVersion={task.published_version ?? null}
                      beforeRun={saveBeforeRun}
                      onRan={refreshFromServer}
                    />
                  ) : (
                    <div className="flex flex-col gap-3">
                      <p className="max-w-[80ch] text-[12.5px] leading-5 text-muted">
                        A task written to <em>talk</em> has to be heard, and it cannot be the
                        agent on a call by itself — it declares no voice and no ears, and
                        testing it in a shape it never runs in would prove the wrong thing. So
                        this places a real web call to a throwaway agent whose only instruction
                        is to start this draft immediately, on the platform&rsquo;s default
                        voice rather than on whichever agent will really call it. It is a real
                        call and it costs like one.
                      </p>
                      <div className="overflow-hidden rounded-xl border border-line-2 bg-subtle">
                        <div className="flex items-center gap-2 border-b border-line bg-surface px-3.5 py-2.5">
                          <span className="flex items-center gap-1.5" aria-hidden>
                            <span className="h-2 w-2 rounded-full bg-ink" />
                            <span className="h-2 w-2 rounded-full bg-line-strong" />
                            <span className="h-2 w-2 rounded-full bg-line-strong" />
                          </span>
                          <span className="ml-2 font-mono text-[11.5px] text-muted">web-call</span>
                        </div>
                        <WebCall
                          agentId={null}
                          agent={harnessAgent}
                          /* The HARNESS declares them, not the task: the task
                             is entered as a tool, and every variable the
                             harness supplies by name is one the model is not
                             asked to invent. */
                          vars={harnessAgent?.vars ?? []}
                          vision={false}
                          beforeStart={saveBeforeRun}
                        />
                      </div>
                    </div>
                  )}
                </Panel>
              </section>
            </div>
          </div>
        </Container>
      </div>

      {validation && (
        <ValidationModal
          title={validation.title}
          sub={validation.sub}
          errors={validation.errors}
          warnings={validation.warnings}
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
                  setValidation(null);
                  setMissingKey(provider);
                }}
              >
                Add key
              </Button>
            );
          }}
          onConfirm={validation.onConfirm}
          onClose={() => setValidation(null)}
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

      {historyOpen && (
        <VersionHistoryModal
          task={task}
          draft={cfg}
          toolNameById={(toolId) => tools.find((t) => t.id === toolId)?.name ?? null}
          integrationNameById={(integrationId) =>
            integrations.find((i) => i.id === integrationId)?.display_name ?? integrationId
          }
          faqNameById={(faqId) => faqs.find((f) => f.id === faqId)?.name ?? faqId}
          onClose={() => setHistoryOpen(false)}
          onRolledBack={load}
        />
      )}

      {unsaved.pendingPath && (
        <UnsavedChangesModal
          sub="This task has changes you have not saved."
          onKeepEditing={unsaved.cancelLeave}
          onDiscard={unsaved.confirmLeave}
        />
      )}
    </AppShell>
  );
}

export default function TaskEditorPage() {
  return (
    <Suspense fallback={<EditorSkeleton />}>
      <TaskEditorInner />
    </Suspense>
  );
}
