"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Track } from "livekit-client";
import type { AgentConfig, JsonObject, VarDeclaration } from "@talqing/sdk";
import {
  TalqingSessionProvider,
  type TalqingCallOptions,
  useAgent,
  useSessionMessages,
  useTalqingConnection,
  useTalqingFrontendRpcs,
  useTalqingImages,
  useTalqingScreenShare,
  useTalqingSession,
  type TalqingSentImage,
} from "@talqing/react";
import { talqing } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { KVRows } from "./KVRows";
import {
  SessionVarFields,
  missingVars,
  missingVarsSentence,
  suppliedVars,
} from "./SessionVars";
import { ActionCards } from "./FrontendActions";
import { AttachButton, NO_VISION_HINT, useImageDropzone } from "./ImageAttach";
import {
  Button,
  ChatBubble,
  ImageLightbox,
  Label,
  Modal,
  SessionStatus,
  Skeleton,
  Transcript,
} from "./ui";
import { StartMediaButton, useLocalParticipant, VideoTrack } from "@livekit/components-react";

// `video`: the agent has an Anam avatar — render its video track in
// a stage above the transcript. The avatar worker's tracks are merged into
// useAgent() (lk.publish_on_behalf), so no participant plumbing is needed here.
//
// Nothing LiveKit is mounted until the user asks for a call. useSession() fetches
// a token from its mount effect to warm the connection, and our token endpoint
// creates the conversation as a side effect — so merely *rendering* this panel
// used to file a conversation nobody started (one per mount; two under dev
// StrictMode). The idle state is inert markup.
export default function WebCall({
  agentId,
  agentVersion = null,
  agent = null,
  vars,
  video = false,
  vision,
  screenShare = false,
  disabledReason = null,
  beforeStart,
}: {
  /** The stored agent to call, or null when `agent` carries a whole definition
   *  for this call and nothing else. */
  agentId: string | null;
  /** Which version of `agentId` to run; null runs the published one. */
  agentVersion?: TalqingCallOptions["agent_version"];
  /** An agent defined for this call and stored nowhere — how the task editor
   *  puts a caller in front of a task without inventing an agent to own it. */
  agent?: AgentConfig | null;
  /** What the version this call runs declares — any other version's list
   *  would offer one set of fields while the API enforces another. */
  vars: VarDeclaration[];
  video?: boolean;
  /** Whether the model this call runs can read an image. There is no setting
   *  for it — it is a property of the model. */
  vision: boolean;
  /** Whether the agent this call runs watches the caller's screen. The control is
   *  shown for the whole call rather than revealed when the agent asks: a
   *  button that appears mid-sentence is one the person hunts for while being
   *  talked to. */
  screenShare?: boolean;
  /** Why this person cannot place a test call, or null when they can. Says it
   *  on the button instead of letting the click come back a 403 — minting a
   *  token now needs the editor role, because these calls run an arbitrary
   *  prompt on an arbitrary model, paid for with the workspace's own keys. */
  disabledReason?: string | null;
  /** Runs before the setup dialog opens, and the call goes no further when it
   *  resolves false — how the agent editor saves the draft it is about to test. */
  beforeStart?: () => Promise<boolean>;
}) {
  // 0 = never started; N = the Nth call. It is also the `key` below, so every
  // call gets a fresh LiveCall and with it a fresh Room.
  //
  // A Room is ONE connection, and reconnecting a disconnected one does not work:
  // `@livekit/components-react` registers its `lk.transcription` text-stream
  // handler when the room connects, but only unregisters it when the last
  // subscriber unsubscribes — which never happens here, because the surface
  // stays mounted to show "Call again". The second connect then hits
  // `registerTextStreamHandler`'s duplicate guard and throws
  // "A text stream handler for topic "lk.transcription" has already been set."
  // Scoping the Room to the call it belongs to fixes that by construction, and
  // keeps every other per-room handler (RPC, byte streams) scoped too.
  const [call, setCall] = useState(0);
  // Pinned across every call on this page, because the Room is not. The API
  // mints a single-use key for a call that arrives without one, so without this
  // every test call would be a different person — and an agent set to carry past
  // conversations would never have one to carry. Filled on the first call below,
  // not read back off the token: the token response says how to join this call
  // and which call it is, and the key is something this page decided.
  const contactKey = useRef("");
  // The rows the setup dialog edits. They outlive the dialog and every call, so
  // a second call reopens on what the first one ran with instead of a blank
  // sheet. Opens on one blank row: typing a variable should not cost a click.
  const [rows, setRows] = useState<JsonObject>({ "": "" });
  // The declared variables the dialog has values for, outliving it the same way.
  const [varValues, setVarValues] = useState<Record<string, string>>({});
  const [setup, setSetup] = useState(false);
  // What the *running* call carries, frozen when it was placed — editing the
  // rows for the next call must not rewrite the token source under this one.
  const [userdata, setUserdata] = useState<JsonObject | null>(null);
  const [sessionVars, setSessionVars] = useState<Record<string, string> | null>(null);
  const [preparing, setPreparing] = useState(false);

  async function openSetup() {
    if (beforeStart) {
      setPreparing(true);
      const ready = await beforeStart();
      setPreparing(false);
      if (!ready) return;
    }
    setSetup(true);
  }

  function startCall() {
    contactKey.current ||= `web-test:${crypto.randomUUID()}`;
    // A half-typed row is a row the author has not finished, not an error to
    // throw back at them — the call goes out without it.
    const seed = Object.fromEntries(
      Object.entries(rows)
        .map(([k, v]) => [k.trim(), v] as const)
        .filter(([k]) => k),
    );
    setUserdata(Object.keys(seed).length ? seed : null);
    const supplied = suppliedVars(vars, varValues);
    setSessionVars(Object.keys(supplied).length ? supplied : null);
    setSetup(false);
    setCall((n) => n + 1);
  }

  return (
    <>
      {call === 0 ? (
        <IdleControls
          onStart={() => void openSetup()}
          video={video}
          preparing={preparing}
          disabledReason={disabledReason}
        />
      ) : (
        <LiveCall
          key={call}
          agentId={agentId}
          agentVersion={agentVersion}
          agent={agent}
          video={video}
          vision={vision}
          screenShare={screenShare}
          userdata={userdata}
          sessionVars={sessionVars}
          contactKey={contactKey}
          onCallAgain={() => void openSetup()}
        />
      )}
      {setup && (
        <CallSetup
          video={video}
          rows={rows}
          onChange={setRows}
          vars={vars}
          varValues={varValues}
          onVarsChange={setVarValues}
          onStart={startCall}
          onClose={() => setSetup(false)}
        />
      )}
    </>
  );
}

// Everything the caller can decide before a web call: the two bags the session
// starts from, the same two a phone call carries.
function CallSetup({
  video,
  rows,
  onChange,
  vars,
  varValues,
  onVarsChange,
  onStart,
  onClose,
}: {
  video: boolean;
  rows: JsonObject;
  onChange: (rows: JsonObject) => void;
  vars: VarDeclaration[];
  varValues: Record<string, string>;
  onVarsChange: (values: Record<string, string>) => void;
  onStart: () => void;
  onClose: () => void;
}) {
  // The API refuses the token for these anyway; finding out after a click, and
  // after the panel has dropped into its connecting state, is worse.
  const missing = missingVars(vars, varValues);
  return (
    <Modal
      title={video ? "Start a video test call" : "Start a test call"}
      sub="Your browser mic connects straight to the agent — no phone number is dialled."
      width="max-w-[520px]"
      onClose={onClose}
      footer={
        <>
          {missing.length > 0 && (
            <span className="mr-auto text-[12.5px] leading-5 text-warn">
              {missingVarsSentence(missing)}
            </span>
          )}
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="web-call-setup" disabled={missing.length > 0}>
            <MicIcon />
            Start call
          </Button>
        </>
      }
    >
      <form
        id="web-call-setup"
        className="grid gap-4 pb-6"
        onSubmit={(e) => {
          e.preventDefault();
          onStart();
        }}
      >
        {vars.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <Label>Variables</Label>
            <SessionVarFields
              declared={vars}
              values={varValues}
              onChange={onVarsChange}
              idPrefix="web-call-var"
            />
            <p className="text-[13px] leading-5 text-muted">
              What this call knows about the deployment rather than about the person — an API host,
              a reseller code. The agent reads them as{" "}
              <code className="font-mono text-[12.5px] text-ink">{"{{vars.name}}"}</code>, they are
              gone when the call ends, and nothing is kept on anybody&rsquo;s record.
            </p>
          </div>
        )}
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-3">
            <Label optional>User data</Label>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => onChange({ ...rows, "": "" })}
            >
              + variable
            </Button>
          </div>
          <KVRows obj={rows} onChange={onChange} className="" />
          <p className="text-[13px] leading-5 text-muted">
            What the call already knows about the person on it. The agent reads it as{" "}
            <code className="font-mono text-[12.5px] text-ink">{"{{userdata.key}}"}</code> in its
            prompt and greeting, and its tools receive it too.
          </p>
        </div>
      </form>
    </Modal>
  );
}

// One mount per call, remounted by `key` for the next one — see WebCall.
function LiveCall({
  agentId,
  agentVersion,
  agent,
  video,
  vision,
  screenShare,
  userdata,
  sessionVars,
  contactKey,
  onCallAgain,
}: {
  agentId: string | null;
  agentVersion: TalqingCallOptions["agent_version"];
  agent: AgentConfig | null;
  video: boolean;
  vision: boolean;
  screenShare: boolean;
  /** Seeds the session's state; frozen by WebCall when this call was placed. */
  userdata: JsonObject | null;
  /** `{{vars.*}}` for this call; frozen with the userdata above. */
  sessionVars: Record<string, string> | null;
  /** Owned by WebCall so it outlives this mount, and with it the contact. */
  contactKey: React.MutableRefObject<string>;
  onCallAgain: () => void;
}) {
  // The `client` auth branch: the dashboard is same-origin with the API, signed
  // in as a real editor, and a static export with no server route to mint from.
  // Nothing a tenant should copy — see the SDK README — but the branch's
  // intended use, and the only one that hands the warnings back to a person.
  //
  // Memoized because the hook rebuilds its token source whenever `callOptions`
  // changes identity, and an object literal is a new identity every render.
  const callOptions = useMemo(
    () => ({
      contact_key: contactKey.current,
      agent_version: agentVersion,
      userdata,
      vars: sessionVars,
      agent,
    }),
    [contactKey, agentVersion, userdata, sessionVars, agent],
  );
  const session = useTalqingSession({ client: talqing, agentId, callOptions });

  return (
    // Renders the room's audio for us — the avatar's audio is room audio like
    // any other; never add a separate AudioTrack for it (double audio).
    <TalqingSessionProvider session={session}>
      <CallSurface
        video={video}
        vision={vision}
        screenShare={screenShare}
        userdata={userdata}
        sessionVars={sessionVars}
        warnings={session.warnings}
        onCallAgain={onCallAgain}
      />
    </TalqingSessionProvider>
  );
}

// The no-call-in-progress state, composed like the "Not published yet" state one
// branch over on the page: same kind of moment, an inert panel asking for one
// click, in the same slot. Used before the first call and again after one ends,
// so hanging up never drops the panel back to a bare button in a grey box.
function CallHero({
  video,
  again,
  onStart,
  preparing = false,
  err,
  disabledReason,
}: {
  video: boolean;
  /** A call already happened — this is "go again", not "here's what this is". */
  again?: boolean;
  onStart: () => void;
  /** `beforeStart` is running. */
  preparing?: boolean;
  err?: string;
  disabledReason?: string | null;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-10 text-center">
      <span className="mb-3 grid h-12 w-12 place-items-center rounded-xl border border-line-2 bg-surface text-ink" aria-hidden>
        <MicIcon className="h-6 w-6" />
      </span>
      <h3 className="mb-1 text-[17px] font-semibold leading-6 text-ink">
        {again ? "Call ended" : video ? "Start a video test call" : "Start a test call"}
      </h3>
      <p className="mb-4 max-w-[340px] text-[13px] leading-relaxed text-muted">
        {again
          ? "Start another whenever you're ready."
          : "Your browser mic connects straight to the agent — no phone number is dialled."}
      </p>
      <Button
        onClick={onStart}
        disabled={preparing || Boolean(disabledReason)}
        title={disabledReason ?? undefined}
      >
        <MicIcon />
        {preparing ? "Starting…" : again ? "Call again" : "Test call"}
      </Button>
      {disabledReason && (
        <div className="mt-3 max-w-[340px] text-[13px] leading-5 text-muted">{disabledReason}</div>
      )}
      {err && (
        <div className="mt-3 max-w-[460px] whitespace-pre-line text-[13.5px] leading-5 text-danger">
          {err}
        </div>
      )}
    </div>
  );
}

// Pre-session twin of CallHero: mounting the LiveKit session is what the click
// does, so the button cannot live inside the session provider.
function IdleControls({
  onStart,
  video,
  preparing,
  disabledReason,
}: {
  onStart: () => void;
  video: boolean;
  preparing: boolean;
  disabledReason?: string | null;
}) {
  return (
    <CallHero video={video} onStart={onStart} preparing={preparing} disabledReason={disabledReason} />
  );
}

function MicIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 3a4 4 0 0 1 4 4v3a4 4 0 0 1-8 0V7a4 4 0 0 1 4-4ZM5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}

function CallSurface({
  video,
  vision,
  screenShare,
  userdata,
  sessionVars,
  warnings,
  onCallAgain,
}: {
  video: boolean;
  vision: boolean;
  screenShare: boolean;
  /** Shown while live, so a personalized greeting can be read against its input. */
  userdata: JsonObject | null;
  /** Shown beside it, for the same reason and with the same shape. */
  sessionVars: Record<string, string> | null;
  /** What resolving the plan complained about, from the token response. Nothing
   *  stores these, so this panel is the only place they are ever seen. */
  warnings: string[];
  /** Remounts the whole call — this Room belongs to the call that just ended. */
  onCallAgain: () => void;
}) {
  const { isConnected, start, end, room } = useTalqingConnection();
  // frontend actions — shared with the text surface. Registered
  // here rather than a level up because every SDK hook that is not handed a Room
  // reads the session from context, which only exists inside the provider.
  const { actions, clear: clearActions } = useTalqingFrontendRpcs();
  const agent = useAgent();
  const { messages } = useSessionMessages();
  const [err, setErr] = useState("");
  // Set when the user hangs up, so the surface can go back to the hero instead
  // of a stranded button. Cleared by the next call.
  const [ended, setEnded] = useState(false);

  function call() {
    setErr("");
    setEnded(false);
    // The token mint is the first thing `start` does, so an API refusal — an
    // unpublished agent, a workspace with no credit left — surfaces here. Read
    // it through `apiErrorMessage` so the sentence the API wrote, which already
    // says what to do about it, is the sentence the caller sees — with every
    // reason under it, since a draft that cannot run is refused with a list.
    start({ tracks: { microphone: { enabled: true } } }).catch((e: unknown) =>
      setErr([apiErrorMessage(e, "connection failed"), ...apiErrorList(e)].join("\n")),
    );
  }

  // This component mounts because the user pressed "Test call" in IdleControls,
  // so place the call. The ref survives StrictMode's double effect invocation —
  // two start() calls would connect to two different rooms.
  const autoStarted = useRef(false);
  useEffect(() => {
    if (autoStarted.current) return;
    autoStarted.current = true;
    call();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- mount only; call() is re-created each render

  // On the way to a call: from mount until the room is up, and again on every
  // reconnect. Covers the first frame too, before the mount effect has run —
  // otherwise the surface paints "ready" for one frame under a call it is
  // already placing.
  const connecting = !isConnected && !ended && !err;

  // The room being up is not the agent being there. LiveKit walks the client
  // through connecting ➡️ pre-connect-buffering ➡️ initializing before the agent
  // is on the call, and the *room* reports connected from the second step on. A
  // surface keyed off the room alone therefore says "live — speak into your mic"
  // to someone nobody is listening to yet — and pre-connect buffering means what
  // they say is banked in silence rather than answered. So the whole surface
  // stays in its loading state until the agent itself lands.
  const joining =
    isConnected &&
    !ended &&
    (agent.state === "connecting" ||
      agent.state === "pre-connect-buffering" ||
      agent.state === "initializing");
  // The room outlives an agent that never arrived (or timed out on the way), and
  // that reads as a healthy call unless it is called out.
  const agentFailed = agent.state === "failed" && !ended;
  const live = isConnected && !joining && !agentFailed;

  // Owns the topic, the size rules, the browser downscale and the
  // `talqing.image_result` ack — the same hook a customer building their own
  // call surface reaches for.
  const { send, sent } = useTalqingImages(room);
  const attach = useCallback(
    async (files: File[]) => {
      setErr("");
      for (const file of files) {
        try {
          await send(file);
        } catch (error: unknown) {
          // The dropzone's own refusals — wrong type, too big, undecodable —
          // land here. Instant, and no round trip spent to be told.
          setErr(error instanceof Error ? error.message : "That image could not be sent");
          return;
        }
      }
    },
    [send],
  );
  const { dragging, dropProps } = useImageDropzone({ enabled: live && vision, onFiles: attach });

  // Hanging up returns to the hero. The message guard is belt-and-braces: today
  // useSessionMessages() empties on disconnect, so this always fires — but if a
  // transcript ever did survive, it should stay on screen rather than be
  // replaced by a "call again" card.
  //
  // Images are deliberately NOT part of that test, even though they are ours and
  // would survive: they are the only thing that would, so the panel would keep a
  // photo with the conversation it belonged to gone from around it, under a
  // receipt promising an agent that has hung up will look at it on the next
  // turn. The call detail page is where a finished call is read.
  if (ended && messages.length === 0) {
    // `onCallAgain`, not `call()`: this Room has already carried one call, and
    // the framework's per-room stream handlers do not survive a second connect.
    return <CallHero video={video} again onStart={onCallAgain} err={err} />;
  }

  return (
    <div className={cn("relative grid gap-4 p-4", dragging && "bg-info/[0.04]")} {...dropProps}>
      {dragging && (
        <div className="pointer-events-none absolute inset-2 z-10 grid place-items-center rounded-xl border-2 border-dashed border-info/40">
          <span className="rounded-lg bg-surface px-3 py-1.5 text-[13px] font-medium text-ink shadow-sm">
            Drop to send to the agent
          </span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        {isConnected ? (
          <Button
            variant="danger"
            onClick={() => {
              end();
              setEnded(true);
            }}
          >
            End call
          </Button>
        ) : (
          // `onCallAgain`, not `call()`: a second call is a second Room, and
          // this token belongs to the one that just ended.
          <Button onClick={onCallAgain} disabled={connecting}>
            <MicIcon />
            {connecting ? "Connecting…" : "Call again"}
          </Button>
        )}
        <AttachButton
          onFiles={attach}
          label="Send an image to the agent"
          disabledReason={
            !vision ? NO_VISION_HINT : !live ? "Wait for the agent to join the call." : null
          }
        />
        {screenShare && <ScreenShareButton live={live} />}
        <SessionStatus tone={live ? "live" : connecting || joining ? "busy" : "idle"}>
          {live
            ? "live — speak into your mic"
            : agentFailed
              ? "the agent didn't join"
              : connecting
                ? "connecting…"
                : joining
                  ? "waiting for the agent…"
                  : "ready"}
        </SessionStatus>
        {/* The raw SDK state is worth having on a test surface, but only once it
            describes the agent's turn rather than its boot sequence. */}
        {live && <span className="font-mono text-[12px] text-faint">agent: {agent.state}</span>}
        {userdata && (
          <span className="font-mono text-[12px] text-faint">
            userdata: {Object.keys(userdata).join(", ")}
          </span>
        )}
        {sessionVars && (
          <span className="font-mono text-[12px] text-faint">
            vars: {Object.keys(sessionVars).join(", ")}
          </span>
        )}
        {/* auto-shows only if the browser blocks audio autoplay */}
        <StartMediaButton label="🔊 Enable audio" />
      </div>
      {err && <div className="whitespace-pre-line text-[13.5px] leading-5 text-danger">{err}</div>}
      {/* Non-blocking, because the call is running: these are about the PLAN
          this call resolved to, not about this call. Nothing stores them — the
          token response is the only place they ever appear — so a panel that
          drops them silently swallows exactly the problems they exist to
          report. Same block as the publish dialog's warnings, without the
          dialog. */}
      {warnings.length > 0 && (
        <div className="grid gap-2 rounded-lg border border-warn/25 bg-warn/[0.05] p-3.5">
          <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-warn">
            Resolved with warnings
          </div>
          {warnings.map((warning) => (
            <div
              key={warning}
              className="text-[13px] leading-snug text-ink-soft [overflow-wrap:anywhere]"
            >
              {warning}
            </div>
          ))}
        </div>
      )}
      {agentFailed && (
        <div className="text-[13.5px] text-danger">
          The agent never joined this call
          {agent.failureReasons?.length ? `: ${agent.failureReasons.join(", ")}` : "."} End it and
          try again.
        </div>
      )}
      {video && <AvatarStage />}
      {screenShare && <ScreenShareStage />}
      <ActionCards actions={actions} onClear={clearActions} />
      {(connecting || joining) && messages.length === 0 && <JoiningTranscript />}
      {/* Images go in here, not in a strip of their own: on a call an image is
          sent the moment it is picked, so it is a message from that moment and
          reads as one — the same way the call detail renders it afterwards. */}
      <TranscriptView images={sent} />
    </div>
  );
}

/* Share screen. `useTalqingScreenShare` carries the four settings that decide
   whether the agent can read what is on it — one frame a second on h264, and
   `contentHint: "text"` — and the comments on TALQING_SCREEN_SHARE_CAPTURE say
   why each one is what it is. The picker needs a user gesture, so this must stay
   a button; an agent can ask for a screen but can never take one. */
function ScreenShareButton({ live }: { live: boolean }) {
  const { enabled, pending, start, stop } = useTalqingScreenShare();
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      disabled={!live || pending}
      onClick={() => void (enabled ? stop() : start())}
    >
      <ScreenIcon />
      {enabled ? "Stop sharing" : "Share screen"}
    </Button>
  );
}

/* What the agent is looking at, back at the person sharing it. People forget
   which window they picked, and on a call where the agent can see, that is the
   one thing they most need to be sure of. */
function ScreenShareStage() {
  const { localParticipant } = useLocalParticipant();
  const publication = localParticipant.getTrackPublication(Track.Source.ScreenShare);
  if (!publication?.track) return null;
  return (
    <div className="grid gap-1.5">
      <span className="text-[12px] font-medium text-muted">
        The agent sees this
      </span>
      <div className="flex max-h-[320px] items-center justify-center overflow-hidden rounded-lg border border-line-2 bg-ink">
        <VideoTrack trackRef={{ participant: localParticipant, publication, source: Track.Source.ScreenShare }} />
      </div>
    </div>
  );
}

function ScreenIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="2.5" y="4" width="19" height="12.5" rx="2" />
      <path d="M8.5 20h7M12 16.5V20" />
    </svg>
  );
}

function AvatarStage() {
  const { isConnected } = useTalqingConnection();
  const agent = useAgent();
  if (!isConnected) return null;
  return (
    // Square, centred, and the face fills it. Anam publishes 720x480, so at the
    // old panel-wide box the frame sat 3:2 against a much wider container and
    // left a black bar down each side. Cropping the sides to a square instead
    // puts the head where a video tile puts it, and costs only background.
    <div className="mx-auto aspect-square w-full max-w-[420px] overflow-hidden rounded-lg border border-line-2 bg-ink">
      {agent.cameraTrack ? (
        <VideoTrack trackRef={agent.cameraTrack} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full items-center justify-center gap-2.5 px-5 text-[13.5px] text-faint">
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-faint" aria-hidden />
          Avatar joining…
        </div>
      )}
    </div>
  );
}

// The transcript's own shape, shimmering. Between the click and the agent's
// greeting the surface has nothing in it, and an empty panel is the same picture
// as a call that quietly failed — so it shows the thing it is waiting for.
function JoiningTranscript() {
  return (
    <div className="flex flex-col gap-2.5 rounded-xl border border-line bg-surface p-3 shadow-sm" aria-hidden>
      <Skeleton className="h-3 w-12 rounded" />
      <Skeleton className="h-11 w-[46%] rounded-xl rounded-bl-md" />
    </div>
  );
}

/* One column, in the order things happened. The two sources have nothing in
   common but a clock: what was said arrives as LiveKit transcriptions, and what
   was attached is ours and never reaches them — the agent takes an image into
   its context without answering it, so there is no turn for it to ride on. Both
   carry epoch milliseconds, which is all the merge needs. */
type Entry =
  | { at: number; kind: "said"; id: string; mine: boolean; text: string }
  | { at: number; kind: "image"; image: TalqingSentImage };

function TranscriptView({ images }: { images: TalqingSentImage[] }) {
  const { messages } = useSessionMessages();
  const [zoomed, setZoomed] = useState<TalqingSentImage | null>(null);
  const entries = useMemo<Entry[]>(
    () =>
      [
        ...messages.map<Entry>((m) => ({
          at: m.timestamp,
          kind: "said",
          id: m.id,
          mine: m.from?.isLocal === true,
          text: m.message,
        })),
        ...images.map<Entry>((image) => ({ at: image.sentAt, kind: "image", image })),
      ].sort((a, b) => a.at - b.at),
    [messages, images],
  );
  if (!entries.length) return null;
  return (
    <>
      <Transcript className="max-h-[420px] rounded-xl border border-line bg-surface p-3 shadow-sm">
        {entries.map((entry) =>
          entry.kind === "said" ? (
            <ChatBubble
              key={entry.id}
              role={entry.mine ? "user" : "agent"}
              who={entry.mine ? "You" : "Agent"}
            >
              {entry.text}
            </ChatBubble>
          ) : (
            <SentImageBubble
              key={entry.image.streamId}
              image={entry.image}
              onZoom={() => setZoomed(entry.image)}
            />
          ),
        )}
      </Transcript>
      {zoomed && (
        <ImageLightbox src={zoomed.previewUrl} alt={zoomed.name} onClose={() => setZoomed(null)} />
      )}
    </>
  );
}

/* An image the caller sent, as the message it is. The receipt sits under the
   bubble rather than in it: the status colours are the legible-on-white end of
   each hue and a user bubble is near-black, and a delivery note under a message
   is where a reader already looks for one.

   There is one even on the happy path, because the agent deliberately says
   nothing when a photo lands — it answers on the caller's next turn — so silence
   is the expected response and cannot also serve as "it arrived". */
function SentImageBubble({ image, onZoom }: { image: TalqingSentImage; onZoom: () => void }) {
  const failed = image.status === "failed" || image.status === "unknown";
  return (
    <div className="flex flex-col items-end gap-1">
      <ChatBubble role="user" who="You">
        <button
          type="button"
          onClick={onZoom}
          title="See full size"
          className="block overflow-hidden rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
        >
          <img
            src={image.previewUrl}
            alt={image.name}
            className={cn(
              "block max-h-[200px] max-w-[220px] object-contain",
              image.status === "sending" && "opacity-70",
              failed && "opacity-45",
            )}
          />
        </button>
      </ChatBubble>
      <span
        className={cn(
          "max-w-[82%] text-right text-[11.5px] leading-4",
          !failed ? "text-faint" : image.status === "failed" ? "text-danger" : "text-warn",
        )}
      >
        {image.status === "sending"
          ? "Sending…"
          : image.status === "sent"
            ? "Delivered — the agent sees it on your next turn"
            : image.error}
      </span>
    </div>
  );
}
