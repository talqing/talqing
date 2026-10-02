"use client";
import { useEffect, useMemo, useRef, useState, Suspense } from "react";
import type { ComponentProps, ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { cn } from "@/lib/cn";
import { AppShell } from "@/app/components/AppShell";
import {
  BoxCheckbox,
  Field,
  Input,
  Textarea,
  Select,
  Button,
  Badge,
  Modal,
  Segment,
  SectionCard,
  Skeleton,
  SURFACE,
  UnsavedChangesModal,
  ValidationModal,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { localTimezone } from "@/lib/date";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { useUnsavedChanges } from "@/lib/useUnsavedChanges";
import { OperationFlowchart } from "./OperationFlowchart";
import { OperationPicker } from "./OperationPicker";
import { ResizableToolEditorLayout } from "./ResizableToolEditorLayout";
import CopilotChat, { copilotName } from "@/app/components/CopilotChat";
import { EMPTY_TEST_DRAFT, ToolTestPanel, type ToolTestDraft } from "./ToolTestPanel";
import { KVRows, asObject, inputValue } from "@/app/components/KVRows";
import {
  KIND_LABEL,
  TERMINAL_KINDS,
  TRANSFER_MODES,
  TRANSFER_RINGING_TIMEOUT_DEFAULT,
  TRANSFER_RINGING_TIMEOUT_MAX,
  TRANSFER_RINGING_TIMEOUT_MIN,
} from "./operationMetadata";
import type {
  ChainContainer,
  DataOperationDraft,
  IfDraft,
  OperationDraft,
  OperationDraftPatch,
  OperationKind,
  ParamDraft,
  PublishFieldDraft,
  TreeEditorActions,
} from "./operationTypes";
import { isDataOperation, isSpeech, paramValueType } from "./operationTypes";
import {
  chains,
  draftDefinition,
  draftSignature,
  inferredPublishKey,
  isValidEnumValueForType,
  makeNode,
  newId,
  paramsFromJsonSchema,
  paramsToJsonSchema,
  toLocal,
  toServer,
  treeIsSilent,
  treeProblems,
  walkTree,
  type OperationProblem,
} from "./definition";
import { ToolDiffView } from "./ToolDiffView";
import { VersionHistoryModal } from "./VersionHistoryModal";
import { EMPTY_DEFINITION, diffToolDefinitions, versionSnapshot } from "./toolDiff";
import type {
  AgentResponse,
  JsonObject,
  JsonValue,
  PublishStore,
  RunToolResponse,
  SecretResponse,
  ToolResponse,
  ToolVersionDetailResponse,
  ValidateResponse,
} from "@talqing/sdk";

type ValidationState = {
  checkedSignature: string;
  errors: string[];
  warnings: string[];
  status: "valid" | "warnings" | "errors";
};
type ValidationModalState = {
  errors: string[];
  warnings: string[];
  mode: "validate" | "publish";
};

/** The last test run, and the draft it ran against — so a trace can say when it
 *  has stopped describing what is on screen. */
type RunState = {
  checkedSignature: string;
  result: RunToolResponse;
};

const PARAM_TYPES = ["string", "number", "integer", "boolean", "enum"];

const PARAM_ROW =
  "grid-cols-[minmax(0,1fr)_auto] gap-x-2.5 md:grid-cols-[minmax(120px,0.9fr)_104px_minmax(0,1.5fr)_72px_32px]";

function findNode(nodes: OperationDraft[], id: string): OperationDraft | null {
  for (const node of walkTree(nodes)) if (node._id === id) return node;
  return null;
}

function removeNode(nodes: OperationDraft[], id: string): boolean {
  for (const chain of chains(nodes)) {
    const index = chain.findIndex((node) => node._id === id);
    if (index >= 0) { chain.splice(index, 1); return true; }
  }
  return false;
}

// append to a chain: container null = root chain; else {parentId, branch} of an `if`
function appendTo(nodes: OperationDraft[], container: ChainContainer, node: OperationDraft): boolean {
  if (!container) { nodes.push(node); return true; }
  const parent = findNode(nodes, container.parentId);
  if (parent?.kind !== "if") return false;
  parent[container.branch].push(node);
  return true;
}

const CODE = "rounded border border-line bg-subtle px-1 py-0.5 font-mono text-[12px]";
/** The handoff target picker's option for a team member, beside the agent ids. */
const TEAM_MEMBER = "team-member";
function ToolEditorInner(): JSX.Element {
  // Query param, not a path segment: the dashboard ships as a static
  // export, which cannot prerender an unbounded set of ids.
  const id = useSearchParams().get("id") ?? "";
  const [tool, setTool] = useState<ToolResponse | null>(null);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [longRunning, setLongRunning] = useState(false);
  const [silent, setSilent] = useState(false);
  const [disableInterruptions, setDisableInterruptions] = useState(false);
  const [params, setParams] = useState<ParamDraft[]>([]);
  const [ops, setOps] = useState<OperationDraft[]>([]);
  const [secrets, setSecrets] = useState<SecretResponse[]>([]);
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<string | string[]>("");
  const [busy, setBusy] = useState(false);
  const [validationModal, setValidationModal] = useState<ValidationModalState | null>(null);
  const [savedDraftSignature, setSavedDraftSignature] = useState("");
  const [validationState, setValidationState] = useState<ValidationState | null>(null);
  const [publishDialogOpen, setPublishDialogOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  /* The definition agents are actually calling. Held here rather than fetched
     when a dialog opens, because the header badge answers "has my draft moved
     on from live?" from it, and that question is on screen the whole time. */
  const [liveDefinition, setLiveDefinition] = useState<ToolVersionDetailResponse | null>(null);
  const [changelog, setChangelog] = useState("");
  const [pendingPublishChangelog, setPendingPublishChangelog] = useState<string | null>(null);
  const [rightPane, setRightPane] = useState<"copilot" | "test" | "tree">("copilot");
  const [runState, setRunState] = useState<RunState | null>(null);
  /* Lives here, not in the panel, so switching to the Tree tab and back keeps
     what you typed. It is not persisted anywhere — a reload starts over. */
  const [testDraft, setTestDraft] = useState<ToolTestDraft>(EMPTY_TEST_DRAFT);

  const currentDefinition = useMemo(
    () => draftDefinition({ name, desc, longRunning, silent, disableInterruptions, params, ops }),
    [name, desc, longRunning, silent, disableInterruptions, params, ops],
  );
  /* Not the author's to set: a tool in which no operation can contribute a
     response IS silent, and publishing freezes it that way. The checkbox shows
     it ticked and locked, and their own value comes back if the tree changes.
     An empty tree is vacuously silent but cannot be published, so it locks
     nothing — a brand-new tool would otherwise open with a tick nobody made. */
  const derivedSilent = useMemo(() => ops.length > 0 && treeIsSilent(ops), [ops]);
  /* Every operation still missing a value it cannot run without. The API refuses
     an incomplete operation at the request — each kind is a typed variant with
     required fields — so Save, Validate, Test and Publish all wait for this to
     be empty rather than round-tripping to a 422 that names a JSON path. */
  const problems = useMemo(() => treeProblems(ops), [ops]);
  const incomplete = problems.size > 0;
  const currentDraftSignature = useMemo(() => JSON.stringify(currentDefinition), [currentDefinition]);
  const dirty = !!savedDraftSignature && currentDraftSignature !== savedDraftSignature;
  /* Against the live version, not against the last save — and by content, not by
     timestamp, so editing a field and editing it back reads as no change. */
  const liveDiff = useMemo(
    () => (liveDefinition ? diffToolDefinitions(versionSnapshot(liveDefinition), currentDefinition) : null),
    [liveDefinition, currentDefinition],
  );
  /* Publishing never reuses a number, so the next one counts from the highest
     version ever published — not from the live one, which a rollback moves
     backwards. Roll v3 back to v1 and the next publish is still v4. */
  const nextVersion = Math.max(0, ...(tool?.versions ?? []).map((v) => v.version)) + 1;
  const validationStale = !!validationState && validationState.checkedSignature !== currentDraftSignature;
  const runStale = !!runState && runState.checkedSignature !== currentDraftSignature;
  /* Only a run of the tree on screen may mark up the tree on screen. Once the
     draft moves on, the step paths still resolve but they point at operations
     that have shifted underneath them. */
  const runSteps = useMemo(
    () => (runState && !runStale
      ? Object.fromEntries((runState.result.steps ?? []).map((s) => [s.path, s]))
      : null),
    [runState, runStale],
  );
  const agentNameById = useMemo(() => Object.fromEntries(agents.map((agent) => {
    const name = agent.config.name;
    return [agent.id, typeof name === "string" && name.trim() ? name : agent.id];
  })), [agents]);

  const unsaved = useUnsavedChanges(dirty);

  /* Put a server draft on screen, replacing whatever is in the form. */
  async function adoptDraft(t: ToolResponse, options: { keepValidation?: boolean } = {}): Promise<void> {
    setTool(t);
    setName(t.name);
    setDesc(t.description || "");
    setLongRunning(t.long_running_task);
    setSilent(t.silent);
    setDisableInterruptions(t.disable_interruptions);
    const nextParams = paramsFromJsonSchema(t.json_schema);
    const nextOps = toLocal(t.operations || []);
    setParams(nextParams);
    setOps(nextOps);
    setSavedDraftSignature(draftSignature({
      name: t.name,
      desc: t.description || "",
      longRunning: t.long_running_task,
      silent: t.silent,
      disableInterruptions: t.disable_interruptions,
      params: nextParams,
      ops: nextOps,
    }));
    if (!options.keepValidation) setValidationState(null);
    setLiveDefinition(t.published_version ? await api.getToolVersion(id, t.published_version) : null);
  }

  async function load(options: { keepValidation?: boolean } = {}): Promise<void> {
    try {
      const [t, s, ag] = await Promise.all([
        api.getTool(id),
        api.listSecrets().then((p) => p.items),
        api.listAllAgents(),
      ]);
      setAgents(ag);
      setSecrets(s);
      await adoptDraft(t, options);
    } catch (error) {
      setErr(apiErrorMessage(error, "Could not load this tool."));
    }
  }

  /* Live sync: ToolCoPilot calls this the moment one of its operations lands
     (and at turn end) — no background polling. The form ALWAYS reflects the
     latest server draft: if the server copy moved, we adopt it, unsaved local
     edits and all. The CoPilot is the other editor of the same draft. */
  const liveRef = useRef<ToolResponse | null>(null);
  liveRef.current = tool;
  async function refreshFromServer(): Promise<void> {
    const current = liveRef.current;
    if (!current) return;
    try {
      const [t, s, ag] = await Promise.all([
        api.getTool(id),
        api.listSecrets().then((p) => p.items),
        api.listAllAgents(),
      ]);
      // Secrets and agents are reference data the CoPilot may have added to;
      // always refresh them, never the form unless the draft actually moved.
      setSecrets(s);
      setAgents(ag);
      // publish bumps published_version WITHOUT touching updated_at, so compare
      // both — otherwise a CoPilot publish leaves a stale badge on screen.
      if (t.updated_at !== current.updated_at || t.published_version !== current.published_version) {
        await adoptDraft(t, { keepValidation: true });
      }
    } catch {}
  }

  useEffect(() => {
    // A different tool means different arguments and a trace that describes
    // something else entirely.
    setTestDraft(EMPTY_TEST_DRAFT);
    setRunState(null);
    load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps -- reload only when the tool changes

  // `load` reports its failure through `err`, and with no tool there is no
  // editor to show it in.
  if (!tool)
    return err ? (
      <AppShell>
        <div className="p-6">
          <div className="rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        </div>
      </AppShell>
    ) : (
      <ToolEditorSkeleton />
    );

  function jsonSchema(): JsonObject {
    return paramsToJsonSchema(params);
  }

  // ── all tree edits are local; nothing hits the server until Save ──
  function mutate(fn: (tree: OperationDraft[]) => void): void {
    setOps((prev) => { const c = structuredClone(prev); fn(c); return c; });
  }
  /* The one place an untyped patch meets a typed config. Every caller is inside
     a `op.kind === …` branch that knows which fields it is setting, and the API
     rejects anything that is not one of them — so the merge itself does not need
     to re-derive the variant. */
  function setCfg(opId: string, patch: Record<string, unknown>): void {
    mutate((t) => { const n = findNode(t, opId); if (n) Object.assign(n.config, patch); });
  }
  function setNode(opId: string, patch: OperationDraftPatch): void {
    mutate((t) => { const n = findNode(t, opId); if (n) Object.assign(n, patch); });
  }
  function addOp(container: ChainContainer, kind: OperationKind): void {
    setErr("");
    mutate((t) => { appendTo(t, container, makeNode(kind)); });
  }
  function delOp(opId: string): void {
    mutate((t) => { removeNode(t, opId); });
  }

  function leaveEditor(): void {
    unsaved.requestLeave("/tools");
  }

  async function saveDraft(showMessage = true): Promise<string> {
    const updatedTool = await api.patchTool(id, {
      name,
      description: desc,
      json_schema: jsonSchema(),
      long_running_task: longRunning,
      silent,
      disable_interruptions: disableInterruptions,
      operations: toServer(ops),
    });
    const canonicalOps = toLocal(updatedTool.operations || []);
    const signature = draftSignature({
      name,
      desc,
      longRunning,
      silent,
      disableInterruptions,
      params,
      ops: canonicalOps,
    });
    setTool((prev) => prev ? {
      ...prev,
      ...updatedTool,
      versions: prev.versions,
    } : updatedTool);
    setOps(canonicalOps);   // adopt canonical server shape
    setSavedDraftSignature(signature);
    if (showMessage) setMsg("Saved draft.");
    return signature;
  }

  async function save(): Promise<void> {
    setBusy(true); setMsg(""); setErr("");
    try {
      await saveDraft();
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function finishPublish(publishChangelog?: string): Promise<void> {
    setBusy(true); setMsg(""); setErr("");
    try {
      const r = await api.publishTool(id, publishChangelog?.trim() || undefined);
      const warningCount = Array.isArray(r.warnings) ? r.warnings.length : 0;
      setMsg(`Published v${r.version}${warningCount ? ` with ${warningCount} warning${warningCount === 1 ? "" : "s"}` : ""}. Republish agents that should use this version.`);
      setChangelog("");
      setPendingPublishChangelog(null);
      await load({ keepValidation: true });
    } catch (error) {
      // structured error contract: a list of validation errors arrives on .errors
      const errors = apiErrorList(error);
      if (errors.length) {
        setErr(errors);
        setValidationModal({ errors, warnings: [], mode: "publish" });
      }
      else setErr(apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function validateDraft(mode: "validate" | "publish", publishChangelog?: string): Promise<{
    result: ValidateResponse;
    hasIssues: boolean;
  }> {
    const signature = await saveDraft(false);
    const result = await api.validateTool(id);
    const errors = result.errors || [];
    const warnings = result.warnings || [];
    setValidationState({
      checkedSignature: signature,
      errors,
      warnings,
      status: errors.length ? "errors" : warnings.length ? "warnings" : "valid",
    });
    if (errors.length || warnings.length) {
      setValidationModal({ errors, warnings, mode });
      if (mode === "publish") setPendingPublishChangelog(publishChangelog?.trim() || "");
      return { result, hasIssues: true };
    }
    setMsg("Validation passed.");
    return { result, hasIssues: false };
  }

  /* A tool with no arguments has nothing to fill in, so Test just runs it. */
  function openTest(): void {
    setRightPane("test");
    if (!params.some((p) => p.name.trim())) void runTool({}, {}, {});
  }

  /* Saves first, exactly like Validate: the endpoint runs what is stored, so
     running without saving would test the tool you had a minute ago. */
  async function runTool(args: JsonObject, userdata: JsonObject, vars: JsonObject): Promise<void> {
    setBusy(true); setMsg(""); setErr("");
    try {
      const signature = await saveDraft(false);
      const result = await api.runTool(id, {
        arguments: args,
        userdata,
        // No agent behind a test run, so the clock comes from this browser and
        // the variables are whatever the panel was given, with nothing under them.
        timezone: localTimezone(),
        vars: vars as Record<string, string>,
      });
      setRunState({ checkedSignature: signature, result });
    } catch (error) {
      const errors = apiErrorList(error);
      setErr(errors.length ? errors : apiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function validate(): Promise<void> {
    setBusy(true); setMsg(""); setErr("");
    try {
      await validateDraft("validate");
    } catch (error) {
      const errors = apiErrorList(error);
      if (errors.length) {
        setErr(errors);
        setValidationModal({ errors, warnings: [], mode: "validate" });
      } else {
        setErr(apiErrorMessage(error));
      }
    } finally {
      setBusy(false);
    }
  }

  async function publish(nextChangelog: string): Promise<void> {
    setBusy(true); setMsg(""); setErr("");
    const trimmedChangelog = nextChangelog.trim();
    setPublishDialogOpen(false);
    try {
      const validation = await validateDraft("publish", trimmedChangelog);
      if (validation.hasIssues) return;
      await finishPublish(trimmedChangelog);
    } catch (error) {
      const errors = apiErrorList(error);
      if (errors.length) {
        setErr(errors);
        setValidationModal({ errors, warnings: [], mode: "publish" });
      } else {
        setErr(apiErrorMessage(error));
      }
    } finally {
      setBusy(false);
    }
  }

  const errors = Array.isArray(err) ? err : err ? [err] : [];
  const incompleteHint = incomplete
    ? `${problems.size === 1 ? "1 operation still needs" : `${problems.size} operations still need`} a value — each one says what on its card.`
    : undefined;
  const validationBadge = validationState
    ? (
      validationStale
        ? { variant: "warn" as const, label: "Validation stale", title: "The draft changed after the last validation." }
        : validationState.status === "valid"
          ? { variant: "live" as const, label: "Validation passed", title: "No validation errors or warnings." }
          : validationState.status === "warnings"
            ? { variant: "warn" as const, label: `${validationState.warnings.length} warning${validationState.warnings.length === 1 ? "" : "s"}`, title: validationState.warnings.join("\n") }
            : { variant: "danger" as const, label: `${validationState.errors.length} error${validationState.errors.length === 1 ? "" : "s"}`, title: validationState.errors.join("\n") }
    )
    : null;

  return (
    <AppShell>
      <div className="min-h-screen bg-subtle text-ink lg:h-screen">
        <section className="flex min-h-screen w-full flex-col bg-white lg:h-screen lg:min-h-0">
            <div className="sticky top-0 z-30 border-b border-line bg-white px-6 py-3">
              <div className="flex items-center gap-3">
                <div className="grid h-9 w-9 flex-none place-items-center rounded-lg border border-line-2 bg-white text-ink">
                  <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d="M9 9a3 3 0 1 1 4 2.8l2.5 4.2M15 14a3 3 0 1 1-2.6 4.5H8M9 12a3 3 0 1 1-2.4 4.8" />
                  </svg>
                </div>
                <input
                  aria-label="Tool name"
                  className="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-1 py-1 font-sans text-[18px] font-semibold leading-6 tracking-normal text-ink outline-none transition-colors hover:border-line-2 focus:border-ink focus:ring-2 focus:ring-ink/10"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
                <div className="hidden items-center gap-2 md:flex">
                  <Button
                    variant="secondary"
                    onClick={() => setHistoryOpen(true)}
                    disabled={!tool.published_version}
                    title={
                      tool.published_version
                        ? "Compare versions and roll back"
                        : "Nothing published yet — publishing freezes the draft as v1"
                    }
                  >
                    History
                  </Button>
                  <Button variant="secondary" onClick={save} disabled={busy || !dirty || incomplete} title={incompleteHint}>
                    Save
                  </Button>
                  <Button variant="secondary" onClick={validate} disabled={busy || incomplete} title={incompleteHint}>
                    Validate
                  </Button>
                  <Button variant="secondary" onClick={openTest} disabled={busy || incomplete} title={incompleteHint}>
                    Test
                  </Button>
                  <Button

                    onClick={() => setPublishDialogOpen(true)}
                    disabled={busy || incomplete}
                    title={incompleteHint}
                  >
                    Publish
                  </Button>
                </div>
                <button type="button" onClick={leaveEditor} className="grid h-8 w-8 flex-none place-items-center rounded-lg text-muted transition-colors hover:bg-subtle hover:text-ink" aria-label="Close tool editor">
                  <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden>
                    <path d="m4 4 8 8M12 4l-8 8" />
                  </svg>
                </button>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Badge variant={dirty ? "warn" : "default"} dot={dirty}>
                  {dirty ? "Unsaved changes" : "Saved draft"}
                </Badge>
                {tool.published_version ? (
                  <Badge variant="live" dot>Published v{tool.published_version}</Badge>
                ) : (
                  <Badge variant="warn">Unpublished</Badge>
                )}
                {/* Whether the draft has moved on from what agents call, by
                    content — an edit reverted by hand is not a difference. */}
                {liveDiff && (
                  <Badge
                    variant={liveDiff.changed ? "warn" : "default"}
                    title={
                      liveDiff.changed
                        ? "The draft is not what agents are calling. Publish to make it live."
                        : "The draft is exactly what agents are calling."
                    }
                  >
                    {liveDiff.changed
                      ? `Draft differs from v${tool.published_version}`
                      : `Matches v${tool.published_version}`}
                  </Badge>
                )}
                {validationBadge && (
                  <Badge variant={validationBadge.variant} title={validationBadge.title}>
                    {validationBadge.label}
                  </Badge>
                )}
                {/* Not a validation result — nothing has been sent yet. This is
                    the draft refusing to leave the browser until the operations
                    it names are complete; each one says what it needs on its own
                    card. */}
                {incomplete && (
                  <Badge variant="danger" title={incompleteHint}>
                    {problems.size} operation{problems.size === 1 ? "" : "s"} incomplete
                  </Badge>
                )}
                <div className="ml-auto flex w-full items-center gap-2 md:hidden">
                  <Button
                    variant="secondary"
                    className="flex-1"
                    onClick={() => setHistoryOpen(true)}
                    disabled={!tool.published_version}
                  >
                    History
                  </Button>
                  <Button variant="secondary" className="flex-1" onClick={save} disabled={busy || !dirty || incomplete} title={incompleteHint}>
                    Save
                  </Button>
                  <Button variant="secondary" className="flex-1" onClick={validate} disabled={busy || incomplete} title={incompleteHint}>
                    Validate
                  </Button>
                  <Button variant="secondary" className="flex-1" onClick={openTest} disabled={busy || incomplete} title={incompleteHint}>
                    Test
                  </Button>
                  <Button className="flex-1" onClick={() => setPublishDialogOpen(true)} disabled={busy || incomplete} title={incompleteHint}>
                    Publish
                  </Button>
                </div>
              </div>
            </div>

            <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-6 py-5 lg:overflow-hidden">
              <ResizableToolEditorLayout
                form={(
                  <div className="min-w-0 flex flex-col gap-4">
                    {msg && (
                      <div className="rounded-lg border border-live/25 bg-live/[0.06] px-3.5 py-2.5 text-[13px] leading-5 text-live">
                        {msg}
                      </div>
                    )}
                    {errors.length > 0 && (
                      <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger">
                        {errors.map((er, i) => (<div key={i}>- {er}</div>))}
                      </div>
                    )}
                    <SectionCard
                      title="Configuration"
                      helper="Describe to the LLM how and when to use the tool."
                    >
                  <Field label="Description">
                    <Textarea
                      value={desc}
                      onChange={(e) => setDesc(e.target.value)}
                      className="min-h-[66px] resize-none"
                    />
                  </Field>

                  <div className="grid gap-3 pt-1">
                    {/* The two are mutually exclusive and publish refuses the
                        pair, so each locks the other rather than letting an
                        author find out at publish. `silent` here is the
                        author's own tick and never `derivedSilent`: a tool
                        whose TREE derives silent — a silent `http` with publish
                        fields, then a `say` — is the best pattern this flag
                        has, so both boxes legitimately read ticked there and
                        neither blocks the other. */}
                    <CheckRow
                      checked={longRunning}
                      onChange={setLongRunning}
                      locked={silent}
                      title="Long-running task"
                      body={silent
                        ? "A background tool exists to report its result later, and a silent one never reports. For fire-and-forget, tick 'Background' on the operation instead."
                        : "Voice and video keep talking while it runs; text waits for the result. What the agent says in the meantime comes from this tool's description — write it so the agent knows to acknowledge and carry on."}
                    />
                    <CheckRow
                      checked={silent || derivedSilent}
                      onChange={setSilent}
                      locked={derivedSilent || longRunning}
                      title="Silent"
                      body={derivedSilent
                        ? "Every operation in this tool is silent, so the agent has nothing to add — it would otherwise improvise a sentence on top of what the tool already said. To add a closing line, add a Generate reply operation."
                        : longRunning
                          ? "A long-running task reports its result when the work finishes, so it cannot be silent. Untick 'Long-running task' first."
                          : "Do the effect without speaking an immediate response."}
                    />
                    <CheckRow
                      checked={disableInterruptions}
                      onChange={setDisableInterruptions}
                      title="Disable interruptions"
                      body="Prevent caller interruptions while this tool is running."
                    />
                  </div>
                    </SectionCard>

                    <SectionCard
                      title="Arguments"
                      className="overflow-hidden"
                      helper={<>The LLM fills these values. Reference them as <code className={CODE}>{"{{args.NAME}}"}</code>.</>}
                      action={
                        <Button
                          variant="secondary"
                          size="sm"
                         
                          onClick={() => setParams((a) => [...a, { name: "", type: "string", description: "", required: false, enumValues: [] }])}
                        >
                          Add argument
                        </Button>
                      }
                    >
                  {params.length === 0 ? (
                    <div className="rounded-lg border border-dashed border-line-strong bg-canvas px-3 py-3 text-[13px] leading-5 text-muted">
                      No arguments yet.
                    </div>
                  ) : (
                    /* One row per parameter, run to the card edges. Four of them
                       used to run the height of the screen as stacked cards, when
                       the thing you actually do here is read one against the next. */
                    <div className="-mx-5 -my-4">
                      <div className={cn("hidden bg-canvas px-5 py-2 text-[12px] font-medium leading-4 text-faint md:grid", PARAM_ROW)}>
                        <span>Name</span>
                        <span>Type</span>
                        <span>Description</span>
                        <span className="text-center">Required</span>
                        <span className="sr-only">Actions</span>
                      </div>
                      {params.map((p, i) => (
                        <div key={i} className="border-t border-line max-md:first:border-t-0">
                          <div className={cn("grid items-center gap-y-2 px-5 py-2", PARAM_ROW)}>
                            <Input
                              placeholder="order_id"
                              value={p.name}
                              onChange={(e) => setParams((a) => a.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                              aria-label={`Argument ${i + 1} name`}
                              className="min-h-9 py-1.5 font-mono text-[13px]"
                            />
                            <Select
                              value={p.type}
                              onChange={(e) => {
                                const type = e.target.value;
                                setParams((a) => a.map((x, j) => (
                                  /* Leaving enum re-points the value type at the one
                                     just picked, so coming back gives an enum of that. */
                                  j === i ? { ...x, type, ...(type === "enum" ? {} : { enumValueType: type }) } : x
                                )));
                              }}
                              aria-label={`Argument ${i + 1} type`}
                              className="min-h-9 py-1.5 text-[13px] max-md:col-span-2"
                            >
                              {PARAM_TYPES.map((t) => (<option key={t}>{t}</option>))}
                            </Select>
                            <Input
                              placeholder="How to pull this out of the transcript"
                              value={p.description}
                              onChange={(e) => setParams((a) => a.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))}
                              aria-label={`Argument ${i + 1} description`}
                              className="min-h-9 py-1.5 text-[13px] max-md:col-span-2"
                            />
                            <div className="flex items-center gap-2 max-md:col-span-2 md:justify-center">
                              <BoxCheckbox
                                checked={p.required}
                                onChange={(checked) => setParams((a) => a.map((x, j) => (j === i ? { ...x, required: checked } : x)))}
                                ariaLabel={`Argument ${i + 1} required`}
                              />
                              <span className="text-[13px] text-muted md:hidden">Required</span>
                            </div>
                            <button
                              type="button"
                              onClick={() => setParams((a) => a.filter((_, j) => j !== i))}
                              aria-label={`Delete argument ${p.name || i + 1}`}
                              className="grid h-8 w-8 place-items-center justify-self-end rounded-lg text-muted transition-colors hover:bg-danger/[0.06] hover:text-danger"
                            >
                              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                                <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
                              </svg>
                            </button>
                          </div>

                          {p.type === "enum" && (
                            <div className="border-t border-line bg-canvas px-5 py-3">
                              <EnumValuesEditor
                                values={p.enumValues}
                                type={paramValueType(p)}
                                onChange={(enumValues) => setParams((a) => a.map((x, j) => (j === i ? { ...x, enumValues } : x)))}
                              />
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                    </SectionCard>

                    <SectionCard
                      title="Operations"
                      helper="The ordered tree that runs when the tool is called."
                    >
                  <Chain
                    nodes={ops}
                    container={null}
                    problems={problems}
                    secrets={secrets}
                    agents={agents}
                    setCfg={setCfg}
                    setNode={setNode}
                    addOp={addOp}
                    delOp={delOp}
                  />
                    </SectionCard>
                  </div>
                )}
                aside={(
                  <div className="flex min-w-0 flex-col gap-3 lg:h-full lg:min-h-0">
                    <Segment
                      value={rightPane}
                      onChange={setRightPane}
                      options={[
                        { value: "copilot", label: copilotName("tool") },
                        { value: "test", label: "Test run" },
                        { value: "tree", label: "Tree" },
                      ]}
                      equal
                      className="flex-none"
                    />
                    {rightPane === "tree" ? (
                      <OperationFlowchart
                        operations={ops}
                        parameters={params}
                        agentNameById={agentNameById}
                        runSteps={runSteps}
                        className="min-h-0 flex-1"
                      />
                    ) : rightPane === "copilot" ? (
                      /* Unmounting on tab switch drops the SSE connection and
                         re-snapshots on return — the timeline lives on the
                         server, so nothing typed here is lost. */
                      <div className="flex min-h-0 flex-1 flex-col">
                        <CopilotChat subject="tool" subjectId={id} subjectName={name} onChanged={refreshFromServer} />
                      </div>
                    ) : (
                      <ToolTestPanel
                        params={params}
                        silent={silent || derivedSilent}
                        agentNameById={agentNameById}
                        running={busy}
                        result={runState?.result ?? null}
                        stale={runStale}
                        draft={testDraft}
                        onDraftChange={setTestDraft}
                        onRun={runTool}
                        className="min-h-0 flex-1"
                      />
                    )}
                  </div>
                )}
              />
            </div>
        </section>
        {publishDialogOpen && (
          <Modal
            title={`Publish v${nextVersion}`}
            sub="Freeze the draft as a new live tool version. Published agents keep their current tool snapshot until the agent is republished."
            onClose={() => !busy && setPublishDialogOpen(false)}
            width="max-w-[820px]"
            footer={
              <>
                <Button variant="secondary" onClick={() => setPublishDialogOpen(false)} disabled={busy}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={() => publish(changelog)} disabled={busy}>
                  Publish
                </Button>
              </>
            }
          >
            <div className="grid gap-4 pb-6">
              {/* What you are about to ship, before you ship it — the one check
                  that catches a publish nobody meant to make. */}
              <div className="flex flex-col gap-2">
                <div className="text-[12px] font-semibold uppercase leading-4 tracking-[0.04em] text-ink-soft">
                  {tool.published_version ? `Changes since v${tool.published_version}` : "Everything in this first version"}
                </div>
                <div className="scroll-thin max-h-[46vh] overflow-y-auto">
                  <ToolDiffView
                    diff={diffToolDefinitions(
                      liveDefinition ? versionSnapshot(liveDefinition) : EMPTY_DEFINITION,
                      currentDefinition,
                    )}
                    agentNameById={agentNameById}
                    initial={!liveDefinition}
                    emptyLabel={`No changes since v${tool.published_version} — publishing would freeze an identical version.`}
                  />
                </div>
              </div>
              <Field label="Changelog" hint="Optional, but useful when teammates review version history.">
                <Textarea
                  value={changelog}
                  onChange={(e) => setChangelog(e.target.value)}
                  placeholder="Updated booking API payload and saved confirmation_id."
                  className="min-h-[96px] resize-none"
                />
              </Field>
            </div>
          </Modal>
        )}
        {historyOpen && (
          <VersionHistoryModal
            tool={tool}
            draft={currentDefinition}
            agentNameById={agentNameById}
            onClose={() => setHistoryOpen(false)}
            onRolledBack={async () => {
              /* The rollback replaced the draft server-side, so reload rather
                 than patch state: name, arguments, tree and history all moved. */
              await load();
              setMsg(`Rolled back. Republish agents that should use this version.`);
            }}
          />
        )}
        {unsaved.pendingPath && (
          <UnsavedChangesModal
            sub="This tool has draft changes that have not been saved."
            onKeepEditing={unsaved.cancelLeave}
            onDiscard={unsaved.confirmLeave}
          />
        )}
        {validationModal && (
          <ValidationModal
            title="Publish checks"
            errors={validationModal.errors}
            warnings={validationModal.warnings}
            confirmLabel={validationModal.mode === "publish" ? "Publish anyway" : "Continue"}
            onClose={() => {
              setValidationModal(null);
              if (validationModal.mode === "publish") setPendingPublishChangelog(null);
            }}
            onConfirm={() => {
              const nextChangelog = pendingPublishChangelog || undefined;
              setValidationModal(null);
              setPendingPublishChangelog(null);
              if (validationModal.mode === "publish") finishPublish(nextChangelog);
            }}
          />
        )}
      </div>
    </AppShell>
  );
}

function EnumValuesEditor({
  values,
  type,
  onChange,
}: {
  values: string[];
  type: string;
  onChange: (values: string[]) => void;
}): JSX.Element {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");

  function addValue(): void {
    const value = draft.trim();
    if (!value) return;
    if (!isValidEnumValueForType(value, type)) {
      setError(
        type === "boolean"
          ? "Use true or false."
          : type === "integer"
            ? "Use a whole number."
            : "Use a valid number.",
      );
      return;
    }
    if (values.includes(value)) {
      setDraft("");
      setError("");
      return;
    }
    onChange([...values, value]);
    setDraft("");
    setError("");
  }

  function removeValue(value: string): void {
    onChange(values.filter((item) => item !== value));
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="text-[12.5px] font-medium leading-5 text-ink">
        Enum values
        {/* The values' own JSON type has no column of its own — it follows the
            type the parameter had before enum was picked — so it is named here
            on the rare row where it isn't the string default. */}
        {type !== "string" && <span className="ml-1 font-normal text-faint">({type})</span>}
        {!values.length && (
          <span className="ml-1 font-normal text-muted">
            — none yet, so this is still a plain {type}.
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            if (error) setError("");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addValue();
            }
          }}
          aria-label="New enum value"
          className="min-h-9 max-w-[260px] py-1.5 font-mono text-[13px]"
        />
        <Button type="button" variant="secondary" size="sm" onClick={addValue} disabled={!draft.trim()}>
          Add
        </Button>
      </div>
      {error && <div className="text-[12px] leading-5 text-danger">{error}</div>}
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {values.map((value) => (
            <span
              key={value}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line-2 bg-white py-1 pl-2 pr-1 font-mono text-[12.5px] leading-4 text-ink"
            >
              <span className="min-w-0 truncate">{value}</span>
              <button
                type="button"
                className="grid h-4 w-4 flex-none place-items-center rounded text-muted transition-colors hover:bg-subtle hover:text-danger"
                onClick={() => removeValue(value)}
                aria-label={`Remove enum value ${value}`}
              >
                <svg className="h-2.5 w-2.5" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                  <path d="m3 3 6 6M9 3 3 9" />
                </svg>
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function CheckRow({
  checked,
  onChange,
  title,
  body,
  /* Set when the platform decides this, not the author — the row still shows the
     value, and `body` says why it is not theirs to change. */
  locked = false,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  title: string;
  body: ReactNode;
  locked?: boolean;
}): JSX.Element {
  return (
    <div className="flex items-start gap-3">
      <BoxCheckbox
        checked={checked}
        onChange={onChange}
        ariaLabel={title}
        disabled={locked}
        className="mt-1"
      />
      <button
        type="button"
        className="min-w-0 text-left disabled:cursor-default"
        disabled={locked}
        onClick={() => onChange(!checked)}
      >
        <span className="block text-[14px] font-medium leading-5 text-ink">{title}</span>
        <span className="block text-[13px] leading-5 text-faint">{body}</span>
      </button>
    </div>
  );
}

// ───────────────────────── recursive chain renderer ─────────────────────────

function canAdd(nodes: OperationDraft[]): boolean {
  if (!nodes.length) return true;
  return !TERMINAL_KINDS.includes(nodes[nodes.length - 1].kind);
}

/* Why the "+ operation" control is gone. Each terminal kind ends the chain for a
   different reason, and the reason is what tells the author where the work they
   were about to add actually belongs. */
function TerminalChainNote({ kind }: { kind: string }): JSX.Element {
  if (kind === "handoff")
    return <>A <code className={CODE}>handoff</code> ends this chain (the conversation moves to the target agent).</>;
  if (kind === "end_call")
    return <>An <code className={CODE}>end_call</code> ends this chain. Anything the tool still has to do — log the outcome, post a lead — goes <em>before</em> it, while there is still a call.</>;
  if (kind === "transfer")
    return <>A <code className={CODE}>transfer</code> ends this chain — the caller is with a person and this session is over. Anything the tool still has to do goes <em>before</em> it.</>;
  return <>An <code className={CODE}>if</code> ends this chain (branches don&apos;t rejoin).</>;
}

type ChainProps = TreeEditorActions & {
  nodes: OperationDraft[];
  container: ChainContainer;
  problems: Map<string, OperationProblem[]>;
  secrets: SecretResponse[];
  agents: AgentResponse[];
};

function Chain({ nodes, container, problems, secrets, agents, setCfg, setNode, addOp, delOp }: ChainProps): JSX.Element {
  return (
    <div className="flex flex-col gap-2.5">
      {nodes.map((op, index) => (
        <OpCard
          key={op._id}
          op={op}
          /* Only this chain's earlier siblings count. A line spoken in the other
             branch of an `if` is not one this caller heard. */
          spokenBefore={nodes.slice(0, index).some(isSpeech)}
          problems={problems}
          secrets={secrets}
          agents={agents}
          setCfg={setCfg}
          setNode={setNode}
          addOp={addOp}
          delOp={delOp}
        />
      ))}
      {canAdd(nodes) ? (
        <AddOp onAdd={(kind) => addOp(container, kind)} />
      ) : (
        <div className="text-[13px] leading-5 text-muted">
          <TerminalChainNote kind={nodes[nodes.length - 1].kind} />
        </div>
      )}
    </div>
  );
}

/* The slot at the end of a chain. A dropdown of eleven terse labels asked the
   author to already know which one they wanted and told them nothing if they
   did not; the tile is a place the next operation visibly goes, and the picker
   behind it is where the eleven get room to explain themselves. */
function AddOp({ onAdd }: { onAdd: (kind: OperationKind) => void }): JSX.Element {
  const [picking, setPicking] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setPicking(true)}
        className="group grid h-[84px] w-full place-items-center rounded-xl border border-dashed border-line-strong bg-subtle/50 transition-colors hover:border-ink/25 hover:bg-subtle focus:outline-none focus-visible:border-ink focus-visible:ring-2 focus-visible:ring-ink/10"
      >
        {/* The same lift the picker's cards use, so hovering the slot and
            hovering a card are one gesture. */}
        <span className="inline-flex items-center gap-2 rounded-lg border border-line-2 bg-white px-3 py-1.5 text-[13px] font-semibold leading-5 text-ink shadow-rest transition-[transform,box-shadow] duration-150 ease-out group-hover:-translate-y-0.5 group-hover:shadow-pop group-active:translate-y-0 group-active:shadow-rest motion-reduce:transition-none motion-reduce:group-hover:transform-none">
          <svg className="h-3.5 w-3.5" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
            <path d="M7 2.5v9M2.5 7h9" />
          </svg>
          Add operation
        </span>
      </button>
      {picking && (
        <OperationPicker
          onPick={(kind) => {
            onAdd(kind);
            setPicking(false);
          }}
          onClose={() => setPicking(false)}
        />
      )}
    </>
  );
}

type OpCardProps = TreeEditorActions & {
  op: OperationDraft;
  /* Whether anything earlier in this chain speaks. Only `transfer` reads it —
     see the hint in `OpConfig`. */
  spokenBefore: boolean;
  /* The whole tree's gaps, by node id — this card reads its own, and passes the
     map down so an `if`'s branches can read theirs. */
  problems: Map<string, OperationProblem[]>;
  secrets: SecretResponse[];
  agents: AgentResponse[];
};

/* ── the operation card ──────────────────────────────────────────────────────
   White, like every other surface on this page. The card used to nest three
   fills — grey card, white publish box, grey field boxes inside that — which
   alternated rather than deepened, so the innermost box read as a sibling of
   the outermost. Depth is a hairline and an indent now, and nothing else.

   The header carries what the operation *is*. For HTTP that is the method and
   the URL, which had been sitting below as two labelled fields weighted the
   same as the timeout. Silent, background and timeout stay in the open in a
   band of their own — they change what the operation does, so they are read,
   not hunted for. */

function OpCard({ op, spokenBefore, problems, secrets, agents, setCfg, setNode, addOp, delOp }: OpCardProps): JSX.Element {
  /* Narrowed once, here, and read as objects below. A data operation is the only
     one that can be silent, publish or run detached — not "shown but ticked":
     the other kinds can never hand the LLM a response, so there is no state the
     box could be in that means anything. */
  const data = isDataOperation(op) ? op : null;
  const speech = isSpeech(op) ? op : null;
  const http = op.kind === "http" ? op : null;
  const gaps = problems.get(op._id) ?? [];
  const invalid = (field: string): boolean => gaps.some((gap) => gap.field === field);

  /* The same CheckRow the tool's own switches use in Configuration — these read
     as the same kind of decision, so they should look like it. */
  const behaviour = (
    <div className="flex flex-col gap-3">
      {/* Abort is the default, so it is the ticked state — the checkbox reads as
          the safe option being on rather than as an unexplained preference. */}
      <CheckRow
        checked={op.on_error === "abort"}
        onChange={(abort) => setNode(op._id, { on_error: abort ? "abort" : "continue" })}
        title="Abort on error"
        body="If this fails, the tool stops here. Untick and the remaining operations still run."
      />
      {data && (
        <CheckRow
          checked={data.silent || data.background_execution}
          onChange={(silent) => setNode(op._id, { silent })}
          /* Not a choice once Background is on: the operation runs against a
             result that is thrown away, so there is no response to hide. Shown
             ticked and locked rather than written into the node, so unticking
             Background brings the author's own choice back. */
          locked={data.background_execution}
          title="Silent"
          body={data.background_execution
            ? "A background operation never returns a response, so there is nothing to hand the LLM."
            : "Runs, but its response is not returned to the LLM."}
        />
      )}
      {speech && (
        <CheckRow
          checked={!!speech.config.wait_for_playback}
          onChange={(wait_for_playback) => setCfg(op._id, { wait_for_playback })}
          title="Finish speaking before the next step"
          body="The next operation won't start until this line has played, or until the caller interrupts. Leave this off for filler lines you want playing while the next step runs."
        />
      )}
      {data && (
        <CheckRow
          checked={data.background_execution}
          onChange={(background_execution) => setNode(op._id, {
            background_execution,
            ...(background_execution ? { publish_fields: [] } : {}),
          })}
          title="Background"
          body="Fire and forget; nothing waits for the response, so it can publish nothing."
        />
      )}
    </div>
  );

  return (
    /* Floating cards: each operation rests on a hairline shadow and lifts 2px
       under the cursor, the same gesture as the picker's tiles. Small enough
       that a card you are typing into does not shift out from under you. */
    <div className="overflow-hidden rounded-xl border border-line-2 bg-white shadow-rest transition-[transform,box-shadow,border-color] duration-150 ease-out hover:-translate-y-0.5 hover:border-line-strong hover:shadow-pop motion-reduce:transition-none motion-reduce:hover:transform-none">
      <div className="px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="h-1.5 w-1.5 flex-none rounded-full bg-ink" aria-hidden />
          <span className="text-[13px] font-semibold leading-5 text-ink">
            {KIND_LABEL[op.kind] || op.kind}
          </span>
          <span className="font-mono text-[11px] text-faint">{op.kind}</span>
          <div className="min-w-0 flex-1" />
          <button
            type="button"
            onClick={() => delOp(op._id)}
            aria-label={`Delete ${KIND_LABEL[op.kind] || op.kind} operation`}
            className="grid h-7 w-7 flex-none place-items-center rounded-lg text-muted transition-colors hover:bg-danger/[0.06] hover:text-danger"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
            </svg>
          </button>
        </div>

        {http && (
          /* A two-row grid rather than three label+control stacks side by side:
             Select renders taller than Input, so a flex row aligned at either
             edge staggers the labels against each other. */
          <div className="mt-2.5 grid grid-cols-[104px_88px_minmax(0,1fr)] items-center gap-x-2 gap-y-1">
            <OpFieldLabel>Method</OpFieldLabel>
            <OpFieldLabel htmlFor={`${op._id}-timeout`}>Timeout (s, max 120)</OpFieldLabel>
            <OpFieldLabel htmlFor={`${op._id}-url`}>URL</OpFieldLabel>
            <Select
              value={http.config.method}
              onChange={(e) => setCfg(op._id, { method: e.target.value })}
              aria-label="HTTP method"
              className="min-h-9 py-1.5 font-mono text-[12.5px]"
            >
              {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => (<option key={m}>{m}</option>))}
            </Select>
            <Input
              id={`${op._id}-timeout`}
              type="number"
              min={0.5}
              max={120}
              step={0.5}
              value={inputValue(http.config.timeout, 20)}
              onChange={(e) => setCfg(op._id, { timeout: Number(e.target.value || 20) })}
              className="min-h-9 py-1.5 text-[13px]"
            />
            <Input
              id={`${op._id}-url`}
              value={http.config.url}
              onChange={(e) => setCfg(op._id, { url: e.target.value })}
              placeholder="https://api.example.com/book"
              aria-invalid={invalid("url")}
              className="min-h-9 py-1.5 font-mono text-[13px]"
            />
          </div>
        )}
      </div>

      {/* What this operation still needs before it can be saved. The API refuses
          an incomplete operation at the request, so naming the gap here is what
          keeps Save from failing with something the author cannot place. */}
      {gaps.length > 0 && (
        <div className="border-t border-danger/25 bg-danger/[0.05] px-4 py-2.5">
          <ul className="flex flex-col gap-1 text-[13px] leading-5 text-danger">
            {gaps.map((gap) => (
              <li key={gap.field}>{gap.message}</li>
            ))}
          </ul>
        </div>
      )}

      {http ? (
        <>
          <OpSection
            title="Headers"
            action={<SectionAdd label="+ header" onClick={() => setCfg(op._id, { headers: { ...asObject(http.config.headers), "": "" } })} />}
          >
            <KVRows
              obj={http.config.headers}
              onChange={(headers) => setCfg(op._id, { headers })}
              valuePlaceholder="value or {{userdata.tenant_id}}"
              empty="None sent."
            />
          </OpSection>
          <OpSection
            title="Query"
            action={<SectionAdd label="+ param" onClick={() => setCfg(op._id, { query: { ...asObject(http.config.query), "": "" } })} />}
          >
            <KVRows
              obj={http.config.query}
              onChange={(query) => setCfg(op._id, { query })}
              empty="None appended to the URL."
            />
          </OpSection>
          <OpSection title="Body">
            <div className="px-4 pb-3">
              <Textarea
                value={http.bodyText}
                onChange={(e) => setNode(op._id, { bodyText: e.target.value })}
                placeholder={'Leave blank to send none — { "date": "{{args.date}}" }'}
                aria-label="Request body"
                className="min-h-[76px] py-2 font-mono text-[13px]"
              />
            </div>
          </OpSection>
        </>
      ) : (
        <div className="border-t border-line px-4 py-3">
          <OpConfig
            op={op}
            spokenBefore={spokenBefore}
            invalid={invalid}
            setCfg={setCfg}
            setNode={setNode}
            secrets={secrets}
            agents={agents}
          />
        </div>
      )}

      {/* Below the request (or the per-kind config) every operation is the same
          shape: what it keeps, then what it does when it runs. */}
      {data && <PublishFieldsBuilder op={data} setNode={setNode} />}
      <OpSection title="Behaviour">
        <div className="px-4 pb-3">{behaviour}</div>
      </OpSection>

      {http && (
        /* Said once, at the bottom, because it is true of every section that
           takes a value — it used to be hung off the URL label alone. */
        <div className="border-t border-line px-4 py-2 text-[13px] leading-5 text-muted">
          URL, headers, query and body all take{" "}
          <code className={CODE}>{"{{args.*}} {{tooldata.*}} {{userdata.*}} {{system_vars.*}} {{vars.*}} {{secrets.*}}"}</code>.
        </div>
      )}
      {op.kind === "if" && (
        <div className="grid gap-3 border-t border-line px-4 py-3">
          <Branch label="then" op={op} problems={problems} secrets={secrets} agents={agents} setCfg={setCfg} setNode={setNode} addOp={addOp} delOp={delOp} />
          <Branch label="else" op={op} problems={problems} secrets={secrets} agents={agents} setCfg={setCfg} setNode={setNode} addOp={addOp} delOp={delOp} />
        </div>
      )}
    </div>
  );
}

/* A titled band inside an operation card: label on the left, its one action on
   the right, rows beneath. Empty sections cost a single line, not a labelled
   block with a button under it.

   One operation card runs on two sizes and no more: 12px for anything that
   names a control (this title, `OpFieldLabel`, `SectionAdd`), 13px for prose.
   The title outranks a field label by weight and colour, not by size — half a
   pixel of difference reads as a mistake rather than a hierarchy. */
function OpSection({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="border-t border-line">
      {/* Held at the height an action button gives the row (26px + py-2), so the
          title sits the same distance below the hairline whether the band has
          one or not — centring it in a shorter row put Behaviour and Body 5px
          tighter than Headers, Query and Publishes. */}
      <div className="flex min-h-[42px] items-center gap-3 px-4 py-2">
        <h4 className="text-[12px] font-semibold leading-4 text-ink-soft">{title}</h4>
        <div className="min-w-0 flex-1" />
        {action}
      </div>
      {children}
    </div>
  );
}

/* Sized to the section titles (see `OpSection`) rather than to Field's 14px
   label, so the request line reads as one row of controls instead of four
   stacked headings. */
function OpFieldLabel({
  htmlFor,
  children,
}: {
  /* Omitted for Select, which renders a custom listbox with no id to point at
     — it carries an aria-label instead. */
  htmlFor?: string;
  children: ReactNode;
}): JSX.Element {
  const className = "block text-[12px] font-medium leading-4 text-faint";
  if (!htmlFor) return <span className={className}>{children}</span>;
  return <label htmlFor={htmlFor} className={className}>{children}</label>;
}

function SectionAdd({ label, onClick }: { label: string; onClick: () => void }): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex-none rounded-lg border border-line-2 bg-white px-2 py-1 text-[12px] font-medium leading-4 text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
    >
      {label}
    </button>
  );
}

/* The two stores, in the words the author will type to read the value back.
   `tooldata` dies with the tool run; `userdata` is the session's own bag. */
const STORE_HELP: Record<PublishStore, string> = {
  tooldata:
    "Kept until this tool finishes. Later operations here can read it; nothing after can.",
  userdata:
    "Kept by the session. Later tools, later turns and the agent's prompt can read it.",
};

/** The namespace prefix, rendered as the control that picks it — so the row
    reads as the token itself: `tooldata.booking_id`. It is the shared `Select`
    in its `inline` dress, so the popup, keyboard and type-ahead are the ones
    every other dropdown on the page uses. */
function StoreSelect({
  value,
  onChange,
  ariaLabel,
}: {
  value: PublishStore;
  onChange: (store: PublishStore) => void;
  ariaLabel: string;
}): JSX.Element {
  return (
    <Select
      inline
      value={value}
      onChange={(e) => onChange(e.target.value as PublishStore)}
      aria-label={ariaLabel}
      title={STORE_HELP[value]}
      className="flex-none"
      triggerClassName="gap-1 rounded-r-none bg-subtle py-1.5 pl-2.5 pr-2 font-mono text-[13px] text-ink-soft hover:text-ink"
      popupClassName="font-mono"
    >
      {/* `title` on an option becomes its hover tooltip and its description —
          the two names are only meaningful once you know what they cost. */}
      <option value="tooldata" title={STORE_HELP.tooldata}>
        tooldata.
      </option>
      <option value="userdata" title={STORE_HELP.userdata}>
        userdata.
      </option>
    </Select>
  );
}

function PublishFieldsBuilder({
  op,
  setNode,
}: {
  op: DataOperationDraft;
  setNode: TreeEditorActions["setNode"];
}): JSX.Element {
  const sourceLabel =
    op.kind === "http"
      ? "JSON response path"
      : op.kind === "frontend_rpc"
        ? "Client response path"
        : "Returned object path";
  const sourcePlaceholder =
    op.kind === "http"
      ? "booking.id"
      : op.kind === "frontend_rpc"
        ? "form.email"
        : "total";
  const help =
    op.kind === "http"
      ? "Add one to keep a value from the JSON response."
      : op.kind === "frontend_rpc"
        ? "Add one to keep a value from the web client's response."
        : "Add one to keep a value from the handler's return value.";

  function updateField(fieldId: string, patch: Partial<PublishFieldDraft>): void {
    setNode(op._id, {
      publish_fields: op.publish_fields.map((field) =>
        field._id === fieldId ? { ...field, ...patch } : field,
      ),
    });
  }

  function removeField(fieldId: string): void {
    setNode(op._id, {
      publish_fields: op.publish_fields.filter((field) => field._id !== fieldId),
    });
  }

  /* `path → store.key`, one row each. The store is the prefix on the key rather
     than a column of its own: the two choices ARE the two template roots, so the
     row already spells out the token a later operation will type. */
  return (
    <OpSection
      title="Publishes"
      action={
        op.background_execution ? undefined : (
          <SectionAdd
            label="+ field"
            onClick={() => setNode(op._id, {
              publish_fields: [
                ...op.publish_fields,
                { _id: newId(), path: "", key: "", store: "tooldata" },
              ],
            })}
          />
        )
      }
    >
      {op.background_execution ? (
        <div className="px-4 pb-2.5 text-[13px] leading-5 text-muted">
          Nothing — a background operation does not wait for a result, so it has none to save.
        </div>
      ) : op.publish_fields.length === 0 ? (
        <div className="px-4 pb-2.5 text-[13px] leading-5 text-muted">
          Nothing kept. {help}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5 px-4 pb-2.5">
          {op.publish_fields.map((field) => (
            <div
              key={field._id}
              className="grid grid-cols-[minmax(0,1fr)_14px_minmax(0,1fr)_28px] items-center gap-2"
            >
              <Input
                value={field.path}
                onChange={(e) => updateField(field._id, { path: e.target.value })}
                placeholder={sourcePlaceholder}
                aria-label={sourceLabel}
                className="min-h-9 py-1.5 font-mono text-[13px]"
              />
              <span className="text-center text-[13px] text-faint" aria-hidden>→</span>
              <div className="flex min-w-0 items-center rounded-[10px] border border-line-2 bg-white transition-colors focus-within:border-ink focus-within:ring-2 focus-within:ring-ink/10 hover:border-line-strong">
                <StoreSelect
                  value={field.store}
                  onChange={(store) => updateField(field._id, { store })}
                  ariaLabel="Where the published value is kept"
                />
                <input
                  value={field.key}
                  onChange={(e) => updateField(field._id, { key: e.target.value })}
                  placeholder={inferredPublishKey(field.path) || "booking_id"}
                  aria-label="Name it is kept under"
                  className="min-w-0 flex-1 bg-transparent py-1.5 pl-0 pr-2.5 font-mono text-[13px] leading-5 text-ink placeholder:text-placeholder focus:outline-none"
                />
              </div>
              <button
                type="button"
                onClick={() => removeField(field._id)}
                aria-label={`Remove published field ${field.path || "row"}`}
                className="grid h-7 w-7 place-items-center justify-self-end rounded-lg text-muted transition-colors hover:bg-danger/[0.06] hover:text-danger"
              >
                <svg className="h-3.5 w-3.5" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                  <path d="m3 3 6 6M9 3 3 9" />
                </svg>
              </button>
            </div>
          ))}
        </div>
      )}
    </OpSection>
  );
}

type BranchProps = TreeEditorActions & {
  label: "then" | "else";
  op: IfDraft;
  problems: Map<string, OperationProblem[]>;
  secrets: SecretResponse[];
  agents: AgentResponse[];
};

function Branch({ label, op, ...rest }: BranchProps): JSX.Element {
  const nodes = op[label];
  return (
    <div className="ml-0.5 border-l-2 border-line pl-4">
      <div className="mb-2.5">
        <span className="inline-flex items-center rounded-md border border-line bg-subtle px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-ink-soft">
          {label}
        </span>
      </div>
      <Chain nodes={nodes} container={{ parentId: op._id, branch: label }} {...rest} />
    </div>
  );
}

// ───────────────────────────── per-kind config ──────────────────────────────

/* One line per pair, no per-row labels — the section title above already says
   what these are, and it said it once instead of once per row. */
type OpConfigProps = Pick<TreeEditorActions, "setCfg" | "setNode"> & {
  op: OperationDraft;
  spokenBefore: boolean;
  /* Whether this operation's named field is one of the gaps `OpCard` listed
     above — the two are the same list, read once as prose and once per field. */
  invalid: (field: string) => boolean;
  secrets: SecretResponse[];
  agents: AgentResponse[];
};

/* No `http` branch: an HTTP request is laid out by OpCard itself, because its
   method and URL belong in the card header rather than in the body. */
function OpConfig({ op, spokenBefore, invalid, setCfg, setNode, secrets, agents }: OpConfigProps): JSX.Element {
  if (op.kind === "code")
    return (
      <div className="grid gap-2.5">
        <Field
          label={
            <>
              TypeScript — <code className={CODE}>{"handler({ args, tooldata, userdata, system_vars, vars, secrets })"}</code> returns an object;
              transpiled at publish, run on the isolated code service. Read state as
              properties (<code className={CODE}>system_vars.now</code>), never as{" "}
              <code className={CODE}>{"{{system_vars.now}}"}</code> — a code operation has no
              templates, so braces here are sent as written
            </>
          }
        >
          <Textarea
            value={op.config.source_ts}
            onChange={(e) => setCfg(op._id, { source_ts: e.target.value })}
            aria-invalid={invalid("source_ts")}
            spellCheck={false}
            className="scroll-thin min-h-[220px] overflow-auto whitespace-pre font-mono text-[13px]"
          />
        </Field>
        <div className="flex items-end gap-2">
          <Field label="Timeout (seconds, max 30)" className="w-[160px] flex-none">
            <Input
              type="number"
              min={1}
              max={30}
              value={inputValue(op.config.timeout, 10)}
              onChange={(e) => setCfg(op._id, { timeout: Number(e.target.value) })}
            />
          </Field>
          <span className="pb-2 text-[13px] leading-5 text-muted">
            <code className={CODE}>fetch</code> reaches public URLs only (no private/internal addresses); no Node APIs.
            Secrets are available as <code className={CODE}>input.secrets.NAME</code>. Over time/memory → the tool fails into a graceful spoken error.
          </span>
        </div>
      </div>
    );

  if (op.kind === "if")
    return (
      <div className="flex items-center gap-2">
        <Input value={inputValue(op.config.left)} onChange={(e) => setCfg(op._id, { left: e.target.value })} placeholder="{{tooldata.status}}" className="flex-1 font-mono" />
        <Select value={op.config.op} onChange={(e) => setCfg(op._id, { op: e.target.value })} className="w-[110px] flex-none">
          {["eq", "neq", "gt", "lt", "exists", "in"].map((o) => (<option key={o}>{o}</option>))}
        </Select>
        <Input value={inputValue(op.config.right)} onChange={(e) => setCfg(op._id, { right: e.target.value })} placeholder="new" className="flex-1 font-mono" />
      </div>
    );

  if (op.kind === "set_variable") {
    const store = op.config.store;
    return (
      <div className="grid gap-2.5">
        <div className="flex items-end gap-2">
          {/* Wide enough for the `userdata.` prefix plus a readable name — at
              240px the name it is stored under was the part that got clipped. */}
          <Field label="Variable" className="w-[320px] flex-none">
            <div className="flex min-w-0 items-center rounded-[10px] border border-line-2 bg-white transition-colors focus-within:border-ink focus-within:ring-2 focus-within:ring-ink/10 hover:border-line-strong">
              <StoreSelect
                value={store}
                onChange={(next) => setCfg(op._id, { store: next })}
                ariaLabel="Where the variable is kept"
              />
              <input
                value={op.config.key}
                onChange={(e) => setCfg(op._id, { key: e.target.value })}
                placeholder="customer_tier"
                aria-label="Variable name"
                aria-invalid={invalid("key")}
                className="min-w-0 flex-1 bg-transparent py-1.5 pl-0 pr-2.5 font-mono text-[13px] leading-5 text-ink placeholder:text-placeholder focus:outline-none"
              />
            </div>
          </Field>
          <Field label={<>Value (templated — a lone <code className={CODE}>{"{{...}}"}</code> token keeps its type)</>} className="flex-1">
            <Input value={inputValue(op.config.value)} onChange={(e) => setCfg(op._id, { value: e.target.value })} placeholder={'gold, or {{args.tier}}, or {{tooldata.lookup_result}}'} className="font-mono" />
          </Field>
        </div>
        <p className="text-[13px] leading-5 text-muted">{STORE_HELP[store]}</p>
      </div>
    );
  }

  if (op.kind === "end_call")
    return (
      <div className="text-[13px] leading-5 text-muted">
        Ends the session. To speak before ending, add a Say operation immediately before this one —
        anything already being spoken, including the agent&rsquo;s own reply, finishes playing before the
        call ends, and the agent never says anything after it.
      </div>
    );

  if (op.kind === "say")
    return (
      <Field label={<>Text (spoken verbatim — supports <code className={CODE}>{"{{tooldata.*}}"}</code> and <code className={CODE}>{"{{userdata.*}}"}</code>)</>}>
        <Input value={op.config.text} onChange={(e) => setCfg(op._id, { text: e.target.value })} placeholder="Welcome back, {{userdata.name}}!" aria-invalid={invalid("text")} />
      </Field>
    );

  if (op.kind === "generate_reply")
    return (
      <Field label="Reply instructions (the LLM phrases the response)">
        <Input value={op.config.instructions} onChange={(e) => setCfg(op._id, { instructions: e.target.value })} placeholder="Greet the caller and mention today's special offer." aria-invalid={invalid("instructions")} />
      </Field>
    );

  if (op.kind === "add_message")
    return (
      <Field label="System message to add to chat context (templated, no reply triggered)">
        <Input value={op.config.text} onChange={(e) => setCfg(op._id, { text: e.target.value })} placeholder="The caller is asking about order {{userdata.order_id}}." className="font-mono" aria-invalid={invalid("text")} />
      </Field>
    );

  if (op.kind === "handoff") {
    const handoffContext = op.config.context ?? "transcript";
    /* The API takes exactly one target: a stored agent, or a member of the team
       the call runs, by name. `agent_name` being present is the second form. */
    const toMember = op.config.agent_name != null;
    return (
      <div className="grid gap-2.5">
        <div className="flex items-end gap-2">
          <Field label="Hand off to" className="flex-1">
            <Select
              value={toMember ? TEAM_MEMBER : (op.config.target_agent_id ?? "")}
              onChange={(e) =>
                setCfg(
                  op._id,
                  e.target.value === TEAM_MEMBER
                    ? { target_agent_id: null, agent_name: op.config.agent_name ?? "" }
                    : { target_agent_id: e.target.value, agent_name: null },
                )
              }
              aria-invalid={invalid("target_agent_id")}
            >
              <option value="">— pick an agent —</option>
              <option value={TEAM_MEMBER}>A team member, by name</option>
              {(agents || []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.config.name || a.id}{a.published_version ? "" : " (not published)"}
                </option>
              ))}
            </Select>
          </Field>
          {toMember && (
            <Field label="Member name" className="flex-1">
              <Input
                value={op.config.agent_name ?? ""}
                onChange={(e) => setCfg(op._id, { agent_name: e.target.value })}
                placeholder="Billing"
                aria-invalid={invalid("agent_name")}
              />
            </Field>
          )}
          <Field label="Context passed" className="w-[200px] flex-none">
            {/* Switching the policy clears what the new one does not accept —
                the API rejects a stale field rather than ignoring it. */}
            <Select
              value={handoffContext}
              onChange={(e) =>
                setCfg(op._id, {
                  context: e.target.value,
                  summary: e.target.value === "summary" ? (op.config.summary ?? "") : "",
                  recent_turns: e.target.value === "transcript" ? null : (op.config.recent_turns ?? null),
                })
              }
            >
              <option value="transcript">everything said so far</option>
              <option value="summary">a summary of it</option>
              <option value="none">nothing</option>
            </Select>
          </Field>
          {handoffContext !== "transcript" && (
            <Field label="Recent turns" className="w-[120px] flex-none">
              <Input
                type="number"
                min={1}
                max={10}
                value={op.config.recent_turns == null ? "" : String(op.config.recent_turns)}
                /* Empty means the policy's own answer: two turns under
                   `summary`, where the tail is sized but never switched off,
                   and none at all under `nothing`. */
                placeholder={handoffContext === "summary" ? "2" : "none"}
                onChange={(e) => setCfg(op._id, { recent_turns: e.target.value ? Number(e.target.value) : null })}
              />
            </Field>
          )}
        </div>
        {handoffContext === "summary" && (
          <Field
            label="Summary handed to the target"
            hint="Text handed across, not a second LLM call. Point it at where the summary comes from — a tool argument the model filled, something an earlier operation published, or fixed prose."
          >
            <Input value={op.config.summary ?? ""} onChange={(e) => setCfg(op._id, { summary: e.target.value })} placeholder="{{args.summary}}" className="font-mono" aria-invalid={invalid("summary")} />
          </Field>
        )}
        <Field label="Transfer message (optional — spoken while connecting)">
          <Input value={op.config.message ?? ""} onChange={(e) => setCfg(op._id, { message: e.target.value })} placeholder="One moment — connecting you to billing." />
        </Field>
        <div className="text-[13px] leading-5 text-muted">
          {toMember
            ? "Resolves only on a call whose team has a member by this name. "
            : "The target agent runs its own prompt, tools and voice. It must be published. "}
          The message always finishes playing before the target takes over, so it is never talked
          over.
        </div>
      </div>
    );
  }

  if (op.kind === "transfer") {
    const mode = op.config.mode ?? "cold";
    const chosenMode = TRANSFER_MODES.find((m) => m.value === mode);
    return (
      <div className="grid gap-2.5">
        <Field label="How the handover happens">
          <Select value={mode} onChange={(e) => setCfg(op._id, { mode: e.target.value })}>
            {TRANSFER_MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </Select>
        </Field>
        {/* Described by what the caller lives through, not by which SIP verb
            runs — that is the platform's choice and never the builder's. */}
        {chosenMode && (
          <p className="text-[13px] leading-5 text-muted">{chosenMode.caller}</p>
        )}
        <div className="flex items-end gap-2">
          <Field label="Transfer to (a phone number, in full international format)" className="flex-1">
            <Input
              value={op.config.destination}
              onChange={(e) => setCfg(op._id, { destination: e.target.value })}
              placeholder="+14155550101"
              className="font-mono"
              aria-invalid={invalid("destination")}
            />
          </Field>
          <Field label={`Ring for (seconds, ${TRANSFER_RINGING_TIMEOUT_MIN}–${TRANSFER_RINGING_TIMEOUT_MAX})`} className="w-[190px] flex-none">
            <Input
              type="number"
              min={TRANSFER_RINGING_TIMEOUT_MIN}
              max={TRANSFER_RINGING_TIMEOUT_MAX}
              value={inputValue(op.config.ringing_timeout, TRANSFER_RINGING_TIMEOUT_DEFAULT)}
              onChange={(e) =>
                setCfg(op._id, { ringing_timeout: Number(e.target.value || TRANSFER_RINGING_TIMEOUT_DEFAULT) })
              }
            />
          </Field>
          <Field label="If it fails" className="w-[240px] flex-none">
            <Select
              value={op.config.on_failure ?? "continue"}
              onChange={(e) => setCfg(op._id, { on_failure: e.target.value })}
            >
              <option value="continue">carry on with the agent</option>
              <option value="end_call">end the call</option>
            </Select>
          </Field>
        </div>
        {/* The number's own problem is named once, in the card's gap list above —
            this section keeps only the hints that are advice rather than errors. */}
        {!spokenBefore && (
          /* A hint, not an error: a builder whose preceding `generate_reply`
             already covers it is not wrong, and neither is one whose agent
             prompt tells the model to announce the handover itself. */
          <p className="text-[13px] leading-5 text-warn">
            {mode === "warm"
              ? "Nothing before this speaks, so the caller is put on hold with no warning. Add a Say above it — “Let me speak to a colleague and bring you in.”"
              : "Nothing before this speaks, so the caller hears the agent stop and a stranger answer. Add a Say above it — “Connecting you to someone who can help.”"}
          </p>
        )}
        <div className="text-[13px] leading-5 text-muted">
          Phone calls only. The number is fixed here at publish and is never chosen by the model.
          The agent finishes whatever it is saying before it starts, and once the caller is through
          this call ends here — its duration and recording cover only the part the agent was on.
          {mode === "warm" &&
            " The briefing is not recorded either; it is kept on the call’s timeline so you can review what was said about a caller."}
        </div>
      </div>
    );
  }

  if (op.kind === "send_dtmf") {
    const digits = op.config.digits.trim();
    const pauses = [...digits].filter((key) => key === "w" || key === "W").length;
    return (
      <div className="grid gap-2.5">
        <Field label="Keys to press">
          <Input
            value={op.config.digits}
            onChange={(e) => setCfg(op._id, { digits: e.target.value })}
            placeholder="2w{{userdata.account_number}}#"
            className="font-mono"
            aria-invalid={invalid("digits")}
          />
        </Field>
        {/* Written as what the far end will hear rather than as a legend of
            allowed characters: the pause keys are the only part of this that is
            not self-evident, and they are the part that makes a real menu
            navigable. */}
        <p className="text-[13px] leading-5 text-muted">
          <code className={CODE}>0-9</code>, <code className={CODE}>*</code>,{" "}
          <code className={CODE}>#</code> and <code className={CODE}>A-D</code>. Add{" "}
          <code className={CODE}>w</code> to wait half a second and{" "}
          <code className={CODE}>W</code> for a full one — “press 2, wait, then the account
          number” is <code className={CODE}>2w{"{{userdata.account_number}}"}</code>.
        </p>
        {/* Only for a literal. A value carrying `{{args.pin}}` has no length
            until the call runs, and counting its braces as keys would put a
            confident wrong number under the field — "19 keys" for four. */}
        {pauses > 0 && !digits.includes("{{") && (
          <p className="text-[13px] leading-5 text-muted">
            {digits.replace(/[wW]/g, "").length} key
            {digits.replace(/[wW]/g, "").length === 1 ? "" : "s"}, {pauses} pause
            {pauses === 1 ? "" : "s"} — about{" "}
            {(
              digits.replace(/[wW]/g, "").length * 0.3 +
              [...digits].reduce((total, key) => total + (key === "w" ? 0.5 : key === "W" ? 1 : 0), 0)
            ).toFixed(1)}
            s in all.
          </p>
        )}
        <div className="text-[13px] leading-5 text-muted">
          Phone calls, and media streams whose platform can emit keypad tones — Twilio, Exotel
          and Vonage streams cannot, and the operation says so rather than doing nothing. Unlike
          a transfer destination, these digits <em>are</em> templated, because an account number
          only exists once the call is running.
        </div>
      </div>
    );
  }

  if (op.kind === "frontend_rpc")
    return (
      <div className="grid gap-2.5">
        <Field label="Client method (the handler name your web UI registers)">
          <Input value={op.config.method} onChange={(e) => setCfg(op._id, { method: e.target.value })} placeholder="getFormState" className="font-mono" aria-invalid={invalid("method")} />
        </Field>
        <Field label="Payload (JSON, templated — sent to the client)">
          <Textarea
            value={op.payloadText}
            onChange={(e) => setNode(op._id, { payloadText: e.target.value })}
            placeholder={'{ "items": "{{userdata.cart}}", "total": "{{userdata.total}}" }'}
            className="min-h-[80px] font-mono"
          />
        </Field>
        <Field label="Timeout (seconds, max 60)" className="w-[180px] flex-none">
          <Input type="number" min={0.5} max={60} step={0.5} value={inputValue(op.config.timeout, 5)} onChange={(e) => setCfg(op._id, { timeout: Number(e.target.value || 5) })} />
        </Field>
        <div className="text-[13px] leading-5 text-muted">
          {op.background_execution
            ? "Fire-and-forget call to the connected web client. The tool does not wait for the response and cannot publish fields."
            : "Awaited request/response call to the connected web client. The returned JSON becomes this tool operation's output and can be saved with publish fields."}
        </div>
      </div>
    );

  return <div className="text-[13px] text-muted">No config.</div>;
}

/** The editor's frame — header, form cards, right-hand pane — at the same
    sizes, so nothing moves when the tool arrives. */
function ToolEditorSkeleton() {
  return (
    <AppShell>
      <div className="min-h-screen bg-subtle lg:h-screen" aria-busy="true" aria-label="Loading tool">
        <section className="flex min-h-screen w-full flex-col bg-white lg:h-screen lg:min-h-0">
          <div className="border-b border-line px-6 py-3">
            <div className="flex items-center gap-3">
              <Skeleton className="h-9 w-9 flex-none rounded-lg" />
              <Skeleton className="h-6 w-[200px]" />
              <div className="ml-auto hidden items-center gap-2 md:flex">
                {["w-[74px]", "w-[60px]", "w-[80px]", "w-[56px]", "w-[76px]"].map((w) => (
                  <Skeleton key={w} className={cn("h-9 rounded-lg", w)} />
                ))}
              </div>
            </div>
            <div className="mt-3 flex items-center gap-2">
              <Skeleton className="h-6 w-[88px] rounded-full" />
              <Skeleton className="h-6 w-[104px] rounded-full" />
            </div>
          </div>
          <div className="min-h-0 flex-1 px-6 py-5 lg:overflow-hidden">
            <ResizableToolEditorLayout
              form={(
                <div className="flex min-w-0 flex-col gap-4">
                  {["h-[120px]", "h-[60px]", "h-[220px]"].map((h) => (
                    <section key={h} className={cn(SURFACE, "flex flex-col gap-4 px-5 py-4")}>
                      <div className="flex flex-col gap-1.5">
                        <Skeleton className="h-4 w-[120px]" />
                        <Skeleton className="h-3 w-[260px]" />
                      </div>
                      <Skeleton className={cn("w-full rounded-lg", h)} />
                    </section>
                  ))}
                </div>
              )}
              aside={(
                <div className="flex min-w-0 flex-col gap-3 lg:h-full lg:min-h-0">
                  <Skeleton className="h-11 w-full flex-none rounded-lg" />
                  <div className="flex-1" />
                  <Skeleton className="h-11 w-full flex-none rounded-lg" />
                </div>
              )}
            />
          </div>
        </section>
      </div>
    </AppShell>
  );
}

// useSearchParams() suspends during prerender, so the exported page provides the
// boundary and ToolEditorInner does the work. Without this, `next build` fails with
// "useSearchParams() should be wrapped in a suspense boundary".
export default function ToolEditorPage(): JSX.Element {
  return (
    <Suspense fallback={<ToolEditorSkeleton />}>
      <ToolEditorInner />
    </Suspense>
  );
}
