"""talqing data-plane worker — one generic entrypoint, explicit dispatch.

Per call: read job metadata, route to a web room, a SIP call or a partner's
media stream, then run the shared ``VoiceRun`` lifecycle. Fully self-hosted — no
LiveKit Cloud inference.
"""

from __future__ import annotations

import json

from livekit.agents import AgentServer, JobContext, JobProcess, cli
from livekit.agents.voice import agent_session
from livekit.plugins import silero

from services.catalog import get_catalog
from settings import get_settings
from workers.voice import runtime
from workers.voice.recorder import TalqingRecorderIO

# These two belong at module scope. Moving them into `entrypoint` puts them on
# the call's critical path, and costs a second time by arriving too late for the
# forkserver.
#
# A job process serves exactly one call, so an import inside the entrypoint is
# paid per call, between the job being assigned and `ctx.connect()` — measured at
# 5.9s on tq-dev, all of it before the agent can join the room.
#
# Module scope also has to mean *before* `cli.run_app` below. These pull in the
# ten `livekit.plugins.*` packages that `compiler/factories.py` names, and the
# SDK seeds the forkserver's preload list from `Plugin.registered_plugins` when
# the worker starts (agents/worker.py). Registered by then, they are imported
# once in the forkserver and inherited by every job process; registered any later
# — from `prewarm`, say — the preload list is silero alone, and each process pays
# for all ten itself.
from workers.voice.room_web import run_web_room
from workers.voice.sip import run_sip_call
from workers.voice.stream import run_stream_call
from workers.voice.whatsapp import run_whatsapp_call

# No `logging.basicConfig` here, deliberately, and adding one back is not a
# formatting choice — it triples the log volume of every call.
#
# `cli.run_app` below installs this process's only root handler (JSON at INFO,
# `agents/cli/_legacy.py::_configure_logger`) and *adds* it to root rather than
# replacing what is there, so a handler installed at import survives forever.
# A job process then imports this module — unpickling `ProcStartArgs` resolves
# `prewarm` and `entrypoint` by name — so it gets that handler too, prints
# through it, and then forwards the same record to the parent over
# `LogQueueHandler`, whose listener replays it through *both* of the parent's
# handlers. One line logged during a call, printed three times.
#
# Levels are not this file's job either: the SDK snapshots every logger's level
# (root included) into `ProcStartArgs.logger_levels` and reapplies it in each
# job process, so INFO carries across without help. Override with
# `LIVEKIT_LOG_LEVEL`, not with a handler.

# Recording follows whichever agent is speaking, which needs a recorder that can
# pause — see `workers.voice.recorder`. `AgentSession` constructs its own
# (`agent_session.py`), and `_forward_audio_task` binds `input.audio` once, so
# there is no point at which the chain can be wrapped from outside; substituting
# the class in that module's namespace is the whole seam. One line, here, where
# it happens before any job process imports the session.
agent_session.RecorderIO = TalqingRecorderIO  # type: ignore[misc]

_settings = get_settings()

server = AgentServer(
    # The agents SDK otherwise reads LIVEKIT_URL / _API_KEY / _API_SECRET from the
    # process environment, which we do not set — configs/{ENV}.config.yaml is the
    # only source of truth. `url` is the private signal address and may point at
    # any node in the cluster; Redis routes the job to whichever hosts the room.
    ws_url=_settings.livekit.url,
    api_key=_settings.livekit.api_key,
    api_secret=_settings.livekit.api_secret,
    prometheus_port=9091,  # scraped over the compose bridge; self-hosted metrics
    # This kwarg alone is NOT enough, and the compose file sets
    # PROMETHEUS_MULTIPROC_DIR to the same path for that reason — do not delete
    # it as a duplicate. prometheus_client picks its value class exactly once,
    # when `prometheus_client.values` is imported, and the SDK exports this
    # variable only at Worker start, long after `import livekit.agents` above.
    # Without the environment variable the four SDK metrics sit in the global
    # registry while /metrics renders a MultiProcessCollector over an empty
    # directory, and :9091 answers 200 with a zero-byte body.
    #
    # What it is for once it works: aggregating across job processes. Nothing of
    # ours may take advantage of that — a metric written from a job process
    # leaves a 64 KiB file per call that every scrape re-reads for ever, and
    # deleting one makes a counter appear to reset. Per-call detail belongs in
    # `sessions` and in traces, not here.
    prometheus_multiproc_dir="/tmp/talqing-prom",
    # SIGTERM marks the worker unavailable, then waits this long for live calls
    # to end on their own. Held at the 3h call cap
    # (livekit.sip.max_call_duration_seconds) so that a call
    # accepted the instant before SIGTERM still reaches its own deadline rather
    # than being cut short by the drain. Calls that somehow outlast it are still
    # closed gracefully by aclose() (on_session_end, close_reason, pricing),
    # which is what compose's extra 60s of stop_grace_period is for. Raising the
    # call cap without raising this reintroduces force-closed calls on deploy.
    drain_timeout=10800,
    # The SDK's default is 10s, and process init no longer fits under it: each
    # job process now imports the whole compiler and its plugin chain (see the
    # imports above), and at boot `num_idle_processes` of them — one per vCPU —
    # do it at once, which is ~14s on the dev droplet's four. Overrunning this is
    # not a warning: the process is SIGKILLed and respawned, so a value that is
    # merely tight reads as a boot loop rather than as a timeout. Nothing in
    # prewarm touches the network, so there is no hang a shorter one would catch.
    initialize_process_timeout=60.0,
    # How long the supervisor waits for a job process to exit before SIGKILLing
    # it. Finalize runs entirely inside that window — the SDK sends `Exiting`,
    # *then* runs the shutdown callbacks (agents/ipc/job_proc_lazy_main.py), and
    # ours is where the session row, both recording uploads, post-call analysis
    # and pricing happen — so this is the real ceiling on finalize, and
    # `runtime.FINALIZE_SHUTDOWN_TIMEOUT_SECONDS` is only the ceiling finalize
    # thinks it has.
    #
    # The SDK's default is 10s, which was under that budget by a factor of nine
    # and under a normal call's finalize by about three seconds. It went unnoticed
    # while the audio was the only upload — a short call finished in ~7s — and
    # showed up the moment a second one joined it: a call whose screen share was
    # recorded was killed mid-upload, leaving both recordings on 'pending'
    # forever and the call detail saying neither could be saved. A SIGKILL leaves
    # no log of its own, so the only trace was a `pending` row.
    #
    # Held one finalize timeout above ours so the failure is always ours to
    # report: `finalize_with_timeout` gives up first and logs which session and
    # how long, instead of the process dying silently with the row half-written.
    shutdown_process_timeout=runtime.FINALIZE_SHUTDOWN_TIMEOUT_SECONDS + 15.0,
)


def prewarm(proc: JobProcess) -> None:
    # load expensive shared models ONCE per process (never tenant state)
    settings = get_settings()
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=settings.vad.min_speech_duration,
        min_silence_duration=settings.vad.min_silence_duration,
        activation_threshold=settings.vad.activation_threshold,
    )
    # Parsed and validated here, in the idle process, instead of on the call's
    # critical path: the compile reads it, and building it blocks the event loop
    # for most of a second. `lru_cache`d, so the call finds it ready.
    get_catalog()
    # The Responses API's stream module, which livekit-agents' own warm-up
    # (`ipc/_preload.py`, `openai.resources`) does not import. The call's first
    # streamed response imported it on the event loop, landing on the greeting.
    # Its models build at import because the voice worker runs with
    # DEFER_PYDANTIC_BUILD=false (set in the compose file).
    import openai.lib.streaming.responses  # noqa: F401


server.setup_fnc = prewarm


@server.rtc_session(agent_name=_settings.livekit.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    if meta.get("kind") == "sip_call":
        await run_sip_call(ctx, meta)
        return
    if meta.get("kind") == "stream_call":
        await run_stream_call(ctx, meta)
        return
    if meta.get("kind") == "whatsapp_call":
        await run_whatsapp_call(ctx, meta)
        return
    await run_web_room(ctx, meta)


if __name__ == "__main__":
    cli.run_app(server)
