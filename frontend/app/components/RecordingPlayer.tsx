"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RecordingResponse, RecordingState } from "@talqing/sdk";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { Button, Modal, Panel } from "./ui";

/* Recordings are stereo: the caller on the left channel, the agent on the right.
   Playing that as-is puts one voice in each ear, which is unpleasant for the
   thing people actually do here — listening back to a call to work out what went
   wrong. So the default sums both channels to mono, and the other two options
   isolate a side: "Agent" is how a TTS or interruption bug becomes audible,
   "Caller" is how a mishearing does. */
type Channel = "both" | "caller" | "agent";

const CHANNELS: { value: Channel; label: string; title: string }[] = [
  { value: "both", label: "Both", title: "Caller and agent together" },
  { value: "caller", label: "Caller", title: "Caller only (left channel)" },
  { value: "agent", label: "Agent", title: "Agent only (right channel)" },
];

/* One sentence per state, because "no player" for six different reasons is the
   difference between a user understanding their own settings and filing a
   ticket. `available` is the only state with media behind it. */
const EMPTY_STATE: Record<Exclude<RecordingState, "available">, string> = {
  none: "This call was not recorded — recording is off for this agent.",
  pending: "The call is still in progress. Its recording appears when the call ends.",
  expired: "This recording has been deleted under your organization's data retention policy.",
  consent_withdrawn: "The caller asked not to be recorded, so the audio was discarded.",
  deleted: "This recording was deleted.",
  failed: "This call's recording could not be saved.",
  /* Audio can never be in this state — a voice call always has audio. */
  not_shared: "Nobody shared their screen on this call.",
};

/* The screen video's own wording. Only three of its states ever differ from the
   audio's, but those three are the ones that matter: "nobody shared" is the
   normal outcome and must not read as a fault, and "off for this agent" has to
   name the setting the reader would go and change. */
const SCREEN_EMPTY_STATE: Partial<Record<Exclude<RecordingState, "available">, string>> = {
  none: "Screen recording is off for this agent.",
  not_shared: "Nobody shared their screen on this call.",
  failed: "This call's screen recording could not be saved.",
};

function fmtBytes(bytes: number | null | undefined): string | null {
  if (typeof bytes !== "number") return null;
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function fmtClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Wall-clock seconds a recording's first frame was written at, or null. */
function originS(recording: RecordingResponse): number | null {
  const at = recording.started_at ? Date.parse(recording.started_at) : NaN;
  return Number.isNaN(at) ? null : at / 1000;
}

export function RecordingPlayer({
  sessionId,
  recording,
  screenRecording,
  onDeleted,
  onPosition,
  seekTo,
}: {
  sessionId: string;
  recording: RecordingResponse;
  /* The screen the caller shared, when the agent watched one and the author
     asked for it to be kept. A second file of the same call, played against the
     same transport below — see `screenOffsetS`. */
  screenRecording: RecordingResponse;
  onDeleted: () => void;
  /* Where the playhead is, so the transcript can highlight the turn being
     spoken. Null while nothing is loaded. */
  onPosition?: (seconds: number | null) => void;
  /* A seek asked for from outside — clicking a turn. Carries a nonce because
     asking for the same second twice is a real request, and a bare number
     would look unchanged. */
  seekTo?: { at: number; nonce: number } | null;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  /* The screen and the transport that drives it, as one element, because that
     is what goes fullscreen. Fullscreening the <video> alone is the obvious
     move and the wrong one: a video with no `controls` has none in fullscreen
     either, so the reader gets a big picture and no way to pause or scrub the
     moment they want — which is the only reason they made it big. */
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [channel, setChannel] = useState<Channel>("both");
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(
    recording.duration_s ?? screenRecording.duration_s ?? 0,
  );
  const [deleting, setDeleting] = useState(false);
  /* Deleting is immediate and cannot be undone — the objects go and the rows are
     stamped `deleted`. A one-click danger button sitting beside "Download" is
     the wrong shape for that, and it is the only irreversible control on this
     page. */
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  /* The media comes straight from object storage through short-lived signed
     URLs on the call detail, not through the API. That is what makes seeking
     cheap: the bucket serves `Range` natively, so the browser fetches the few
     seconds around the playhead instead of the whole call before the first
     sound. `crossOrigin` is what lets the Web Audio graph below read it — see
     `buildGraph`. */
  const src = recording.url ?? null;
  const screenSrc = screenRecording.url ?? null;

  /* One transport for both files: the audio drives, and the video follows. When
     there is no audio — recording off, screen recording on — the video drives
     itself and the offset below is zero by construction.

     The two files do not start at the same instant — the audio's origin is its
     first recorded frame, the video's is the writer's first tick — so lining
     them up is one subtraction of their origins. Everything else on this page
     measures from whichever origin is driving, which is why the transcript's
     seeks need no adjustment at all. */
  const clockIsAudio = Boolean(src);
  const clockRef = clockIsAudio ? audioRef : videoRef;
  const screenOffsetS = useMemo(() => {
    if (!clockIsAudio) return 0;
    const audioOrigin = originS(recording);
    const screenOrigin = originS(screenRecording);
    if (audioOrigin === null || screenOrigin === null) return 0;
    return audioOrigin - screenOrigin;
  }, [clockIsAudio, recording, screenRecording]);

  /* Nudged rather than driven frame by frame: the video is muted and carries one
     frame a second, so a fifth of a second of drift is invisible and correcting
     it on every `timeupdate` would fight the browser's own playback. */
  const syncScreen = useCallback(
    (at: number) => {
      const el = videoRef.current;
      if (!el || !clockIsAudio) return;
      const target = Math.max(0, at + screenOffsetS);
      if (Math.abs(el.currentTime - target) > 0.4) el.currentTime = target;
    },
    [clockIsAudio, screenOffsetS],
  );

  /* Web Audio graph: split the stereo pair, gate each side, merge back to two
     identical channels so whatever is selected plays in both ears. Built once
     per element — a MediaElementSource can only be created once per element,
     and creating a second one throws.

     This is why the bucket needs a CORS rule for the dashboard's origin:
     `createMediaElementSource` on a cross-origin
     element the bucket has not approved returns a graph that outputs silence,
     while plain playback goes on working. Missing CORS therefore looks like
     "the Caller/Agent switch is broken", not like a network error. */
  const gainsRef = useRef<{ caller: GainNode; agent: GainNode } | null>(null);
  const contextRef = useRef<AudioContext | null>(null);

  useEffect(() => {
    return () => {
      void contextRef.current?.close();
    };
  }, []);

  /* Escape and the browser's own chrome can leave fullscreen without going
     through the button, so the label follows the document rather than a click. */
  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === stageRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  /* Both halves reject rather than throw, and an unhandled rejection is a
     button that did nothing and said nothing. The browser refuses for reasons a
     page cannot see — a permissions policy on an embedding frame, a window that
     is already occupied — so the honest answer is to say it was refused and
     leave the player exactly where it was. */
  const toggleFullscreen = useCallback(() => {
    const request = document.fullscreenElement
      ? document.exitFullscreen()
      : stageRef.current?.requestFullscreen();
    request?.catch(() => setError("This browser would not let the screen go full screen."));
  }, []);

  const buildGraph = useCallback(() => {
    const el = audioRef.current;
    if (!el || gainsRef.current) return;
    /* Safari still only has the prefixed constructor on older versions. */
    const Ctor: typeof AudioContext | undefined =
      window.AudioContext ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return; // no Web Audio: plain stereo playback still works
    const ctx = new Ctor();
    contextRef.current = ctx;
    const source = ctx.createMediaElementSource(el);
    const splitter = ctx.createChannelSplitter(2);
    const callerGain = ctx.createGain();
    const agentGain = ctx.createGain();
    const merger = ctx.createChannelMerger(2);
    source.connect(splitter);
    splitter.connect(callerGain, 0);
    splitter.connect(agentGain, 1);
    /* Each side into BOTH output channels, so an isolated voice is centred
       rather than stuck in one ear. */
    callerGain.connect(merger, 0, 0);
    callerGain.connect(merger, 0, 1);
    agentGain.connect(merger, 0, 0);
    agentGain.connect(merger, 0, 1);
    merger.connect(ctx.destination);
    gainsRef.current = { caller: callerGain, agent: agentGain };
  }, []);

  useEffect(() => {
    const gains = gainsRef.current;
    if (!gains) return;
    gains.caller.gain.value = channel === "agent" ? 0 : 1;
    gains.agent.gain.value = channel === "caller" ? 0 : 1;
  }, [channel, playing]);

  const toggle = useCallback(async () => {
    const el = clockRef.current;
    if (!el) return;
    if (el.paused) {
      buildGraph();
      /* Browsers start an AudioContext suspended until a user gesture. */
      await contextRef.current?.resume();
      syncScreen(el.currentTime);
      await el.play();
      if (clockIsAudio) await videoRef.current?.play();
    } else {
      el.pause();
      if (clockIsAudio) videoRef.current?.pause();
    }
  }, [buildGraph, clockIsAudio, clockRef, syncScreen]);

  /* Clicking a turn seeks here, and starts playing from there — "jump to this
     moment" reading as nothing-happened is the worst possible answer to a
     click. */
  useEffect(() => {
    if (!seekTo) return;
    const el = clockRef.current;
    if (!el) return;
    const jump = () => {
      el.currentTime = seekTo.at;
      setPosition(seekTo.at);
      syncScreen(seekTo.at);
      if (el.paused) void toggle();
    };
    /* `currentTime` does not stick before the element knows how long the file
       is; `preload="metadata"` means that is usually already true. */
    if (el.readyState === HTMLMediaElement.HAVE_NOTHING) {
      el.addEventListener("loadedmetadata", jump, { once: true });
      return () => el.removeEventListener("loadedmetadata", jump);
    }
    jump();
  }, [clockRef, seekTo, syncScreen, toggle]);

  /* Whole seconds only. `timeupdate` fires about four times a second, and the
     listener re-renders the entire call detail — a sixty-turn transcript — to
     move a highlight that can only land on one turn per second anyway. */
  const playheadS = src || screenSrc ? Math.floor(position) : null;
  useEffect(() => {
    onPosition?.(playheadS);
  }, [onPosition, playheadS]);

  const remove = useCallback(async () => {
    setDeleting(true);
    setError(null);
    try {
      await api.deleteCallRecording(sessionId);
      setConfirmingDelete(false);
      onDeleted();
    } catch (e) {
      setError(apiErrorMessage(e));
      setDeleting(false);
    }
  }, [sessionId, onDeleted]);

  /* The screen's own sentence, shown under the audio's whenever it says
     something the audio's does not. On the common call — screen recording off —
     it says nothing at all, because a panel that explains a feature nobody
     turned on is noise. */
  const screenNote =
    screenRecording.state === "available" || screenRecording.state === "none"
      ? null
      : (SCREEN_EMPTY_STATE[screenRecording.state] ?? EMPTY_STATE[screenRecording.state]);

  if (recording.state !== "available" && screenRecording.state !== "available") {
    return (
      <Panel className="grid gap-1.5">
        <div className="text-[13px] font-medium text-ink">Recording</div>
        <p className="text-[12.5px] leading-5 text-muted">{EMPTY_STATE[recording.state]}</p>
        {screenNote && <p className="text-[12.5px] leading-5 text-muted">{screenNote}</p>}
      </Panel>
    );
  }

  /* Two files when a screen was kept, so each is named — one number beside the
     word "Recording" would be the audio's, describing a panel holding both. */
  const size = fmtBytes(recording.bytes);
  const screenSize = fmtBytes(screenRecording.bytes);
  const expires = recording.expires_at ? new Date(recording.expires_at) : null;
  /* Every clock event lands on whichever element is driving, so the handlers are
     written once and spread onto it. */
  const clockProps = {
    onPlay: () => setPlaying(true),
    onPause: () => setPlaying(false),
    onEnded: () => {
      setPlaying(false);
      if (clockIsAudio) videoRef.current?.pause();
    },
    onTimeUpdate: (e: { currentTarget: HTMLMediaElement }) => {
      setPosition(e.currentTarget.currentTime);
      syncScreen(e.currentTarget.currentTime);
    },
    onLoadedMetadata: (e: { currentTarget: HTMLMediaElement }) => {
      if (Number.isFinite(e.currentTarget.duration)) setDuration(e.currentTarget.duration);
    },
  };

  return (
    <Panel className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[13px] font-medium text-ink">Recording</div>
        <div className="flex items-center gap-1.5 text-[12px] text-faint">
          {size && <span>{screenSize ? `${size} audio` : size}</span>}
          {screenSize && <span>· {screenSize} screen</span>}
          {expires && <span>· kept until {expires.toLocaleDateString()}</span>}
        </div>
      </div>

      {!src && !screenSrc ? (
        /* `state` said available and the detail carried no link — a bug rather
           than an empty state, so it says so instead of showing dead controls. */
        <p className="text-[12.5px] leading-5 text-danger">
          This recording could not be opened. Reload the page to try again.
        </p>
      ) : (
        <div className="grid gap-2.5">
          {/* Controlled entirely by the buttons below: the native control set
              cannot express the channel choice, and showing both invites the
              user to drive two players at once. */}
          {src && (
            <audio
              ref={audioRef}
              src={src}
              crossOrigin="anonymous"
              preload="metadata"
              onError={() =>
                setError("The audio could not be loaded. Reload the page for a fresh link.")
              }
              {...clockProps}
            />
          )}
          {/* The stage: the screen above the transport that drives it, and the
              pair of them is what goes fullscreen. Everything destructive stays
              outside it — a Delete button on a black fullscreen surface is a
              misclick nobody can undo. */}
          <div
            ref={stageRef}
            className="relative grid gap-2.5 [&:fullscreen]:content-center [&:fullscreen]:bg-ink [&:fullscreen]:p-5"
          >
            {/* Rendered only when there is a screen — a black rectangle on the
                ordinary call reads as a broken player, so a call with no screen
                shows none. Inline it is capped against the viewport rather than
                at a fixed height: this is a 720p capture of somebody's editor,
                and the whole point of keeping it is being able to read what was
                on it. Fullscreen is where that actually happens.

                Full width with the picture letterboxed inside it on `bg-ink`,
                rather than a box that hugs the frame: the video is 16:9 and the
                panel is far wider than that at any height worth giving it, so
                something has to fill the sides. Black beside a screen recording
                reads as a player; the page's own white does not. It is also what
                keeps the Full screen chip over the video at every window size. */}
            {screenSrc && (
              <>
                <video
                  ref={videoRef}
                  src={screenSrc}
                  muted
                  playsInline
                  preload="metadata"
                  onClick={toggle}
                  title={playing ? "Pause" : "Play"}
                  className="max-h-[min(46vh,520px)] w-full cursor-pointer rounded-lg border border-line-2 bg-ink object-contain [:fullscreen_&]:max-h-[calc(100vh_-_7rem)] [:fullscreen_&]:rounded-none [:fullscreen_&]:border-0"
                  {...(clockIsAudio ? {} : clockProps)}
                  onError={() =>
                    setError(
                      "The screen recording could not be loaded. Reload the page for a fresh link.",
                    )
                  }
                />
                <button
                  type="button"
                  onClick={toggleFullscreen}
                  className="absolute right-2 top-2 rounded-lg bg-ink/70 px-2.5 py-1 text-[12px] font-medium text-white backdrop-blur-sm transition-colors hover:bg-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
                >
                  {fullscreen ? "Exit full screen" : "Full screen"}
                </button>
              </>
            )}
            <div className="flex flex-wrap items-center gap-2.5">
              <Button variant="secondary" size="sm" onClick={toggle}>
                {playing ? "Pause" : "Play"}
              </Button>
              <input
                type="range"
                min={0}
                max={duration || 0}
                step={0.1}
                value={Math.min(position, duration || 0)}
                onChange={(e) => {
                  const at = Number(e.target.value);
                  setPosition(at);
                  if (clockRef.current) clockRef.current.currentTime = at;
                  syncScreen(at);
                }}
                aria-label="Seek"
                className="h-1 min-w-40 flex-1 cursor-pointer accent-ink [:fullscreen_&]:accent-white"
              />
              <span className="font-mono text-[12px] tabular-nums text-muted [:fullscreen_&]:text-white/70">
                {fmtClock(position)} / {fmtClock(duration)}
              </span>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {src && (
              <div className="inline-flex overflow-hidden rounded-[10px] border border-line-2">
                {CHANNELS.map((c) => (
                  <button
                    key={c.value}
                    type="button"
                    title={c.title}
                    onClick={() => setChannel(c.value)}
                    className={
                      c.value === channel
                        ? "min-h-8 bg-ink px-2.5 text-[13px] font-medium text-white"
                        : "min-h-8 bg-white px-2.5 text-[13px] text-ink-soft hover:bg-subtle"
                    }
                  >
                    {c.label}
                  </button>
                ))}
              </div>
            )}
            {/* No `download` attribute: the file is cross-origin and the
                attribute is ignored there, so the filename comes from the
                bucket's own Content-Disposition — which is exactly what this
                second signed URL carries. */}
            {recording.download_url && (
              <a
                href={recording.download_url}
                className="text-[12.5px] text-ink-soft underline underline-offset-2 hover:text-ink"
              >
                {screenRecording.download_url ? "Download audio" : "Download"}
              </a>
            )}
            {screenRecording.download_url && (
              <a
                href={screenRecording.download_url}
                className="text-[12.5px] text-ink-soft underline underline-offset-2 hover:text-ink"
              >
                Download screen
              </a>
            )}
            <Button
              variant="danger"
              size="sm"
              onClick={() => setConfirmingDelete(true)}
              disabled={deleting}
            >
              Delete
            </Button>
          </div>
        </div>
      )}

      {screenNote && (
        <p className="text-[12.5px] leading-5 text-muted">{screenNote}</p>
      )}

      {confirmingDelete && (
        <Modal
          title={screenSrc ? "Delete this call's recordings?" : "Delete this recording?"}
          sub={
            screenSrc
              ? "The audio and the screen video are removed from storage straight away. This cannot be undone."
              : "The audio is removed from storage straight away. This cannot be undone."
          }
          width="max-w-[460px]"
          onClose={() => !deleting && setConfirmingDelete(false)}
          footer={
            <>
              <Button
                variant="secondary"
                onClick={() => setConfirmingDelete(false)}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button variant="danger" onClick={() => void remove()} disabled={deleting}>
                {deleting ? "Deleting…" : screenSrc ? "Delete recordings" : "Delete recording"}
              </Button>
            </>
          }
        >
          <p className="pb-4 text-[13px] leading-6 text-muted">
            The transcript, cost, analysis and everything else about this call stay exactly as they
            are — only the {screenSrc ? "recorded media goes" : "audio goes"}.
          </p>
        </Modal>
      )}

      {error && <p className="text-[12.5px] leading-5 text-danger">{error}</p>}
    </Panel>
  );
}
